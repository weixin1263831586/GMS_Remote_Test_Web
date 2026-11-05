// Validate the same server recovery policy before uploading any firmware bytes.
'use strict';

async function checkFirmwareAoaBeforeUpload(workerId, devices, profileId, bootWait) {
    const hint = document.getElementById('firmware-aoa-hint');
    delete hint.dataset.state;
    hint.removeAttribute('title');
    hint.textContent = '正在检查 AOA 配置、目标固件声明与设备环境…';
    const result = await apiCall('/api/cluster/firmware/preflight', 'POST', {
        worker_id: workerId || '', devices, aoa_profile_id: profileId, aoa_boot_wait_seconds: bootWait,
    }, {silentToast: true});
    if (result.ready !== true) throw new Error('未收到 AOA 配置检查通过的回执，尚未上传或烧写');
    hint.textContent = 'AOA 配置检查通过；暂存后仍会核对实际镜像 SHA-256。';
    if (result.expected_firmware_sha256) hint.title = `已验证固件 SHA-256：${result.expected_firmware_sha256}`;
    return result;
}

function firmwareAoaPreflightFailed(error) {
    const message = error.message || 'AOA 配置检查未通过';
    const hint = document.getElementById('firmware-aoa-hint');
    hint.textContent = `烧写前检查未通过，本次尚未上传或烧写：${message} 点上方「查看所选 AOA 配置 / 补齐固件声明」可生成声明草稿。`;
    hint.dataset.state = 'error';
    hint.title = hint.textContent;
    addLogEntry(hint.textContent, 'warning');
    showToast('AOA 配置检查未通过，请查看弹框提示；尚未上传或烧写', 'warning');
}

async function openFirmwareAoaProfile() {
    const id = document.getElementById('firmware-aoa-profile').value;
    if (!id) { showToast('请先选择 AOA 配置', 'warning'); return; }
    if (aoaPanelState.running || aoaPanelState.recording) {
        showToast('请先结束当前 AOA 录制或执行，再打开配置', 'warning'); return;
    }
    closeFirmwareModal();
    switchPage('devices');
    await aoaViewToggle(true);
    if (!aoaPanelState.profiles.some(profile => profile.id === id)) {
        aoaSetStatus('当前账号中未找到该配置，请刷新后重试', 'error'); return;
    }
    aoaEl('aoa-replay-profile').value = id;
    aoaLoadSelectedProfile();
    document.querySelector('.aoa-semantic-tools').open = true;
    aoaFirmwareDeclarationRead();
    aoaEl('aoa-firmware-declaration').focus();
}

function aoaFirmwareTargetDraftContext() {
    // 目标固件环境来自烧写弹框所选设备的清单采集；采集缺失的项保持模板空值，
    // 由人工核对补齐（固件声明是明确实测声明，不做推断填充）。
    const serial = typeof selectedBurnableDeviceIds === 'function' ? selectedBurnableDeviceIds()[0] : '';
    const device = serial ? aoaPanelState.devices.find(item => aoaDeviceSerial(item) === serial) : null;
    if (!device) return null;
    const context = aoaDeviceContext(serial, aoaDeviceWorker(device));
    if (!context) return null;
    return context;
}

function aoaFirmwareDeclarationRead() {
    const existing = aoaHidMetadata().firmware_compatibility;
    const textarea = aoaEl('aoa-firmware-declaration');
    if (existing) {
        textarea.value = JSON.stringify(existing, null, 2);
        return;
    }
    const draft = aoaFirmwareTargetDraftContext();
    const touchMode = aoaEl('aoa-hid-touch-mode')?.value || aoaHidMetadata().hid_touch_mode || 'pointer';
    const declaration = {version: 1, sha256: '',
        context: draft ? {...draft, hid_touch_mode: touchMode}
            : {model: '', release: '', fingerprint: '', kernel: '', display_route: '',
               display: [0, 0], rotation: 0, density: 0, hid_touch_mode: touchMode},
        validated_by: '', validated_at: new Date().toISOString(), evidence: ''};
    textarea.value = JSON.stringify(declaration, null, 2);
    aoaSetStatus(draft
        ? '已按烧写目标设备生成固件声明草稿：补固件 SHA-256、验证人与实测记录后应用并保存'
        : '未采集到目标设备环境，请刷新设备列表（含 model/display 等属性）后再读取', draft ? '' : 'error');
}

function aoaFirmwareDeclarationApply() {
    if (aoaPanelState.running) return;
    try {
        const text = aoaEl('aoa-firmware-declaration').value.trim();
        if (new TextEncoder().encode(text).length > 8192) throw new Error('固件声明不能超过 8 KiB');
        const declaration = JSON.parse(text);
        if (declaration.version !== 1 || !/^[a-f0-9]{64}$/.test(declaration.sha256 || '')
                || !declaration.context || typeof declaration.context !== 'object' || Array.isArray(declaration.context)
                || !['validated_by', 'validated_at', 'evidence'].every(key => typeof declaration[key] === 'string' && declaration[key].trim())) {
            throw new Error('请填写版本 1、真实固件 SHA-256、刷后环境、验证人、带时区时间及实测记录');
        }
        aoaPanelState.profileMetadata = {...aoaHidMetadata(), firmware_compatibility: declaration};
        aoaSetStatus('固件声明已应用到当前编辑内容，请保存配置；服务端会完整核对环境和标定');
    } catch (error) { aoaSetStatus(`固件声明无效：${error.message}`, 'error'); }
}
