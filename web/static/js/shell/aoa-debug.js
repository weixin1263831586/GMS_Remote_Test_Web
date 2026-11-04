// 用户逐条输入，共用一个有租约和期限的 USB/HID 会话。
'use strict';

const aoaDebugState = {id: '', commandId: '', worker: '', device: '', timer: 0, pending: null, ending: false, lastEndedId: ''};

function aoaDebugSyncTarget() {
    if (!aoaDebugState.id) return false;
    const {worker, device} = aoaDebugState;
    const inventory = aoaPanelState.devices;
    const target = inventory.find(item => aoaDeviceWorker(item) === worker && aoaDeviceSerial(item) === device);
    const bindings = [
        ['aoa-replay-worker', worker, worker, !inventory.some(item => aoaDeviceWorker(item) === worker)],
        ['aoa-replay-device', device, target ? aoaDeviceLabel(target) : device, !target],
    ];
    for (const [id, value, label, missing] of bindings) {
        const select = aoaEl(id);
        aoaReplaceOptions(select, [{value, label, missing}], '');
        if (select) { select.value = value; select.disabled = true; }
    }
    return true;
}

function aoaDebugButtonState() {
    const active = Boolean(aoaDebugState.id);
    const busy = aoaPanelState.running || aoaDebugState.ending;
    const keep = aoaEl('aoa-debug-keep');
    if (keep) keep.disabled = busy;
    const end = aoaEl('aoa-debug-end');
    if (end) end.disabled = !active || aoaDebugState.ending;
    if (active) {
        aoaDebugSyncTarget();
        document.querySelectorAll('#aoa-panel select, #aoa-hid-settle, #aoa-panel [data-aoa-run], #aoa-panel [data-aoa-screen-action], #aoa-record-start, #aoa-panel [data-click="aoaLoadSelectedProfile"], #aoa-panel [data-click="aoaCommandsImport"]')
            .forEach(node => { node.disabled = true; });
    }
}

function aoaDebugReset() {
    clearInterval(aoaDebugState.timer);
    Object.assign(aoaDebugState, {id: '', commandId: '', worker: '', device: '', timer: 0, pending: null});
    aoaSyncDeviceSources(aoaPanelState.devices);
}

function aoaDebugWatch() {
    if (aoaDebugState.timer) return;
    aoaDebugState.timer = setInterval(async () => {
        const id = aoaDebugState.commandId;
        if (!id || aoaDebugState.ending) return;
        try {
            const status = await aoaApi(`/api/cluster/commands/${encodeURIComponent(id)}`, {timeoutMs: 5000});
            const command = status.command || {};
            if (id !== aoaDebugState.commandId || !['completed', 'cancelled', 'failed'].includes(command.status)) return;
            if (aoaPanelState.running) return; // 当前单条轮询负责其执行结果。
            aoaDebugReset();
            aoaSetBusy(false);
            const clean = command.status === 'completed' && !command.result?.cleanup_errors?.length;
            aoaSetStatus(clean ? '调试空闲超时，HID 连接已结束'
                : `调试连接已结束，请检查 USB 状态：${command.error || '清理异常'}`, clean ? '' : 'warning');
        } catch (error) { /* 网络恢复后继续跟随原会话，不重新发送输入。 */ }
    }, 2000);
}

async function aoaDebugExecute(command, worker, device, onProgress) {
    if (aoaDebugState.pending) throw new Error('上条调试命令结果尚未确认，请结束调试并核对设备状态');
    if (aoaDebugState.id && (aoaDebugState.worker !== worker || aoaDebugState.device !== device)) {
        throw new Error('调试连接已绑定其他设备，请先结束调试');
    }
    if (!aoaDebugState.id) {
        Object.assign(aoaDebugState, {id: `dbg-${crypto.randomUUID()}`, worker, device});
    }
    const metadata = aoaHidMetadata();
    const body = {worker_id: worker, devices: [device], action: 'aoa_sequence',
        aoa_commands: aoaPanelState.commands.slice(), wait_adb_seconds: 0,
        aoa_hid_touch_mode: metadata.hid_touch_mode, aoa_hid_settle_seconds: metadata.hid_settle_seconds,
        aoa_hid_session_mode: 'continuous', playback_control_id: aoaDebugState.id,
        aoa_debug_session_id: aoaDebugState.id, aoa_debug_request_id: crypto.randomUUID(), aoa_debug_command: command};
    aoaDebugState.pending = body;
    aoaDebugButtonState();
    try {
        const accepted = await aoaApi('/api/cluster/devices/actions', {method: 'POST', body, timeoutMs: 15000});
        if (!accepted.command_id || !accepted.debug_sequence) throw new Error('未收到调试会话回执，请核对原操作');
        aoaDebugState.commandId = accepted.command_id;
        aoaDebugWatch();
        const result = await aoaAwaitCommand(accepted, {trackPlayback: false,
            debugSequence: accepted.debug_sequence, onProgress});
        aoaDebugState.pending = null;
        return result;
    } catch (error) {
        // A semantic rejection sent no input; transport failures retain the
        // pending request so another click cannot accidentally repeat it.
        if (error.status && error.status >= 400 && error.status < 500) {
            aoaDebugState.pending = null;
            if (!aoaDebugState.commandId) aoaDebugReset();
        }
        if (aoaDebugState.ending || aoaDebugState.lastEndedId === body.aoa_debug_session_id) error.cancelledByUser = true;
        throw error;
    }
}

