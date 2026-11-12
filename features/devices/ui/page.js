(() => {
    'use strict';

    const WORKSPACE_STORAGE_KEY = 'gms_serial_console_workspace_v1';

    // 每个控制台 tab 对应一个 session（终端、WebSocket、输入、历史日志各自独立），
    // 同一串口同一时刻只保留一个 session，重复“打开控制台”只会切换到已有 tab。
    const state = {
        ports: [],
        editingKey: '',
        sessions: [],
        sessionCounter: 0,
        activeKey: '',
        portsRequestGeneration: 0,
        portsRequestsInFlight: 0,
        refreshTimer: null,
        noticeTimer: null,
        activeView: 'ports',
        canManageDevices: true,
        modalReturnFocus: null,
    };

    const $ = id => document.getElementById(id);

    async function api(path, options = {}) {
        const request = {...options, headers: {...(options.headers || {})}};
        if (request.body && typeof request.body !== 'string') {
            request.headers['Content-Type'] = 'application/json';
            request.body = JSON.stringify(request.body);
        }
        const response = await fetch(path, request);
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.success === false) {
            const detail = payload.error || payload.detail?.message || payload.detail || response.statusText;
            throw new Error(friendlyError(typeof detail === 'string' ? detail : JSON.stringify(detail)));
        }
        return payload.data || payload;
    }

    function friendlyError(message) {
        if (/permission denied/i.test(message)) {
            return '权限不足：此操作需要 devices.inventory 权限（device_operator 或 admin 角色，或先完成管理员二次验证）';
        }
        return message;
    }

    function notice(message, type = '') {
        const node = $('page-notice');
        node.textContent = message;
        node.className = `notice ${type}`;
        node.hidden = false;
        clearTimeout(state.noticeTimer);
        state.noticeTimer = setTimeout(() => { node.hidden = true; }, 4500);
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function addMeta(container, label, value) {
        container.append(element('span', '', label), element('span', 'mono', value || '—'));
    }

    function addIdentityMeta(container, port) {
        // 稳定标识 + 可靠性合并为一行：徽标区分 by-id/usb-path（稳定）与
        // 裸 tty 名（临时节点），tooltip 保留完整提示，端口卡矮一行。
        const unstable = port.identity_stable === false;
        const value = element('span', 'mono', port.by_id || port.port_key);
        const badge = element('span', `identity-badge${unstable ? ' unstable' : ''}`,
            unstable ? '临时节点' : '稳定');
        badge.title = unstable
            ? '只有易变的 tty 名称，重插后可能指向不同硬件，不能持久绑定'
            : 'by-id / usb-path 稳定标识，重插后绑定保持';
        value.append(badge);
        container.append(element('span', '', '稳定标识'), value);
    }

    function button(text, action, className = '') {
        const node = element('button', className, text);
        const name = {'打开控制台': 'terminal', '切换控制台': 'terminal', '编辑绑定': 'edit',
            '绑定': 'link', '启动采集': 'play', '停止采集': 'stop'}[text];
        if (name) node.innerHTML = GmsUi.icon(name) + GmsUi.escape(text);
        node.type = 'button';
        node.addEventListener('click', action);
        return node;
    }

    function portTitle(port) {
        return port.binding?.label || port.devname || port.port_key;
    }

    function friendlySerialError(value) {
        const text = String(value || '');
        if (/Errno 5|Input\/output error|protocol error/i.test(text)) {
            return 'USB 串口通信异常：请重新插拔 FTDI 或更换 USB 端口，系统将自动重连';
        }
        return text;
    }

    function sessionByKey(portKey) {
        return state.sessions.find(session => session.portKey === portKey) || null;
    }

    function readWorkspace() {
        try {
            const value = JSON.parse(sessionStorage.getItem(WORKSPACE_STORAGE_KEY) || '{}');
            const keys = Array.isArray(value.portKeys)
                ? value.portKeys.filter(key => typeof key === 'string' && key).slice(0, 12)
                : [];
            return {
                portKeys: [...new Set(keys)],
                activeView: value.activeView === 'console' ? 'console' : 'ports',
                activeKey: typeof value.activeKey === 'string' ? value.activeKey : '',
            };
        } catch {
            return {portKeys: [], activeView: 'ports', activeKey: ''};
        }
    }

    function persistWorkspace() {
        try {
            sessionStorage.setItem(WORKSPACE_STORAGE_KEY, JSON.stringify({
                portKeys: state.sessions.map(session => session.portKey),
                activeView: state.activeView,
                activeKey: state.activeKey,
            }));
        } catch {
            // sessionStorage may be unavailable in an opaque embedded context.
        }
    }

    function computeManagePermission(status) {
        // 服务端才是安全边界（无权限请求仍会被 403）；这里只决定点击写操作
        // 按钮时是否放行。按钮始终渲染：普通 user 点击时给出明确的权限
        // 提示（requireManagePermission），既不隐藏按钮也不裸抛 403。
        if (!status?.auth_required) return true;
        const user = status.user;
        if (!user) return false;
        if (status.elevated) return true;
        const permissions = Array.isArray(user.permissions) ? user.permissions : [];
        return permissions.includes('*') || permissions.includes('devices.inventory');
    }

    function requireManagePermission() {
        if (state.canManageDevices) return true;
        notice('当前账号无设备管理权限：串口绑定与采集需要 device_operator 或管理员角色（devices.inventory），请联系管理员调整角色', 'error');
        return false;
    }

    async function loadAuthStatus() {
        try {
            const status = await api('/api/auth/status');
            state.canManageDevices = computeManagePermission(status);
        } catch {
            // 状态读取失败时保持完整视图：可见性只是体验优化，
            // 越权操作由服务端 403 兜底。
        }
    }

    function renderPorts() {
        const list = $('ports-list');
        list.replaceChildren();
        if (!state.ports.length) {
            list.append(element('div', 'empty', '未发现 ttyUSB/ttyACM 串口。插入 USB 转串口线后点击刷新。'));
            return;
        }
        state.ports.forEach(port => {
            const session = sessionByKey(port.port_key);
            const card = element('article', `port-card${session ? ' selected' : ''}`);
            const head = element('div', 'port-card-head');
            head.append(element('div', 'port-title', portTitle(port)));
            head.append(element('span', `port-state ${port.online ? 'online' : ''}`, port.online ? '在线' : '离线'));
            card.append(head);

            const meta = element('div', 'port-meta');
            // 硬件身份行按实际有值展示：离线绑定卡（list_ports 无设备
            // 节点、USB 字段为空）不再渲染整排 "—" 占位。
            if (port.devname) addMeta(meta, '设备节点', port.devname);
            addIdentityMeta(meta, port);
            const chipLine = [port.vendor_product, port.usb_vendor_name, port.usb_model_name, port.driver].filter(Boolean).join(' · ');
            if (chipLine) addMeta(meta, 'USB 芯片/驱动', chipLine);
            if (port.usb_serial) addMeta(meta, 'USB 序列号', port.usb_serial);
            if (port.usb_bus_node) addMeta(meta, 'USB 总线节点', port.usb_bus_node);
            addMeta(meta, '绑定设备', port.binding
                ? (port.binding.device_id
                    || (port.binding.note ? '未关联（见备注）' : '未关联（可补填）'))
                : '未绑定');
            addMeta(meta, '波特率', port.binding ? String(port.binding.baudrate) : '未绑定');
            addMeta(meta, '常驻采集', port.capture_enabled ? (port.capture_active ? '采集中' : '等待端口') : '关闭');
            addMeta(meta, '最近输出', port.last_output_at ? new Date(port.last_output_at).toLocaleString() : '—');
            if (port.binding?.note) addMeta(meta, '备注', port.binding.note);
            card.append(meta);
            if (port.error) card.append(element('div', 'port-error', friendlySerialError(port.error)));

            const actions = element('div', 'port-actions');
            actions.append(button(port.binding ? '编辑绑定' : '绑定', () => {
                if (!requireManagePermission()) return;
                openBinding(port);
            }));
            const captureButton = () => button(
                port.capture_enabled ? '停止采集' : '启动采集',
                () => {
                    if (!requireManagePermission()) return;
                    toggleCapture(port);
                },
                port.capture_enabled ? 'capture-on' : ''
            );
            if (session) {
                actions.append(button('切换控制台', () => switchView('console', session.portKey), 'primary'));
                actions.append(captureButton());
            } else if (port.binding) {
                actions.append(button('打开控制台', () => openConsole(port), 'primary'));
                actions.append(captureButton());
            }
            card.append(actions);
            list.append(card);
        });
    }

    function switchView(view, portKey = '') {
        const target = view === 'console' ? sessionByKey(portKey) : null;
        if (view === 'console' && !target) return;
        state.activeView = view;
        state.activeKey = target ? target.portKey : '';
        const isPorts = view === 'ports';
        $('ports-section').hidden = !isPorts;
        $('console-section').hidden = isPorts;
        $('ports-tab').classList.toggle('active', isPorts);
        $('ports-tab').setAttribute('aria-selected', String(isPorts));
        $('ports-tab').tabIndex = isPorts ? 0 : -1;
        state.sessions.forEach(session => {
            const active = session === target;
            session.tab.classList.toggle('active', active);
            session.tab.setAttribute('aria-selected', String(active));
            session.tab.tabIndex = active ? 0 : -1;
            session.pane.hidden = !active;
        });
        syncConsoleToolbar();
        renderPorts();
        if (target) {
            requestAnimationFrame(() => {
                target.fitAddon?.fit();
                target.terminal?.scrollToBottom();
            });
        }
        persistWorkspace();
    }

    async function loadPorts(silent = false) {
        if (silent && state.portsRequestsInFlight > 0) return;
        const generation = ++state.portsRequestGeneration;
        state.portsRequestsInFlight += 1;
        if (!silent) $('refresh-status').textContent = '扫描中…';
        try {
            const data = await api('/api/devices/console/ports');
            if (generation !== state.portsRequestGeneration) return;
            state.ports = data.ports || [];
            renderPorts();
            syncSessionHeadings();
            $('refresh-status').textContent = `串口更新于 ${new Date().toLocaleTimeString()}`;
        } catch (error) {
            if (generation !== state.portsRequestGeneration) return;
            $('refresh-status').textContent = '刷新失败';
            if (!silent) notice(error.message, 'error');
        } finally {
            state.portsRequestsInFlight = Math.max(0, state.portsRequestsInFlight - 1);
        }
    }

    function syncPortAutoRefresh(visible) {
        if (state.refreshTimer) {
            clearInterval(state.refreshTimer);
            state.refreshTimer = null;
        }
        if (!visible) return;
        state.refreshTimer = setInterval(() => void loadPorts(true), 3000);
    }

    async function loadBindingDeviceOptions(selectedDevice, editingKey) {
        const select = $('binding-label');
        select.disabled = true;
        select.replaceChildren(new Option('正在加载设备…', ''));
        try {
            const data = await api('/api/devices/management');
            if (state.editingKey !== editingKey) return;
            const devices = data.source === 'local' && Array.isArray(data.devices) ? data.devices : [];
            const options = new Map();
            devices.forEach(device => {
                const value = String(device.device_id || device.serial_no || '').trim();
                if (!value || options.has(value)) return;
                const model = String(device.model || '').trim();
                options.set(value, {
                    text: model && model !== value ? `${value} · ${model}` : value,
                    workerId: String(device.worker_id || '').trim(),
                });
            });
            select.replaceChildren(new Option(
                options.size ? '不关联设备（无法开机/未枚举的板卡）' : '未发现本机设备，可直接保存绑定',
                '',
            ));
            options.forEach((item, value) => {
                const option = new Option(item.text, value);
                option.dataset.workerId = item.workerId;
                select.add(option);
            });
            if (selectedDevice && !options.has(selectedDevice)) {
                select.add(new Option(`${selectedDevice} · 当前绑定`, selectedDevice));
            }
            select.value = selectedDevice || '';
            if (data.source && data.source !== 'local') {
                notice('当前设备清单来自远端主机，不能与 Controller 本机串口绑定', 'error');
            }
        } catch (error) {
            if (state.editingKey !== editingKey) return;
            select.replaceChildren(new Option('设备列表加载失败，可不关联设备直接保存', ''));
            if (selectedDevice) select.add(new Option(`${selectedDevice} · 当前绑定`, selectedDevice));
            select.value = selectedDevice || '';
            notice(`设备列表加载失败：${error.message}`, 'error');
        } finally {
            if (state.editingKey === editingKey) select.disabled = false;
        }
    }

    function openBinding(port) {
        state.modalReturnFocus = document.activeElement;
        state.editingKey = port.port_key;
        const binding = port.binding || {};
        $('binding-port').textContent = `${port.devname || '离线端口'} · ${port.port_key}`;
        loadBindingDeviceOptions(binding.device_id || binding.label || '', port.port_key);
        $('binding-note').value = binding.note || '';
        $('binding-baudrate').value = binding.baudrate || 1500000;
        $('binding-newline').value = binding.newline || 'cr';
        $('binding-capture').checked = Boolean(binding.capture_enabled);
        $('delete-binding').hidden = !port.binding;
        $('binding-modal').hidden = false;
        $('binding-label').focus();
    }

    function closeBinding() {
        $('binding-modal').hidden = true;
        state.editingKey = '';
        state.modalReturnFocus?.focus?.();
        state.modalReturnFocus = null;
    }

    async function saveBinding(event) {
        event.preventDefault();
        const key = state.editingKey;
        if (!key) return;
        try {
            await api(`/api/devices/console/bindings/${encodeURIComponent(key)}`, {
                method: 'PUT',
                body: {
                    label: $('binding-label').value,
                    device_id: $('binding-label').value,
                    worker_id: $('binding-label').selectedOptions[0]?.dataset.workerId || '',
                    note: $('binding-note').value,
                    baudrate: Number($('binding-baudrate').value),
                    newline: $('binding-newline').value,
                    capture_enabled: $('binding-capture').checked,
                },
            });
            closeBinding();
            notice('串口绑定已保存', 'success');
            await loadPorts(true);
        } catch (error) {
            notice(error.message, 'error');
        }
    }

    async function deleteBinding() {
        const key = state.editingKey;
        if (!key || !window.confirm('删除此串口绑定？历史日志不会自动删除。')) return;
        try {
            await api(`/api/devices/console/bindings/${encodeURIComponent(key)}`, {method: 'DELETE'});
            closeBinding();
            const session = sessionByKey(key);
            if (session) closeConsole(key);
            notice('串口绑定已删除', 'success');
            await loadPorts(true);
        } catch (error) {
            notice(error.message, 'error');
        }
    }

    async function toggleCapture(port) {
        const operation = port.capture_enabled ? 'stop' : 'start';
        try {
            await api(`/api/devices/console/ports/${encodeURIComponent(port.port_key)}/capture/${operation}`, {method: 'POST'});
            notice(port.capture_enabled ? '常驻采集已停止' : '常驻采集已启动', 'success');
            await loadPorts(true);
        } catch (error) {
            notice(error.message, 'error');
        }
    }

    function syncSessionHeading(session, port) {
        session.title = portTitle(port);
        // 标题/副标题行已删（信息在右侧栏设备信息里），只更新 Tab 标题
        // 与 header 操作区（激活会话）。
        const details = session.pane.querySelector('.console-info-grid');
        details.replaceChildren();
        for (const [label, value] of [
            ['绑定设备', port.binding?.label || port.binding?.device_id || '未关联'],
            ['串口节点', port.devname || '离线'],
            ['波特率', port.binding?.baudrate ?? '未配置'],
            ['回车格式', port.binding?.newline || '未配置'],
            ['芯片 / 驱动', [port.usb_vendor_name, port.usb_model_name, port.driver].filter(Boolean).join(' / ') || '未上报'],
            ['备注', port.binding?.note || '无'],
        ]) {
            details.append(element('dt', '', label), element('dd', '', String(value)));
        }
        const others = session.pane.querySelector('.console-other-list');
        others.replaceChildren();
        state.ports.filter(item => item.port_key !== session.portKey && item.binding).forEach(item => {
            const row = element('div', 'console-other-port');
            row.append(element('span', '', portTitle(item)), button('打开控制台', () => openConsole(item)));
            others.append(row);
        });
        if (!others.childElementCount) others.append(element('span', 'muted', '暂无其他已绑定串口'));
        const tabTitle = session.tab?.querySelector('.tab-title');
        if (tabTitle) tabTitle.textContent = `控制台${session.id}`;
        if (!port.online) setSocketStatus(session, '串口离线', 'offline');
        else if (port.capture_active) setSocketStatus(session, '串口已打开', 'online');
        else if (session.socket?.readyState === WebSocket.OPEN) setSocketStatus(session, '等待串口', 'waiting');
        syncConsoleToolbar();
    }

    // 控制台操作区跟随激活 Tab：激活的是控制台会话时显示，按钮作用于
    // 该会话；激活本机串口列表时隐藏。状态胶囊取当前会话的实时状态。
    function activeConsoleSession() {
        if (state.activeView !== 'console') return null;
        return sessionByKey(state.activeKey);
    }

    function syncConsoleToolbar() {
        const toolbar = $('console-toolbar');
        if (!toolbar) return;
        const session = activeConsoleSession();
        toolbar.hidden = !session;
        // 刷新端口/更新时间是本机串口列表专属，控制台激活时让位给操作区。
        const portsActions = document.querySelector('.toolbar-actions');
        if (portsActions) portsActions.hidden = Boolean(session);
        if (!session) return;
        const pill = session.pane.querySelector('.socket-status');
        const mirror = $('console-socket-status');
        if (pill && mirror) {
            mirror.className = pill.className;
            mirror.innerHTML = pill.innerHTML;
        }
        const pause = $('console-pause');
        if (pause) pause.textContent = session.paused ? '继续' : '暂停';
    }

    function setSocketStatus(session, text, className) {
        const pill = session.pane.querySelector('.socket-status');
        if (pill) {
            pill.innerHTML = GmsUi.icon(className === 'online' ? 'check' : className === 'offline' ? 'close' : 'clock') + GmsUi.escape(text);
            pill.className = `socket-status status-pill ${className}`;
        }
        if (session === activeConsoleSession()) syncConsoleToolbar();
    }

    function syncSessionHeadings() {
        state.sessions.forEach(session => {
            const port = state.ports.find(item => item.port_key === session.portKey);
            if (port) syncSessionHeading(session, port);
        });
    }

    function setWritable(session, writable) {
        const pane = session.pane;
        pane.querySelector('.terminal-input').disabled = !writable;
        pane.querySelector('.send-input').disabled = !writable;
        pane.querySelector('.send-ctrl-c').disabled = !writable;
    }

    function rememberConsoleAction(session, label) {
        const list = session.pane.querySelector('.console-activity');
        list.querySelector('.empty')?.remove();
        const row = element('li');
        row.append(element('time', 'mono', new Date().toLocaleTimeString()), element('span', '', label));
        list.prepend(row);
        while (list.children.length > 4) list.lastElementChild.remove();
    }

    function ensureTerminal(session) {
        if (session.terminal) return true;
        if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') {
            notice('终端渲染组件加载失败，请刷新页面重试', 'error');
            return false;
        }
        const theme = getComputedStyle(document.documentElement);
        const terminal = new Terminal({
            convertEol: true,
            cursorBlink: false,
            disableStdin: true,
            // DejaVu/Liberation 置前：Linux 客户端 fontconfig 会把 Consolas
            // 别名到比例宽度字体，xterm 量出的单元格宽度与行渲染字形不一致，
            // 导致串口日志字符间距被 letter-spacing 补偿拉开。
            fontFamily: '"DejaVu Sans Mono", "Liberation Mono", Consolas, "Courier New", monospace',
            fontSize: 13,
            scrollback: 10000,
            theme: {
                background: theme.getPropertyValue('--darker-bg').trim(),
                foreground: theme.getPropertyValue('--text-primary').trim(),
                cursor: theme.getPropertyValue('--text-primary').trim(),
            },
        });
        const fitAddon = new FitAddon.FitAddon();
        terminal.loadAddon(fitAddon);
        const output = session.pane.querySelector('.terminal-output');
        terminal.open(output);
        session.terminal = terminal;
        session.fitAddon = fitAddon;
        session.resizeObserver = new ResizeObserver(() => {
            if (state.activeView === 'console' && state.activeKey === session.portKey) fitAddon.fit();
        });
        session.resizeObserver.observe(output);
        fitAddon.fit();
        return true;
    }

    function clearTerminal(session) {
        if (session.terminal) session.terminal.reset();
        else session.pane.querySelector('.terminal-output').textContent = '';
    }

    function terminalText(session) {
        const output = session.pane.querySelector('.terminal-output');
        if (!session.terminal) return output.textContent;
        if (session.terminal.hasSelection()) return session.terminal.getSelection();
        session.terminal.selectAll();
        const text = session.terminal.getSelection();
        session.terminal.clearSelection();
        return text;
    }

    function appendOutput(session, text) {
        if (!text) return;
        if (session.paused) {
            session.pendingOutput += text;
            if (session.pendingOutput.length > 2_000_000) session.pendingOutput = session.pendingOutput.slice(-2_000_000);
            return;
        }
        if (session.terminal) {
            session.terminal.write(text);
            return;
        }
        const output = session.pane.querySelector('.terminal-output');
        output.textContent = `${output.textContent}${text}`.slice(-2_000_000);
        output.scrollTop = output.scrollHeight;
    }

    function connectSocket(session) {
        clearTimeout(session.reconnectTimer);
        session.socketGeneration += 1;
        const generation = session.socketGeneration;
        if (session.socket) session.socket.close();
        setWritable(session, false);
        setSocketStatus(session, '连接中', 'waiting');
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const socket = new WebSocket(`${protocol}//${location.host}/api/devices/console/ws/${encodeURIComponent(session.portKey)}`);
        session.socket = socket;
        socket.onopen = () => {
            if (generation !== session.socketGeneration || session.closed) return;
            setSocketStatus(session, '通道已连接', 'waiting');
            rememberConsoleAction(session, 'WebSocket 通道已连接');
        };
        socket.onmessage = event => {
            if (generation !== session.socketGeneration || session.closed) return;
            let message;
            try { message = JSON.parse(event.data); } catch { return; }
            if (message.type === 'data' || message.type === 'backlog') appendOutput(session, message.data || '');
            if (message.type === 'backlog') setWritable(session, Boolean(message.writable));
            if (message.type === 'backlog') {
                const status = message.port_status || {};
                if (!status.device_online) setSocketStatus(session, '串口离线', 'offline');
                else if (status.capture_active) setSocketStatus(session, '串口已打开', 'online');
            }
            if (message.type === 'error') notice(friendlySerialError(message.error) || '串口操作失败', 'error');
        };
        socket.onerror = () => {
            if (generation !== session.socketGeneration || session.closed) return;
            setWritable(session, false);
            setSocketStatus(session, '连接失败', 'offline');
        };
        socket.onclose = event => {
            if (generation !== session.socketGeneration || session.closed) return;
            session.socket = null;
            setWritable(session, false);
            setSocketStatus(session, event.code === 4404 ? '请先绑定' : '已断开', 'offline');
            if (![4401, 4403, 4404].includes(event.code)) {
                session.reconnectTimer = setTimeout(() => connectSocket(session), 2000);
            }
        };
    }

    function sendInput(session, data, appendNewline) {
        if (!session.socket || session.socket.readyState !== WebSocket.OPEN) {
            setWritable(session, false);
            notice('串口尚未连接', 'error');
            return false;
        }
        try {
            session.socket.send(JSON.stringify({type: 'input', data, append_newline: appendNewline}));
            rememberConsoleAction(session, '已发送输入');
            return true;
        } catch (error) {
            setWritable(session, false);
            notice(`串口发送失败：${error.message}`, 'error');
            return false;
        }
    }

    function sendInputField(session) {
        const input = session.pane.querySelector('.terminal-input');
        if (sendInput(session, input.value, true)) input.value = '';
        input.focus();
    }

    async function loadHistory(session) {
        if (!session || session.closed) return;
        const portKey = session.portKey;
        const generation = ++session.historyGeneration;
        const dateSelect = session.pane.querySelector('.log-date');
        const selectedDate = dateSelect.value;
        const params = new URLSearchParams({tail: session.pane.querySelector('.log-tail').value});
        if (selectedDate) params.set('date', selectedDate);
        try {
            const data = await api(`/api/devices/console/ports/${encodeURIComponent(portKey)}/logs?${params}`);
            if (generation !== session.historyGeneration || session.closed) return;
            session.pane.querySelector('.history-output').textContent =
                data.content || data.capture_status?.hint || '暂无日志';
            const effectiveDate = selectedDate || data.date || '';
            dateSelect.replaceChildren(new Option('最新', ''));
            (data.available_dates || []).forEach(date => dateSelect.add(new Option(date, date)));
            if (selectedDate && (data.available_dates || []).includes(selectedDate)) dateSelect.value = selectedDate;
            dateSelect.dataset.effectiveDate = effectiveDate;
        } catch (error) {
            if (generation !== session.historyGeneration || session.closed) return;
            session.pane.querySelector('.history-output').textContent = `日志读取失败：${error.message}`;
        }
    }

    function downloadLog(session) {
        if (!session || session.closed) return;
        const params = new URLSearchParams();
        const dateSelect = session.pane.querySelector('.log-date');
        const date = dateSelect.value || dateSelect.dataset.effectiveDate;
        if (date) params.set('date', date);
        location.href = `/api/devices/console/ports/${encodeURIComponent(session.portKey)}/logs/download?${params}`;
    }

    async function clearLogs(session) {
        if (!session || session.closed) return;
        if (!window.confirm(`清空控制台${session.id}（${session.title}）的全部历史日志？此操作不可恢复。`)) return;
        try {
            await api(`/api/devices/console/ports/${encodeURIComponent(session.portKey)}/logs`, {method: 'DELETE'});
            notice('串口日志已清空', 'success');
            await loadHistory(session);
        } catch (error) {
            notice(error.message, 'error');
        }
    }

    function renderConsoleTabs() {
        const tablist = $('console-view-tabs');
        tablist.querySelectorAll('.console-tab').forEach(node => node.remove());
        let anchor = $('ports-tab');
        state.sessions.forEach(session => {
            const tab = element('button', 'view-tab console-tab');
            tab.type = 'button';
            tab.setAttribute('role', 'tab');
            tab.dataset.portKey = session.portKey;
            tab.setAttribute('aria-controls', session.pane.id);
            const active = state.activeView === 'console' && state.activeKey === session.portKey;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
            tab.append(element('span', 'tab-title', `控制台${session.id}`));
            const close = element('span', 'tab-close', '×');
            close.title = '关闭控制台';
            close.setAttribute('aria-label', `关闭控制台${session.id}`);
            close.addEventListener('click', event => {
                event.stopPropagation();
                closeConsole(session.portKey);
            });
            tab.append(close);
            tab.addEventListener('click', () => switchView('console', session.portKey));
            anchor.after(tab);
            anchor = tab;
            session.tab = tab;
            session.pane.hidden = !active;
        });
    }

    function buildConsolePane(session, port) {
        const pane = element('section', 'console-session');
        pane.dataset.portKey = session.portKey;
        pane.id = `console-pane-${session.id}`;
        pane.setAttribute('role', 'tabpanel');
        pane.hidden = true;
        pane.innerHTML = `
            <div class="console-layout">
                <div class="terminal-column">
                    <div class="terminal-output terminal" tabindex="0" aria-label="串口输出"></div>
                    <div class="input-dock">
                        <div class="input-row">
                            <input class="terminal-input" type="text" autocomplete="off" placeholder="输入命令，按 Enter 发送">
                            <button class="send-input primary" type="button">${GmsUi.icon('play')}发送</button>
                            <button class="send-ctrl-c" type="button" title="发送 0x03">Ctrl+C</button>
                        </div>
                    </div>
                </div>
                <aside class="console-sidebar">
                    <section class="console-device-info">
                        <h3>${GmsUi.icon('device')}设备信息</h3>
                        <dl class="console-info-grid"></dl>
                    </section>
                    <section class="console-operation-history">
                        <h3>${GmsUi.icon('clock')}本次操作</h3>
                        <ul class="console-activity"><li class="empty">尚无操作</li></ul>
                    </section>
                    <section class="history-column">
                    <div class="history-head">
                        <h3>历史日志</h3>
                        <button class="reload-log" type="button">${GmsUi.icon('refresh')}刷新</button>
                    </div>
                    <label>日期<select class="log-date"><option value="">最新</option></select></label>
                    <label>尾部行数<select class="log-tail"><option>200</option><option selected>500</option><option>2000</option><option>10000</option></select></label>
                    <pre class="history-output">暂无日志</pre>
                    <div class="history-actions">
                        <button class="download-log" type="button">${GmsUi.icon('download')}下载</button>
                        <button class="clear-logs danger" type="button">${GmsUi.icon('trash')}清空日志</button>
                    </div>
                    </section>
                    <section class="console-other-ports">
                        <h3>${GmsUi.icon('plug')}其他已绑定串口</h3>
                        <div class="console-other-list"></div>
                    </section>
                </aside>
            </div>`;
        $('console-section').append(pane);
        session.pane = pane;
        syncSessionHeading(session, port);
        if (!state.canManageDevices) {
            const clearButton = pane.querySelector('.clear-logs');
            clearButton.disabled = true;
            clearButton.title = '权限不足：清空日志需要 devices.inventory 权限';
        }
        bindConsolePaneEvents(session);
    }

    function bindConsolePaneEvents(session) {
        const pane = session.pane;
        // 重连/暂停/清屏/复制/关闭已上移 header 操作区（#console-toolbar），
        // 按 data-console-action 委托绑定，统一作用于当前激活会话。
        pane.querySelector('.send-input').addEventListener('click', () => sendInputField(session));
        pane.querySelector('.terminal-input').addEventListener('keydown', event => {
            if (event.key === 'Enter') { event.preventDefault(); sendInputField(session); }
        });
        pane.querySelector('.send-ctrl-c').addEventListener('click', () => sendInput(session, '\u0003', false));
        pane.querySelector('.reload-log').addEventListener('click', () => loadHistory(session));
        pane.querySelector('.log-date').addEventListener('change', () => loadHistory(session));
        pane.querySelector('.log-tail').addEventListener('change', () => loadHistory(session));
        pane.querySelector('.download-log').addEventListener('click', () => downloadLog(session));
        pane.querySelector('.clear-logs').addEventListener('click', () => clearLogs(session));
    }

    function createSession(port) {
        state.sessionCounter += 1;
        const session = {
            id: state.sessionCounter,
            portKey: port.port_key,
            title: portTitle(port),
            closed: false,
            socket: null,
            socketGeneration: 0,
            reconnectTimer: null,
            historyGeneration: 0,
            paused: false,
            pendingOutput: '',
            terminal: null,
            fitAddon: null,
            resizeObserver: null,
            tab: null,
            pane: null,
        };
        state.sessions.push(session);
        buildConsolePane(session, port);
        renderConsoleTabs();
        return session;
    }

    function openConsole(port) {
        const existing = sessionByKey(port.port_key);
        if (!existing) {
            const session = createSession(port);
            // 先切换视图让面板可见，再创建 xterm：在 display:none 的容器里
            // open/fit 会拿到 0 尺寸，渲染出默认 80x24 的“半屏”终端且不自愈。
            switchView('console', session.portKey);
            ensureTerminal(session);
            session.fitAddon?.fit();
            connectSocket(session);
            void loadHistory(session);
            return;
        }
        syncSessionHeading(existing, port);
        switchView('console', existing.portKey);
    }

    function closeConsole(portKey) {
        const session = sessionByKey(portKey);
        if (!session) return;
        session.closed = true;
        session.socketGeneration += 1;
        session.historyGeneration += 1;
        if (session.socket) session.socket.close();
        session.socket = null;
        clearTimeout(session.reconnectTimer);
        session.resizeObserver?.disconnect();
        session.terminal?.dispose();
        session.pane.remove();
        const index = state.sessions.indexOf(session);
        state.sessions = state.sessions.filter(item => item !== session);
        renderConsoleTabs();
        if (state.activeKey === portKey && state.activeView === 'console') {
            const next = state.sessions[index] || state.sessions[index - 1];
            if (next) switchView('console', next.portKey);
            else switchView('ports');
        } else {
            renderPorts();
        }
        persistWorkspace();
    }

    function restoreWorkspace() {
        const workspace = readWorkspace();
        workspace.portKeys.forEach(portKey => {
            const port = state.ports.find(item => item.port_key === portKey && item.binding);
            if (port) openConsole(port);
        });
        if (workspace.activeView === 'console' && sessionByKey(workspace.activeKey)) {
            switchView('console', workspace.activeKey);
        } else {
            switchView('ports');
        }
        persistWorkspace();
    }

    function bindEvents() {
        $('ports-tab').addEventListener('click', () => switchView('ports'));
        $('console-view-tabs').addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            const portsTab = $('ports-tab');
            const tabs = [portsTab, ...state.sessions.map(session => session.tab)].filter(Boolean);
            const index = tabs.indexOf(document.activeElement);
            if (index === -1) return;
            const nextIndex = event.key === 'Home' ? 0
                : event.key === 'End' ? tabs.length - 1
                    : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
            event.preventDefault();
            const nextTab = tabs[nextIndex];
            nextTab.focus();
            // overflow:hidden 的 tab 栏不响应滚轮（避免劫持页面滚动），
            // 键盘切换到视口外的 tab 时显式滚入可视区。
            nextTab.scrollIntoView({block: 'nearest', inline: 'nearest'});
            switchView(nextTab === portsTab ? 'ports' : 'console', nextTab.dataset.portKey || '');
        });
        $('refresh-ports').addEventListener('click', () => loadPorts());
        // header 控制台操作区：按钮作用于当前激活会话（data-console-action 分发）。
        const consoleToolbar = $('console-toolbar');
        consoleToolbar.addEventListener('click', async event => {
            const session = activeConsoleSession();
            if (!session) return;
            const action = event.target.closest('[data-console-action]')?.dataset.consoleAction;
            if (action === 'reconnect') connectSocket(session);
            else if (action === 'clear') clearTerminal(session);
            else if (action === 'copy') {
                try { await navigator.clipboard.writeText(terminalText(session)); notice('控制台内容已复制', 'success'); }
                catch (error) { notice(`复制失败：${error.message}`, 'error'); }
            } else if (action === 'close') closeConsole(session.portKey);
            else if (action === 'pause') {
                session.paused = !session.paused;
                syncConsoleToolbar();
                if (!session.paused && session.pendingOutput) {
                    const pending = session.pendingOutput;
                    session.pendingOutput = '';
                    appendOutput(session, pending);
                }
            }
        });
        $('binding-form').addEventListener('submit', saveBinding);
        $('close-binding').addEventListener('click', closeBinding);
        $('cancel-binding').addEventListener('click', closeBinding);
        $('delete-binding').addEventListener('click', deleteBinding);
        $('binding-modal').addEventListener('click', event => { if (event.target === $('binding-modal')) closeBinding(); });
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && !$('binding-modal').hidden) closeBinding();
        });
        window.addEventListener('gms:embedded-visibility', event => {
            syncPortAutoRefresh(event.detail?.visible !== false);
        });
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) syncPortAutoRefresh(false);
            else if (!window.GmsEmbeddedWorkspace) syncPortAutoRefresh(true);
        });
        window.addEventListener('beforeunload', () => {
            syncPortAutoRefresh(false);
            state.sessions.forEach(session => {
                session.closed = true;
                if (session.socket) session.socket.close();
                clearTimeout(session.reconnectTimer);
                session.resizeObserver?.disconnect();
                session.terminal?.dispose();
            });
        });
    }

    async function initialize() {
        bindEvents();
        await loadAuthStatus();
        try {
            await loadPorts();
            restoreWorkspace();
        }
        finally { window.GmsEmbeddedWorkspace?.markReady(); }
        syncPortAutoRefresh(!document.hidden);
    }

    initialize();
})();
