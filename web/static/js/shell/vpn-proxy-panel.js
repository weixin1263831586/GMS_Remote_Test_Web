// vpn-proxy management: read-only system status/stats + elevated admin control.
// Uses ModalManager and apiCall; never talks to the privileged stats port from
// the browser and never executes a client installer automatically.
let vpnProxyPollTimer = null;
let vpnProxyBusy = false;
let vpnProxyRequestGeneration = 0;
let vpnProxyInstallCommands = {linux: '', windows: ''};

function vpnProxyText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
}

function vpnProxyServiceLabel(value) {
    return ({
        active: '运行中', inactive: '已停止', failed: '运行失败',
        activating: '启动中', deactivating: '停止中',
        unknown: '状态未知', unavailable: '不可用'
    })[value] || '状态未知';
}

function vpnProxyFillRows(id, items, fields, emptyMessage) {
    const node = document.getElementById(id);
    if (!node) return;
    node.replaceChildren();
    if (!items || !items.length) {
        const row = node.insertRow();
        const cell = row.insertCell();
        cell.colSpan = fields.length;
        cell.textContent = emptyMessage;
        return;
    }
    for (const item of items.slice(0, 100)) {
        const row = node.insertRow();
        for (const field of fields) {
            row.insertCell().textContent = field(item);
        }
    }
}

function vpnProxyFormatBytes(bytes) {
    let size = Number(bytes) || 0;
    const units = ['B', 'KiB', 'MiB', 'GiB'];
    let idx = 0;
    while (size >= 1024 && idx < units.length - 1) { size /= 1024; idx++; }
    return (idx ? size.toFixed(1) : String(size)) + ' ' + units[idx];
}

function vpnProxyRenderClientAccess(connected, access, emptyMessage) {
    const disabled = new Set(Array.isArray(access?.disabled) ? access.disabled : []);
    const items = new Map();
    // Keep current socket statistics separate from durable authorization state.
    // A disconnected, disabled IP must remain visible so it can be re-enabled.
    for (const item of connected || []) {
        if (item && typeof item.client === 'string') items.set(item.client, item);
    }
    for (const client of disabled) {
        if (!items.has(client)) items.set(client, {client, connections: null, network: '—'});
    }
    const tbody = document.getElementById('vpn-proxy-connected');
    if (!tbody) return;
    tbody.replaceChildren();
    if (items.size === 0) {
        const cell = tbody.insertRow().insertCell();
        cell.colSpan = 5;
        cell.textContent = emptyMessage;
        return;
    }
    for (const [client, item] of items) {
        const blocked = disabled.has(client);
        const row = tbody.insertRow();
        row.insertCell().textContent = client;
        row.insertCell().textContent = connected === null || item.connections == null
            ? '—' : String(item.connections);
        row.insertCell().textContent = item.network || '—';
        const state = row.insertCell();
        state.className = 'vpn-proxy-client-state ' + (blocked ? 'is-disabled' : 'is-enabled');
        state.textContent = blocked ? '已禁用' : '允许访问';
        const action = row.insertCell();
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'vpn-proxy-client-toggle';
        button.textContent = blocked ? '启用' : '禁用';
        button.disabled = vpnProxyBusy || access?.available !== true;
        button.title = access?.available ? '管理此源 IP 的新代理请求；不保证中断已有长连接'
            : (access?.error || '需部署支持访问控制的 vpn-proxy 服务');
        button.addEventListener('click', () => vpnProxyToggleClientAccess(client, blocked ? 'enable' : 'disable'));
        action.appendChild(button);
    }
}

