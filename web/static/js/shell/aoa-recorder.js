// AOA 录制器（①列）：投屏交互、录制输入执行与触摸标定。
// 拆自 shell-aoa.js（体积 ratchet）：本文件只依赖 shell-aoa.js 的共享
// 状态/工具（aoaPanelState/aoaEl/aoaApi/aoaAwaitCommand/aoaAppendCommand/
// aoaSetBusy/aoaSetStatus/aoaHidMetadata/executeClusterCommandAndWait/
// aoaViewIsOpen/showToast/showConfirmDialog），均在函数调用时读取。本文件
// 只声明函数，须在 shell-aoa.js 初始化工作台前加载。真正的输入和回放
// 都经 Controller 的设备 claim/fencing → Worker 边界执行。
'use strict';

function aoaRecorderTarget() {
    const workerId = aoaEl('aoa-record-worker')?.value || '';
    const device = aoaEl('aoa-record-device')?.value || '';
    if (!workerId || !device) throw new Error('请选择录制 Worker 和设备');
    return {workerId, device};
}

async function aoaRefreshScreen() {
    // 实时投屏激活期间画面由流自更新：截图刷新（手动按钮/回退轮询残留）
    // 一律跳过，避免「正在获取…/已连接」状态在实时状态上反复跳动。
    if (window.aoaLiveScreen?.isActive()) return;
    let target;
    try { target = aoaRecorderTarget(); }
    catch (error) { showToast(error.message, 'warning'); return; }
    const refreshId = ++aoaPanelState.screenRefreshId;
    const status = aoaEl('aoa-screen-status');
    if (status) status.textContent = '正在获取设备截图…';
    try {
        const result = await executeClusterCommandAndWait({
            worker_id: target.workerId, devices: [target.device], action: 'screenshot',
        }, () => refreshId === aoaPanelState.screenRefreshId && aoaViewIsOpen());
        if (refreshId !== aoaPanelState.screenRefreshId) return;
        if (!result?.image) throw new Error(result?.error || '截图为空');
        const image = aoaEl('aoa-screen-image'); image.src = result.image; image.hidden = false;
        aoaEl('aoa-screen-empty').hidden = true;
        if (status) status.textContent = `已连接 ${target.device} · 单击、长按或拖动操作`;
    } catch (error) {
        if (refreshId !== aoaPanelState.screenRefreshId) return;
        aoaEl('aoa-screen-image').hidden = true;
        const empty = aoaEl('aoa-screen-empty'); empty.hidden = false;
        empty.textContent = '无法获取画面。AOA 回放或 USB 调试关闭后，ADB 截图会暂时不可用。';
        if (status) status.textContent = `截图失败：${error.message}`;
    }
}

async function aoaExecuteRecorderInput(input, {refresh = true, pace = true} = {}) {
    if (aoaPanelState.running) return null;
    let target;
    try { target = aoaRecorderTarget(); }
    catch (error) { showToast(error.message, 'warning'); return null; }
    const payload = {...input, hid_touch_mode: aoaHidMetadata().hid_touch_mode};
    if (input.action !== 'calibrate' && aoaPanelState.calibration?.measured) {
        payload.calibration_context = aoaPanelState.calibration.context;
        if (aoaPanelState.calibration.mapping) {
            payload.touch_mapping = aoaPanelState.calibration.mapping;
        } else {
            // Legacy single-point records remain supported until re-calibrated.
            payload.scale_x = aoaPanelState.calibration.scale_x;
            payload.scale_y = aoaPanelState.calibration.scale_y;
        }
    }
    aoaSetBusy(true, `执行 ${input.action}…`);
    try {
        let result = await aoaApi('/api/devices/aoa/record/input', {
            method: 'POST', body: {worker_id: target.workerId, device: target.device, input: payload},
        });
        result = await aoaAwaitCommand(result);
        if (result?.success === false) throw new Error(result.error || `${input.action} 执行失败`);
        if (target.workerId !== aoaEl('aoa-record-worker')?.value || target.device !== aoaEl('aoa-record-device')?.value) {
            throw new Error('设备选择已改变，请核对原设备的操作结果');
        }
        if (aoaPanelState.recording && result?.command) aoaAppendCommand(result.command, pace);
        if (result?.touch_calibration) {
            aoaPanelState.calibration = result.touch_calibration;
            const key = `gms-aoa-calibration:${target.workerId}:${target.device}`;
            try { localStorage.setItem(key, JSON.stringify(result.touch_calibration)); } catch (_) { /* storage unavailable */ }
            aoaRenderCalibration();
            // 标定成功时同步持久化到 Controller（aoa_calibrations 表，owner
            // 隔离）：跨页面/跨会话可复用，不再只依赖 localStorage 临时状态。
            if (input.action === 'calibrate' && result.touch_calibration.mapping?.verified) {
                try {
                    const saved = await aoaApi('/api/devices/aoa/calibrations', {
                    method: 'POST',
                    body: {mapping: result.touch_calibration.mapping,
                           label: `${target.device} @ ${new Date().toISOString().slice(0, 19)}`},
                    });
                    aoaPanelState.calibration.record_id = saved.calibration.id;
                    aoaRenderCalibration();
                    showToast(`标定已保存：${saved.calibration.id}`, 'success');
                } catch (error) {
                    aoaPanelState.calibration.save_error = error.message;
                    aoaRenderCalibration();
                    showToast(`标定已实测，但保存失败：${error.message}。请重新保存标定。`, 'warning');
                }
            }
        }
        aoaSetStatus(aoaPanelState.recording ? '录制中' : '操作完成', 'success');
        // 实时投屏激活时画面自更新，跳过截图刷新（省一条命令+轮询）。
        if (refresh && !aoaLiveInputSurfaceActive()) {
            setTimeout(() => aoaRefreshScreen(), 500);
        }
        return result;
    } catch (error) {
        if (/recalibrat|calibration_context|display configuration/i.test(error.message)) {
            aoaPanelState.calibration = null;
            try { localStorage.removeItem(`gms-aoa-calibration:${target.workerId}:${target.device}`); } catch (_) { /* storage unavailable */ }
            aoaRenderCalibration();
        }
        aoaSetStatus(`操作失败：${error.message}`, 'error');
        showToast(`AOA 录制操作失败：${error.message}`, 'warning');
        return null;
    } finally { aoaSetBusy(false); }
}

