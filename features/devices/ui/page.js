(() => {
    'use strict';

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

    function button(text, action, className = '') {
        const node = element('button', className, text);
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
            addMeta(meta, '设备节点', port.devname);
            addMeta(meta, '稳定标识', port.by_id || port.port_key);
            addMeta(meta, 'USB 芯片', [port.vendor_product, port.driver].filter(Boolean).join(' · '));
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
        renderPorts();
        if (target) {
            requestAnimationFrame(() => {
                target.fitAddon?.fit();
                target.terminal?.scrollToBottom();
            });
        }
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

    async function loadBindingDeviceOptions(selectedLabel, editingKey) {
        const select = $('binding-label');
        select.disabled = true;
        select.replaceChildren(new Option('正在加载设备…', ''));
        try {
            const data = await api('/api/devices/management');
            if (state.editingKey !== editingKey) return;
            const devices = Array.isArray(data.devices) ? data.devices : [];
            const options = new Map();
            devices.forEach(device => {
                const value = String(device.device_id || device.serial_no || '').trim();
                if (!value || options.has(value)) return;
                const model = String(device.model || '').trim();
                options.set(value, model && model !== value ? `${value} · ${model}` : value);
            });
            select.replaceChildren(new Option(options.size ? '请选择设备' : '未发现设备', ''));
            options.forEach((text, value) => select.add(new Option(text, value)));
            if (selectedLabel && !options.has(selectedLabel)) {
                select.add(new Option(`${selectedLabel} · 当前绑定`, selectedLabel));
            }
            select.value = selectedLabel || '';
        } catch (error) {
            if (state.editingKey !== editingKey) return;
            select.replaceChildren(new Option('设备列表加载失败', ''));
            if (selectedLabel) select.add(new Option(`${selectedLabel} · 当前绑定`, selectedLabel));
            select.value = selectedLabel || '';
            notice(`设备列表加载失败：${error.message}`, 'error');
        } finally {
            if (state.editingKey === editingKey) select.disabled = false;
        }
    }

    function openBinding(port) {
        state.editingKey = port.port_key;
        const binding = port.binding || {};
        $('binding-port').textContent = `${port.devname || '离线端口'} · ${port.port_key}`;
        loadBindingDeviceOptions(binding.label || '', port.port_key);
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
        session.pane.querySelector('.console-title').textContent = session.title;
        session.pane.querySelector('.console-subtitle').textContent =
            `${port.devname || '当前离线'} · ${port.binding?.baudrate || ''} baud · ${port.port_key}`;
        const tabTitle = session.tab?.querySelector('.tab-title');
        if (tabTitle) tabTitle.textContent = `控制台${session.id}`;
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

    function setSocketStatus(session, text, className) {
        const node = session.pane.querySelector('.socket-status');
        node.textContent = text;
        node.className = `socket-status status-pill ${className}`;
    }

    function ensureTerminal(session) {
        if (session.terminal) return true;
        if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') {
            notice('终端渲染组件加载失败，请刷新页面重试', 'error');
            return false;
        }
        const terminal = new Terminal({
            convertEol: true,
            cursorBlink: false,
            disableStdin: true,
            fontFamily: 'Consolas, "Courier New", monospace',
            fontSize: 13,
            scrollback: 10000,
            theme: {background: '#070a0e', foreground: '#d5dfcf', cursor: '#d5dfcf'},
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
            setSocketStatus(session, '已连接', 'online');
        };
        socket.onmessage = event => {
            if (generation !== session.socketGeneration || session.closed) return;
            let message;
            try { message = JSON.parse(event.data); } catch { return; }
            if (message.type === 'data' || message.type === 'backlog') appendOutput(session, message.data || '');
            if (message.type === 'backlog') setWritable(session, Boolean(message.writable));
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
            session.pane.querySelector('.history-output').textContent = data.content || '暂无日志';
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
            <div class="section-heading console-heading">
                <div class="heading-main">
                    <h2 class="console-title"></h2>
                    <p class="console-subtitle"></p>
                </div>
                <div class="console-actions">
                    <span class="socket-status status-pill offline">未连接</span>
                    <button class="pause-output" type="button">暂停</button>
                    <button class="clear-screen" type="button">清屏</button>
                    <button class="copy-output" type="button">复制</button>
                    <button class="close-console" type="button">关闭</button>
                </div>
            </div>
            <div class="console-layout">
                <div class="terminal-column">
                    <div class="terminal-output terminal" tabindex="0" aria-label="串口输出"></div>
                    <div class="input-dock">
                        <div class="input-row">
                            <input class="terminal-input" type="text" autocomplete="off" placeholder="输入命令，按 Enter 发送">
                            <button class="send-input primary" type="button">发送</button>
                            <button class="send-ctrl-c" type="button" title="发送 0x03">Ctrl+C</button>
                        </div>
                    </div>
                </div>
                <aside class="history-column">
                    <div class="history-head">
                        <h3>历史日志</h3>
                        <button class="reload-log" type="button">刷新</button>
                    </div>
                    <label>日期<select class="log-date"><option value="">最新</option></select></label>
                    <label>尾部行数<select class="log-tail"><option>200</option><option selected>500</option><option>2000</option><option>10000</option></select></label>
                    <pre class="history-output">暂无日志</pre>
                    <div class="history-actions">
                        <button class="download-log" type="button">下载</button>
                        <button class="clear-logs danger" type="button">清空日志</button>
                    </div>
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
        pane.querySelector('.close-console').addEventListener('click', () => closeConsole(session.portKey));
        pane.querySelector('.clear-screen').addEventListener('click', () => clearTerminal(session));
        pane.querySelector('.copy-output').addEventListener('click', async () => {
            try { await navigator.clipboard.writeText(terminalText(session)); notice('控制台内容已复制', 'success'); }
            catch (error) { notice(`复制失败：${error.message}`, 'error'); }
        });
        pane.querySelector('.pause-output').addEventListener('click', () => {
            session.paused = !session.paused;
            pane.querySelector('.pause-output').textContent = session.paused ? '继续' : '暂停';
            if (!session.paused && session.pendingOutput) {
                const pending = session.pendingOutput;
                session.pendingOutput = '';
                appendOutput(session, pending);
            }
        });
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
        $('binding-form').addEventListener('submit', saveBinding);
        $('close-binding').addEventListener('click', closeBinding);
        $('cancel-binding').addEventListener('click', closeBinding);
        $('delete-binding').addEventListener('click', deleteBinding);
        $('binding-modal').addEventListener('click', event => { if (event.target === $('binding-modal')) closeBinding(); });
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
                session.resizeObserver?.disconnect();
                session.terminal?.dispose();
            });
        });
    }

    async function initialize() {
        bindEvents();
        await loadAuthStatus();
        try { await loadPorts(); }
        finally { window.GmsEmbeddedWorkspace?.markReady(); }
        syncPortAutoRefresh(!document.hidden);
    }

    initialize();
})();