function vpnProxyRender(data) {
    const services = data.services || {};
    vpnProxyText('vpn-proxy-status', vpnProxyServiceLabel(services.proxy));
    vpnProxyText('vpn-proxy-firewall', vpnProxyServiceLabel(services.firewall));
    vpnProxyText('vpn-proxy-stats', vpnProxyServiceLabel(services.stats));
    vpnProxyText('vpn-proxy-installer', vpnProxyServiceLabel(services.installer));

    const config = data.config || {};
    vpnProxyText('vpn-proxy-endpoint',
        config.proxy_url ? '代理地址：' + config.proxy_url : '当前未检测到已部署的代理配置');
    const sources = data.clients?.sources || {};
    // A failed ss/access-log read is not "zero connected clients".
    // client-stats.sh returns explicit per-source health in the JSON snapshot.
    const connected = sources.connections === true && Array.isArray(data.clients?.connected)
        ? data.clients.connected : null;
    const recent = sources.access_log === true && Array.isArray(data.clients?.recent)
        ? data.clients.recent : null;
    const statsError = data.stats_error || null;
    const unavailable = '统计不可用（请检查 rk-vpn-proxy-stats.service）';
    const access = data.client_access || {};
    vpnProxyRenderClientAccess(connected, access,
        statsError || (sources.connections === false ? unavailable : '当前无 TCP 连接'));
    vpnProxyFillRows('vpn-proxy-recent', recent,
        [x => x.label || x.client || '-', x => String(x.requests || 0),
         x => vpnProxyFormatBytes(x.bytes), x => String(x.errors || 0),
         x => x.last_seen ? new Date(x.last_seen * 1000).toLocaleTimeString() : '-'],
        statsError || (sources.access_log === false ? unavailable : '最近时间窗内没有访问记录'));
    vpnProxyText('vpn-proxy-connected-note', (connected
        ? '当前 ' + connected.length + ' 个客户端' : unavailable) +
        ' · 已禁用 ' + (Array.isArray(access.disabled) ? access.disabled.length : 0) + ' 个 IP' +
        (access.available ? '' : ' · 访问控制待部署'));
    vpnProxyText('vpn-proxy-recent-note', recent
        ? '最近 ' + (data.clients?.window_seconds || 300) + ' 秒，共 ' + recent.length + ' 个客户端' : unavailable);
    vpnProxyText('vpn-proxy-allowlist', '已授权网段：' +
        ((config.allowed_clients || []).join('、') || '未配置'));
    vpnProxyInstallCommands = {
        linux: data.install?.linux || '',
        windows: data.install?.windows || '',
    };
    // Backend returns one executable command per platform. The input and
    // copied value must match: manual selection remains safe and usable.
    for (const platform of ['linux', 'windows']) {
        const node = document.getElementById('vpn-proxy-' + platform + '-command');
        if (!node) continue;
        const command = vpnProxyInstallCommands[platform];
        node.value = command;
        node.title = command ? '一行完整安装命令；点击复制后在对应终端执行' : '安装服务不可用';
    }
    const start = document.getElementById('vpn-proxy-start');
    const stop = document.getElementById('vpn-proxy-stop');
    if (start) start.disabled = vpnProxyBusy || !data.installed
        || !['inactive', 'failed'].includes(services.proxy);
    if (stop) stop.disabled = vpnProxyBusy || services.proxy !== 'active';
}

async function refreshVpnProxyPanel() {
    const generation = ++vpnProxyRequestGeneration;
    try {
        const payload = await apiCall('/api/tools/vpn-proxy/status');
        if (generation !== vpnProxyRequestGeneration || !ModalManager.isOpen('vpn-proxy-modal')) return;
        if (!payload.success) throw new Error(payload.error || '读取失败');
        vpnProxyText('vpn-proxy-error', '');
        vpnProxyRender(payload.data || {});
    } catch (error) {
        if (generation !== vpnProxyRequestGeneration || !ModalManager.isOpen('vpn-proxy-modal')) return;
        // Clear the previous successful snapshot: stale service/client data
        // must never remain presented as live data after a poll error.
        vpnProxyText('vpn-proxy-error', '状态读取失败：' + error.message);
        for (const id of ['vpn-proxy-status', 'vpn-proxy-firewall', 'vpn-proxy-stats',
                          'vpn-proxy-installer']) vpnProxyText(id, '状态不可用');
        vpnProxyText('vpn-proxy-endpoint', '无法确认当前代理服务状态');
        vpnProxyText('vpn-proxy-connected-note', '数据不可用');
        vpnProxyText('vpn-proxy-recent-note', '数据不可用');
        vpnProxyFillRows('vpn-proxy-connected', null,
            [x => x.client, x => x.connections, x => x.network, x => x.status,
             x => x.action], '统计数据不可用');
        vpnProxyFillRows('vpn-proxy-recent', null,
            [x => x.client, x => x.requests, x => x.bytes,
             x => x.errors, x => x.last_seen], '统计数据不可用');
        vpnProxyInstallCommands = {linux: '', windows: ''};
        document.getElementById('vpn-proxy-linux-command').value = '';
        document.getElementById('vpn-proxy-windows-command').value = '';
        document.getElementById('vpn-proxy-start').disabled = true;
        document.getElementById('vpn-proxy-stop').disabled = true;
    }
}

