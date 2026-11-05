// User-triggered class verification and semantic template authoring. No network writes on initialization.
'use strict';

async function aoaAdvancedTask(label, task) {
    if (aoaPanelState.running || aoaPanelState.playbackActive) return;
    if (aoaDebugState.id) { aoaSetStatus('请先结束 HID 调试，再验证或解析语义模板', 'warning'); return; }
    aoaSetBusy(true, label);
    try {
        const result = await task();
        aoaShowResult(label, result);
        aoaSetStatus(`${label}完成`, 'success');
        return result;
    } catch (error) {
        aoaShowResult(label, {error: error.message});
        aoaSetStatus(error.message, 'error');
    } finally { aoaSetBusy(false); }
}

function aoaAdvancedTarget(recording = false) {
    const prefix = recording ? 'aoa-record' : 'aoa-replay';
    const worker_id = aoaEl(`${prefix}-worker`)?.value;
    const device = aoaEl(`${prefix}-device`)?.value;
    if (!worker_id || !device) throw new Error('请先选择对应的 Worker 和设备');
    return {worker_id, device};
}

async function aoaVerifyClassReference() {
    return aoaAdvancedTask('标准 direct 参考验证', async () => {
        const payload = await aoaApi('/api/devices/aoa/certifications/verify', {method: 'POST', body: {
            ...aoaAdvancedTarget(true), display_id: Number(aoaEl('aoa-reference-display').value)}});
        const commandId = payload.command_id;
        const result = await aoaAwaitCommand(payload, {trackPlayback: false, onProgress: () => {}});
        if (result.success !== true || !result.direct_reference_evidence || !commandId) {
            throw new Error('未收到完整参考验证回执，不能用于类别认证');
        }
        const input = aoaEl('aoa-reference-receipts');
        input.value = [...new Set([...input.value.split(/\s+/).filter(Boolean), commandId])].join('\n');
        return {...result, command_id: commandId};
    });
}

async function aoaIssueClassCertificate() {
    return aoaAdvancedTask('签发设备类别认证', async () => {
        const result = await aoaApi('/api/devices/aoa/certifications', {method: 'POST', body: {
            label: aoaEl('aoa-certificate-label').value.trim(),
            command_ids: aoaEl('aoa-reference-receipts').value.split(/\s+/).filter(Boolean)}});
        const ids = aoaEl('aoa-template-certificates');
        ids.value = [...new Set([...ids.value.split(/\s+/).filter(Boolean), result.certificate.id])].join('\n');
        await aoaRefreshClassCertificates();
        return result;
    });
}

async function aoaRefreshClassCertificates() {
    try {
        const result = await aoaApi('/api/devices/aoa/certifications');
        const select = aoaEl('aoa-class-certificates');
        select.replaceChildren();
        for (const item of result.certificates || []) {
            const option = document.createElement('option');
            option.value = item.id;
            option.textContent = `${item.label} · ${item.id}${item.revoked_at ? ' · 已撤销' : ''}`;
            option.disabled = Boolean(item.revoked_at);
            select.append(option);
        }
    } catch (error) { aoaSetStatus(error.message, 'error'); }
}

async function aoaRevokeClassCertificate() {
    const id = aoaEl('aoa-class-certificates').value;
    if (!id) return;
    return aoaAdvancedTask('撤销类别认证', async () => {
        await aoaApi(`/api/devices/aoa/certifications/${encodeURIComponent(id)}`, {method: 'DELETE'});
        await aoaRefreshClassCertificates();
        return {success: true, certificate_id: id};
    });
}

function aoaTemplateRead() {
    aoaEl('aoa-semantic-template').value = JSON.stringify(aoaHidMetadata().semantic_template || {}, null, 2);
    aoaEl('aoa-template-certificates').value = (aoaHidMetadata().class_certificate_ids || []).join('\n');
    aoaEl('aoa-template-display').value = String(aoaHidMetadata().target_display_id ?? 0);
}

function aoaTemplateApply() {
    if (aoaPanelState.running) return;
    try {
        const text = aoaEl('aoa-semantic-template').value;
        if (new TextEncoder().encode(text).length > 65536) throw new Error('语义模板不能超过 64 KiB');
        const template = JSON.parse(text);
        if (template.version !== 1 || !Array.isArray(template.layouts) || !Array.isArray(template.steps)
                || template.steps.length !== aoaPanelState.commands.length) {
            throw new Error('模板版本须为 1，包含 layouts，并且 steps 与当前命令逐条对应');
        }
        const ids = aoaEl('aoa-template-certificates').value.split(/\s+/).filter(Boolean);
        if (!ids.length) throw new Error('请选择已签发的类别认证 ID');
        const displayId = Number(aoaEl('aoa-template-display').value);
        if (!Number.isInteger(displayId) || displayId < 0 || displayId > 255) throw new Error('目标显示器 ID 须为 0..255');
        aoaPanelState.profileMetadata = {...aoaHidMetadata(), semantic_template: template,
            class_certificate_ids: ids, target_display_id: displayId};
        aoaEl('aoa-hid-touch-mode').value = 'direct';
        aoaEl('aoa-hid-session-mode').value = 'per_command';
        aoaPanelState.calibration = null;
        delete aoaPanelState.profileMetadata.calibration;
        delete aoaPanelState.profileMetadata.mapping_policy;
        aoaRenderCommands();
        aoaSetStatus('语义模板已应用到当前命令；保存时由服务端完整校验。页面断言需要 ADB 或绑定画面源');
    } catch (error) { aoaSetStatus(error.message, 'error'); }
}

async function aoaResolveSemanticTemplate() {
    return aoaAdvancedTask('匹配目标语义模板', async () => {
        const result = await aoaApi('/api/devices/aoa/templates/resolve', {method: 'POST', body: {
            ...aoaAdvancedTarget(), display_id: Number(aoaEl('aoa-template-display').value)}});
        aoaEl('aoa-replay-profile').value = result.match.profile_id;
        aoaLoadSelectedProfile();
        aoaPanelState.profileMetadata.target_display_id = result.match.display_id;
        aoaTemplateRead();
        return result.match;
    });
}

async function aoaCapturePageReference() {
    return aoaAdvancedTask('采集页面画面参考', async () => {
        const target = aoaAdvancedTarget();
        const roi = aoaEl('aoa-reference-roi').value.split(',').map(value => Number(value.trim()));
        const payload = await aoaApi('/api/devices/aoa/record/input', {method: 'POST', body: {
            ...target, input: {action: 'observe_frame', roi}}});
        const result = await aoaAwaitCommand(payload, {trackPlayback: false, onProgress: () => {}});
        aoaEl('aoa-frame-reference').value = JSON.stringify(result.condition, null, 2);
        return result;
    });
}

async function aoaExecuteSemanticStep(index, worker, device, command, onProgress) {
    const metadata = aoaHidMetadata();
    const step = metadata.semantic_template?.steps[index];
    if (!step) throw new Error('语义步骤与当前命令不匹配，请更新模板');
    if (aoaDebugState.id) throw new Error('语义断言需在 USB 恢复后执行，请先结束 HID 调试');
    const payload = await aoaApi('/api/devices/aoa/recover', {method: 'POST', body: {
        worker_id: worker, device, commands: [command], wait_adb_seconds: 0,
        metadata: {...metadata, semantic_template: {...metadata.semantic_template, steps: [step]}}}});
    return aoaAwaitCommand(payload, {trackPlayback: false, onProgress});
}
