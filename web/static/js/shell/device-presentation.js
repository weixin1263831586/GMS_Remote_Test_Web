/* Inventory display only; action targeting and permission checks stay in the controller. */
window.GmsDevicePresentation = (() => {
    const {escape: text, icon, badge} = GmsUi;
    const stack = (...lines) => `<div class="ui-stack">${lines.join('')}</div>`;
    const name = value => `<span class="ui-name" title="${text(value)}">${text(value)}</span>`;
    const meta = (value, extra = '') => `<span class="ui-meta ${extra}" title="${text(value)}">${text(value)}</span>`;

    function connectionType(device) {
        if (device.source_type === 'adb_proxy') return 'adb_proxy';
        if (device.source_type === 'usbip' || device.transport === 'usbip' || device.is_usbip) return 'usbip';
        if (['local', 'cluster'].includes(device.source_type)) {
            const prefix = `${device.worker_id || ''}:`;
            let id = String(device.device_id || '');
            if (device.worker_id && id.startsWith(prefix)) id = id.slice(prefix.length);
            return /^(?:(?:\d{1,3}\.){3}\d{1,3}|\[[\da-f:]+\]):\d+$/i.test(id) ? 'network' : 'usb';
        }
        return 'unknown';
    }

    function connection(device) {
        const kinds = {
            adb_proxy: ['accent', 'network', 'ADB Proxy'], usbip: ['info', 'plug', 'USB/IP'],
            network: ['info', 'wifi', '网络 ADB'], usb: ['neutral', 'plug', 'USB 直连'],
            unknown: ['neutral', 'info', '未知连接'],
        };
        return badge(...kinds[connectionType(device)]);
    }

    function hostLabel(device) {
        const display = device.host_display_name;
        return display && display !== device.worker_id
            ? display : device.source_host || display || device.worker_id || '主机未上报';
    }

    function hostDescription(device) {
        return [...new Set([device.host_display_name, device.worker_id, device.source_host].filter(Boolean))].join('\n');
    }

    function host(device) {
        return `<span class="ui-name inventory-host" title="${text(hostDescription(device))}">${text(hostLabel(device))}</span>`;
    }

    function runtimeState(device) {
        const states = [device.protocol, device.cluster_state, device.status];
        if (states.includes('fastboot')) return 'fastboot';
        if (states.some(value => ['loader', 'rockusb'].includes(value))) return 'loader';
        for (const value of ['usb_only', 'unresponsive', 'unauthorized', 'offline']) {
            if (states.includes(value)) return value;
        }
        return device.status === 'online' || device.cluster_state === 'available' ? 'online' : 'unknown';
    }

    function runtime(device) {
        const states = {
            fastboot: ['info', 'bolt', 'Fastboot'], loader: ['info', 'chip', 'Loader'],
            usb_only: ['warning', 'plug', 'ADB 未连接'], unresponsive: ['danger', 'error', 'ADB 无响应'],
            unauthorized: ['warning', 'lock', 'ADB 未授权'], offline: ['neutral', 'close', '离线'],
            online: ['success', 'check', 'ADB 在线'], unknown: ['neutral', 'info', '连接未知'],
        };
        const value = runtimeState(device);
        return badge(...states[value]);
    }

    function occupancyState(device) {
        if (device.cluster_state === 'external_busy') return 'external';
        if (device.cluster_state === 'reserved') return 'reserved';
        return device.locked_by || device.claimed || device.cluster_state === 'allocated' ? 'occupied' : 'free';
    }

    function occupancy(device) {
        const states = {
            external: ['danger', 'lock', '外部占用'], reserved: ['accent', 'clock', '已预留'],
            occupied: ['warning', 'lock', '已占用'], free: ['neutral', 'unlock', '未占用'],
        };
        // 徽章在上、锁定人在下（与"主机 / Worker"列同款 ui-name 字号，
        // 未占用时不渲染空行）。
        const holder = device.locked_by
            ? `<span class="ui-name inventory-host" title="${text(device.locked_by)}">${text(device.locked_by)}</span>`
            : '';
        return stack(badge(...states[occupancyState(device)]), holder);
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

    function systemLabel(device) {
        const version = String(device.android_version || '');
        return version ? (/^Android\b/i.test(version) ? version : `Android ${version}`) : '系统未上报';
    }

    function cells(device, groups = []) {
        const serial = device.serial_no || device.device_id || '未上报';
        const label = device.name && device.name !== device.model ? device.name : serial;
        const identity = `<div class="inventory-device">${icon('device')}<div class="ui-stack">`
            + (label === serial ? `<span class="ui-name device-serial ui-mono" title="${text(serial)}">${text(serial)}</span>`
                : name(label) + meta(serial, 'device-serial ui-mono')) + '</div></div>';
        return [
            identity, name(device.model || '未上报'), meta(groups.length ? groups.map(group => group.name).join(' / ') : '未分组'),
            name(systemLabel(device)), meta(device.soc_model || '—', 'ui-mono'),
            connection(device), runtime(device), host(device), occupancy(device), battery(device),
        ].map((content, index) => `<td data-device-field="${['identity', 'model', 'group', 'system', 'soc', 'connection', 'runtime', 'host', 'occupancy', 'battery'][index]}">${content}</td>`).join('');
    }
    return Object.freeze({cells, connectionType, runtimeState, occupancyState, systemLabel, hostLabel, hostDescription});
})();