function aoaLiveTarget() {
    const workerId = aoaEl('aoa-record-worker')?.value || '';
    const device = aoaEl('aoa-record-device')?.value || '';
    if (!workerId || !device) return null;
    if (!aoaUsableDevices(workerId, true).some(item => aoaDeviceSerial(item) === device)) return null;
    return {workerId, device};
}

async function aoaToggleLiveScreen() {
    if (window.aoaLiveScreen?.isActive()) {
        window.aoaLiveScreen.stop();
        aoaEl('aoa-toggle-live')?.setAttribute('data-live', 'off');
        aoaSetStatus('实时投屏已关闭');
        return;
    }
    // 手动启动视为对当前目标的确认：同步 liveTargetKey，后续只读联动
    // （重新打开页签）不会误判目标变化而中断会话。
    aoaPanelState.liveTargetKey = `${aoaEl('aoa-record-worker')?.value || ''}:${aoaEl('aoa-record-device')?.value || ''}`;
    const target = aoaLiveTarget();
    if (!target) { showToast('请选择录制 Worker 和设备', 'warning'); return; }
    await window.aoaLiveScreen.start(target.device, target.workerId);
    const on = window.aoaLiveScreen.isActive();
    aoaEl('aoa-toggle-live')?.setAttribute('data-live', on ? 'on' : 'off');
}

function aoaLiveInputSurfaceActive() {
    return Boolean(window.aoaLiveScreen?.isActive());
}

function aoaPointerFrame() {
    const live = aoaLiveInputSurfaceActive();
    const canvas = aoaEl('aoa-screen-canvas');
    const surface = live ? (window.aoaLiveScreen.activeSurface?.() ||
        (!window.aoaLiveScreen.activeSurface && !canvas?.hidden ? canvas : null)) : aoaEl('aoa-screen-image');
    if (!surface || surface.hidden) return null;
    const width = live ? surface.width : surface.naturalWidth;
    const height = live ? surface.height : surface.naturalHeight;
    if (!width || !height) return null;
    return {surface, width, height, worker: aoaEl('aoa-record-worker')?.value,
        device: aoaEl('aoa-record-device')?.value};
}

function aoaPointFromPointer(event, frame = aoaPointerFrame(), clamp = false) {
    if (!frame) return null;
    const rect = frame.surface.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    // object-fit: contain can leave bars inside the element's box as well as
    // outside it. Only the actual video/image content defines input pixels.
    const scale = Math.min(rect.width / frame.width, rect.height / frame.height);
    const left = rect.left + (rect.width - frame.width * scale) / 2;
    const top = rect.top + (rect.height - frame.height * scale) / 2;
    const x = (event.clientX - left) / scale;
    const y = (event.clientY - top) / scale;
    if (!clamp && (x < 0 || y < 0 || x >= frame.width || y >= frame.height)) return null;
    return {x: Math.max(0, Math.min(frame.width - 1, Math.round(x))),
        y: Math.max(0, Math.min(frame.height - 1, Math.round(y)))};
}