async function aoaDebugEnd() {
    if (!aoaDebugState.id || aoaDebugState.ending) return;
    aoaDebugState.ending = true;
    aoaSetBusy(true, '正在结束调试并清理 HID…');
    try {
        let id = aoaDebugState.commandId;
        try {
            const result = await aoaApi(`/api/devices/aoa/playback/${encodeURIComponent(aoaDebugState.id)}/control`,
                {method: 'POST', body: {action: 'cancel', worker_id: aoaDebugState.worker}, timeoutMs: 10000});
            id = result.command_id || id;
        } catch (error) { if (error.status !== 404 || !id) throw error; }
        const deadline = Date.now() + 60000;
        while (id && Date.now() < deadline) {
            const status = await aoaApi(`/api/cluster/commands/${encodeURIComponent(id)}`, {timeoutMs: 5000});
            const command = status.command || {};
            if (['completed', 'cancelled', 'failed'].includes(command.status)) {
                const warning = command.result?.cleanup_errors?.length || command.status === 'failed';
                aoaDebugState.lastEndedId = aoaDebugState.id;
                aoaDebugReset();
                aoaSetStatus(warning ? `调试已结束，请检查 USB 状态：${command.error || '清理异常'}` : '调试已结束，HID 连接已释放', warning ? 'warning' : 'success');
                return;
            }
            await new Promise(resolve => setTimeout(resolve, 300));
        }
        throw new Error('清理尚未确认，请稍后再次结束调试并核对设备状态');
    } catch (error) {
        aoaSetStatus(`结束调试未确认：${error.message}`, 'warning');
    } finally {
        aoaDebugState.ending = false;
        aoaSetBusy(false);
    }
}

async function aoaDebugKeepChanged() {
    if (!aoaEl('aoa-debug-keep').checked && aoaDebugState.id) await aoaDebugEnd();
}

async function aoaCommandExecute(index) {
    if (aoaPanelState.running || aoaPanelState.playbackActive || !Number.isInteger(index)) return;
    if (!aoaCommandEditFinish()) return;
    const command = aoaPanelState.commands[index];
    if (typeof command !== 'string' || !command.trim()) return;
    aoaDebugSyncTarget();
    const worker = aoaDebugState.id ? aoaDebugState.worker : aoaEl('aoa-replay-worker')?.value;
    const device = aoaDebugState.id ? aoaDebugState.device : aoaEl('aoa-replay-device')?.value;
    if (!worker || !device) {
        aoaSetStatus('请先选择目标 Worker 和目标设备，再执行单条命令', 'warning');
        showToast('请先选择目标 Worker 和目标设备，再执行单条命令', 'warning'); return;
    }
    aoaPanelState.commandDebug = {index, command, device, state: 'running'};
    aoaSetBusy(true, `正在执行第 ${index + 1} 条：${command}`);
    aoaUpdateCommandDebug('running');
    try {
        const onProgress = event => {
            if (event.phase === 'running' || event.phase === 'failed') aoaUpdateCommandDebug(event.phase);
        };
        const keep = Boolean(aoaEl('aoa-debug-keep')?.checked || aoaDebugState.id);
        let result = keep ? await aoaDebugExecute(command, worker, device, onProgress)
            : await aoaApi('/api/cluster/devices/actions', {method: 'POST', body: {
            worker_id: worker, devices: [device], action: 'aoa_sequence',
            aoa_commands: [command], wait_adb_seconds: 0,
            aoa_hid_touch_mode: aoaHidMetadata().hid_touch_mode,
            aoa_hid_settle_seconds: aoaHidMetadata().hid_settle_seconds,
        }});
        if (!keep) result = await aoaAwaitCommand(result, {trackPlayback: false, onProgress});
        if (result.success !== true || result.commands_executed !== 1 || result.native_mode_restored === false) {
            throw new Error(result.error || (result.native_mode_restored === false
                ? '命令已发送，但设备未恢复原生 USB 模式' : '未收到单条命令执行成功的完整回执'));
        }
        aoaUpdateCommandDebug('completed');
        aoaShowResult(`单条执行完成 · 第 ${index + 1} 条 · ${device} · ${command}`, result);
        aoaSetStatus(`第 ${index + 1} 条执行完成${result.hid_session_kept ? ' · HID 保持连接' : ''}：${command}`, 'success');
        if (result.cleanup_errors?.length) showToast('命令已执行，USB 清理出现异常，请检查设备状态', 'warning');
    } catch (error) {
        if (error.cancelledByUser) { aoaUpdateCommandDebug('cancelled'); return; }
        aoaUpdateCommandDebug('failed');
        aoaShowResult(`单条执行失败 · 第 ${index + 1} 条 · ${device}`, {error: error.message});
        aoaSetStatus(`第 ${index + 1} 条执行失败：${error.message}`, 'error');
    } finally {
        aoaSetBusy(false);
    }
}