function openVpnProxyPanel() {
    ModalManager.open('vpn-proxy-modal');
    ModalManager.onClose('vpn-proxy-modal', () => {
        vpnProxyRequestGeneration++;
        if (vpnProxyPollTimer !== null) {
            clearInterval(vpnProxyPollTimer);
            vpnProxyPollTimer = null;
        }
    });
    refreshVpnProxyPanel();
    if (vpnProxyPollTimer === null) vpnProxyPollTimer = setInterval(() => {
        if (ModalManager.isOpen('vpn-proxy-modal') && !vpnProxyBusy) refreshVpnProxyPanel();
    }, 5000);
}

function closeVpnProxyPanel() { ModalManager.close('vpn-proxy-modal'); }

async function vpnProxyControl(action) {
    if (vpnProxyBusy) return;
    const verb = action === 'start' ? '启动' : '停止';
    const confirmed = await showConfirmDialog('vpn-proxy 服务控制',
        '确定' + verb + '代理服务吗？' + (action === 'stop' ? '已连接的客户端代理请求将中断。' : ''));
    if (!confirmed) return;
    vpnProxyBusy = true;
    document.getElementById('vpn-proxy-start').disabled = true;
    document.getElementById('vpn-proxy-stop').disabled = true;
    let operationError = '';
    try {
        const response = await apiCall('/api/tools/vpn-proxy/control', 'POST', {action});
        if (!response.success) throw new Error(response.error || '操作失败');
        showToast('vpn-proxy 已' + verb, 'success');
    } catch (error) {
        operationError = verb + '失败：' + error.message;
        showToast('vpn-proxy ' + verb + '失败', 'error');
    } finally {
        vpnProxyBusy = false;
        await refreshVpnProxyPanel();
        if (operationError) vpnProxyText('vpn-proxy-error', operationError);
    }
}

async function vpnProxyToggleClientAccess(client, action) {
    if (vpnProxyBusy || !['enable', 'disable'].includes(action)) return;
    const label = action === 'disable' ? '禁用' : '启用';
    const confirmed = await showConfirmDialog('vpn-proxy 客户端访问控制',
        '确定' + label + '客户端 ' + client + ' 的代理访问吗？'
        + (action === 'disable' ? '将阻止新请求；已有长连接可能继续保持。' : ''));
    if (!confirmed) return;
    vpnProxyBusy = true;
    let errorMessage = '';
    try {
        // Re-render immediately so concurrent clicks cannot race this update.
        document.querySelectorAll('#vpn-proxy-connected .vpn-proxy-client-toggle')
            .forEach(button => { button.disabled = true; });
        const response = await apiCall('/api/tools/vpn-proxy/client-control', 'POST',
            {client, action});
        if (!response.success) throw new Error(response.error || '操作失败');
        showToast('客户端 ' + client + ' 已' + label, 'success');
    } catch (error) {
        errorMessage = label + '客户端失败：' + error.message;
        showToast(errorMessage, 'error');
    } finally {
        vpnProxyBusy = false;
        await refreshVpnProxyPanel();
        if (errorMessage) vpnProxyText('vpn-proxy-error', errorMessage);
    }
}

function startVpnProxyService() { return vpnProxyControl('start'); }
function stopVpnProxyService() { return vpnProxyControl('stop'); }

async function vpnProxyCopyCommand(id) {
    const platform = id === 'vpn-proxy-linux-command' ? 'linux' : 'windows';
    const value = vpnProxyInstallCommands[platform] || '';
    if (!value) { showToast('安装服务未启用或代理配置不可用', 'warning'); return; }
    try {
        await navigator.clipboard.writeText(value);
        showToast('已复制客户端安装命令', 'success');
    } catch (_) {
        showToast('复制失败，请选中文本手动复制', 'error');
    }
}
function copyVpnProxyLinux() { return vpnProxyCopyCommand('vpn-proxy-linux-command'); }
function copyVpnProxyWindows() { return vpnProxyCopyCommand('vpn-proxy-windows-command'); }