function aoaPointerDown(event) {
    if (aoaPanelState.running || aoaPanelState.pointer) return;
    const frame = aoaPointerFrame();
    const point = aoaPointFromPointer(event, frame);
    if (!point) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    aoaPanelState.pointer = {id: event.pointerId, startedAt: performance.now(),
        clientX: event.clientX, clientY: event.clientY, point, frame};
}

function aoaPointerMove(event) {
    const pointer = aoaPanelState.pointer;
    if (!pointer || pointer.id !== event.pointerId) return;
    const stage = aoaEl('aoa-screen-stage').getBoundingClientRect();
    const dx = event.clientX - pointer.clientX; const dy = event.clientY - pointer.clientY;
    const line = aoaEl('aoa-gesture-line'); line.hidden = false;
    line.style.left = `${pointer.clientX - stage.left}px`;
    line.style.top = `${pointer.clientY - stage.top}px`;
    line.style.width = `${Math.hypot(dx, dy)}px`;
    line.style.transform = `rotate(${Math.atan2(dy, dx)}rad)`;
}

function aoaPointerCancel(event) {
    if (aoaPanelState.pointer?.id !== event.pointerId) return;
    aoaPanelState.pointer = null;
    aoaEl('aoa-gesture-line').hidden = true;
}

function aoaPointerUp(event) {
    const pointer = aoaPanelState.pointer;
    if (!pointer || pointer.id !== event.pointerId) return;
    aoaPointerCancel(event);
    const frame = aoaPointerFrame();
    if (aoaPanelState.running || !frame || ['surface', 'width', 'height', 'worker', 'device']
        .some(key => frame[key] !== pointer.frame[key])) return;
    const end = aoaPointFromPointer(event, frame, true);
    if (!end) return;
    const frame_size = [frame.width, frame.height];
    const distance = Math.hypot(event.clientX - pointer.clientX, event.clientY - pointer.clientY);
    const elapsed = Math.round(performance.now() - pointer.startedAt);
    if (distance < 9 && elapsed >= 650) {
        aoaExecuteRecorderInput({action: 'long_click', frame_size, x: pointer.point.x, y: pointer.point.y,
            duration_ms: Math.min(Math.max(elapsed, 650), 3000)});
    } else if (distance < 9) {
        aoaExecuteRecorderInput({action: 'tap', frame_size, x: pointer.point.x, y: pointer.point.y});
    } else {
        aoaExecuteRecorderInput({action: 'swipe', frame_size, x1: pointer.point.x, y1: pointer.point.y,
            x2: end.x, y2: end.y, duration_ms: Math.min(Math.max(elapsed, 150), 2000)});
    }
}

function aoaSendKey(key) { return aoaExecuteRecorderInput({action: key}); }

function aoaSendText() {
    const node = aoaEl('aoa-input-text'); const text = node?.value || '';
    if (!text) return;
    return aoaExecuteRecorderInput({action: 'write', text}).then(result => {
        if (result) node.value = '';
    });
}

async function aoaCalibrateTouch() {
    const ok = await showConfirmDialog(
        'AOA 触摸标定',
        '标定将打开设备上的 AOA Calibration 安全页面，切入 AOAv2 后发送五个拟合点和两个独立验证点。'
        + '请先安装安全标定应用，是否继续？');
    if (!ok) return;
    const result = await aoaExecuteRecorderInput({action: 'calibrate'}, {refresh: true, pace: false});
    if (result?.touch_calibration?.measured) {
        const mapping = result.touch_calibration.mapping || {};
        const verified = mapping.verified || result.touch_calibration.verified;
        showToast(verified ? 'AOA 多点仿射标定成功（已通过误差验证）'
                           : 'AOA 标定完成，但误差超出门槛（unverified）',
                  verified ? 'success' : 'warning');
    }
}

function aoaRenderCalibration() {
    const node = aoaEl('aoa-calibration-status'); if (!node) return;
    const calibration = aoaPanelState.calibration;
    if (!calibration?.measured) {
        node.textContent = '触摸映射：未标定，使用显示分辨率按轴换算（未验证）';
        return;
    }
    const policy = calibration.mapping?.mapping_policy
        || calibration.mapping_policy || '';
    const policyLabel = {
        direct_standard: '标准映射兼容性记录',
        direct_calibrated: '直接触摸实测修正（独立验证通过）',
        direct_unverified: '多点实测未达误差门槛/未验证',
        pointer_calibrated: '指针模式实测映射',
        estimated: '仅按轴估算',
        unsupported: '映射不可用，已禁止自动触摸',
    }[policy] || '';
    node.textContent = `触摸映射：已实测（X ${calibration.scale_x} / Y ${calibration.scale_y} 像素每逻辑单位）`
        + (policyLabel ? ` · ${policyLabel}` : '')
        + (calibration.record_id ? ` · 已保存 ${calibration.record_id}` : '')
        + (calibration.save_error ? ' · 保存失败，仅本页可用' : '');
}
