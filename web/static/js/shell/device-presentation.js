/* Inventory display only; action targeting and permission checks stay in the controller. */
window.GmsDevicePresentation = (() => {
    const {escape: text, icon, badge} = GmsUi;
    const stack = (...lines) => `<div class="ui-stack">${lines.join('')}</div>`;
    const name = value => `<span class="ui-name">${text(value)}</span>`;
    const meta = (value, extra = '') => `<span class="ui-meta ${extra}">${text(value)}</span>`;

    function connection(device) {
        let kind = ['neutral', 'info', '未知连接'];
        if (device.source_type === 'adb_proxy') kind = ['accent', 'network', 'ADB Proxy'];
        else if (device.source_type === 'usbip' || device.transport === 'usbip' || device.is_usbip) {
            kind = ['info', 'plug', 'USB/IP'];
        } else if (['local', 'cluster'].includes(device.source_type)) {
            kind = /^(?:\d{1,3}\.){3}\d{1,3}:\d+$/.test(String(device.device_id || ''))
                ? ['info', 'wifi', '网络 ADB'] : ['neutral', 'plug', 'USB 直连'];
        }
        const host = device.host_display_name || device.worker_id || device.source_host || '主机未上报';
        return stack(name(host), badge(...kind), device.source_host && device.source_host !== host
            ? meta(device.source_host, 'ui-mono') : '');
    }

    function runtime(device) {
        const states = [device.protocol, device.cluster_state, device.status];
        if (states.includes('fastboot')) return stack(badge('info', 'bolt', 'Fastboot'), meta('启动模式'));
        if (states.some(value => ['loader', 'rockusb'].includes(value))) {
            return stack(badge('info', 'chip', 'Loader'), meta('启动模式'));
        }
        if (states.includes('usb_only')) return stack(badge('warning', 'plug', 'USB / AOA'), meta('ADB 未连接'));
        if (states.includes('unresponsive')) return badge('danger', 'error', 'ADB 无响应');
        if (states.includes('unauthorized')) return badge('warning', 'lock', 'ADB 未授权');
        if (states.includes('offline')) return badge('neutral', 'close', '离线');
        if (device.status === 'online' || device.cluster_state === 'available') {
            return badge('success', 'check', 'ADB 在线');
        }
        return badge('neutral', 'info', '连接未知');
    }

    function occupancy(device) {
        let state = ['neutral', 'unlock', '未占用'];
        if (device.cluster_state === 'external_busy') state = ['danger', 'lock', '外部占用'];
        else if (device.cluster_state === 'reserved') state = ['accent', 'clock', '已预留'];
        else if (device.locked_by || device.claimed || device.cluster_state === 'allocated') {
            state = ['warning', 'lock', '已占用'];
        }
        return stack(badge(...state), device.locked_by ? meta(device.locked_by) : '');
    }

    function battery(device) {
        const raw = device.battery_level;
        const level = Number(raw);
        if (raw === null || raw === undefined || raw === '' || !Number.isFinite(level) || level < 0 || level > 100) {
            return meta('未上报');
        }
        const tone = level < 20 ? 'low' : level < 50 ? 'medium' : 'high';
        return `<span class="device-battery is-${tone}">${icon('battery')}<span>${text(level)}%</span></span>`;
    }

    function cells(device) {
        const serial = device.serial_no || device.device_id || '未上报';
        const version = String(device.android_version || '');
        const system = version ? (/^Android\b/i.test(version) ? version : `Android ${version}`) : '系统未上报';
        return [
            stack(name(device.name || device.model || 'Android 设备'), meta(serial, 'ui-mono device-serial')),
            connection(device),
            stack(name(system), device.soc_model ? meta(device.soc_model, 'ui-mono') : ''),
            runtime(device), occupancy(device), battery(device),
        ].map((content, index) => `<td data-device-field="${['identity', 'connection', 'system', 'runtime', 'occupancy', 'battery'][index]}">${content}</td>`).join('');
    }
    return Object.freeze({cells});
})();
