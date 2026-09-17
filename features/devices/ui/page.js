(() => {
    'use strict';

    const state = {
        ports: [],
        selectedKey: '',
        editingKey: '',
        socket: null,
        socketGeneration: 0,
        portsRequestGeneration: 0,
        portsRequestsInFlight: 0,
        historyRequestGeneration: 0,
        paused: false,
        pendingOutput: '',
        writable: false,
        refreshTimer: null,
        noticeTimer: null,
        activeView: 'ports',
        terminal: null,
        fitAddon: null,
        terminalResizeObserver: null,
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
            throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
        }
        return payload.data || payload;
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

    function renderPorts() {
        const list = $('ports-list');
        list.replaceChildren();
        $('ports-count').textContent = String(state.ports.length);
        if (!state.ports.length) {
            list.append(element('div', 'empty', '未发现 ttyUSB/ttyACM 串口。插入 USB 转串口线后点击刷新。'));
            return;
        }
        state.ports.forEach(port => {
            const card = element('article', `port-card${port.port_key === state.selectedKey ? ' selected' : ''}`);
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
            actions.append(button(port.binding ? '编辑绑定' : '绑定', () => openBinding(port)));
            if (port.binding) {
                actions.append(button('打开控制台', () => openConsole(port), 'primary'));
                actions.append(button(
                    port.capture_enabled ? '停止采集' : '启动采集',
                    () => toggleCapture(port),
                    port.capture_enabled ? 'capture-on' : ''
                ));
            }
            card.append(actions);
            list.append(card);
        });
    }

    function switchView(view) {
        if (view === 'console' && !state.selectedKey) return;
        state.activeView = view;
        const isPorts = view === 'ports';
        $('ports-section').hidden = !isPorts;
        $('console-section').hidden = isPorts;
        $('ports-tab').classList.toggle('active', isPorts);
        $('console-tab').classList.toggle('active', !isPorts);
        $('ports-tab').setAttribute('aria-selected', String(isPorts));
        $('console-tab').setAttribute('aria-selected', String(!isPorts));
        $('ports-tab').tabIndex = isPorts ? 0 : -1;
        $('console-tab').tabIndex = isPorts ? -1 : 0;
        if (!isPorts) {
            requestAnimationFrame(() => {
                state.fitAddon?.fit();
                state.terminal?.scrollToBottom();
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
            $('refresh-status').textContent = `更新于 ${new Date().toLocaleTimeString()}`;
            const selected = state.ports.find(item => item.port_key === state.selectedKey);
            if (selected) updateConsoleHeading(selected);
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
            if (state.selectedKey === key) closeConsole();
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

    function updateConsoleHeading(port) {
        $('console-title').textContent = portTitle(port);
        $('console-subtitle').textContent = `${port.devname || '当前离线'} · ${port.binding?.baudrate || ''} baud · ${port.port_key}`;
    }

    function setSocketStatus(text, className) {
        const node = $('socket-status');
        node.textContent = text;
        node.className = `status-pill ${className}`;
    }

    function setWritable(writable) {
        state.writable = Boolean(writable);
        $('terminal-input').disabled = !state.writable;
        $('send-input').disabled = !state.writable;
        $('send-ctrl-c').disabled = !state.writable;
    }

    function ensureTerminal() {
        if (state.terminal) return true;
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
        terminal.open($('terminal-output'));
        state.terminal = terminal;
        state.fitAddon = fitAddon;
        state.terminalResizeObserver = new ResizeObserver(() => {
            if (state.activeView === 'console') fitAddon.fit();
        });
        state.terminalResizeObserver.observe($('terminal-output'));
        fitAddon.fit();
        return true;
    }

    function clearTerminal() {
        if (state.terminal) state.terminal.reset();
        else $('terminal-output').textContent = '';
    }

    function terminalText() {
        if (!state.terminal) return $('terminal-output').textContent;
        if (state.terminal.hasSelection()) return state.terminal.getSelection();
        state.terminal.selectAll();
        const text = state.terminal.getSelection();
        state.terminal.clearSelection();
        return text;
    }

    function appendOutput(text) {
        if (!text) return;
        if (state.paused) {
            state.pendingOutput += text;
            if (state.pendingOutput.length > 2_000_000) state.pendingOutput = state.pendingOutput.slice(-2_000_000);
            return;
        }
        if (state.terminal) {
            state.terminal.write(text);
            return;
        }
        const output = $('terminal-output');
        output.textContent = `${output.textContent}${text}`.slice(-2_000_000);
        output.scrollTop = output.scrollHeight;
    }

    function connectSocket(portKey) {
        state.socketGeneration += 1;
        const generation = state.socketGeneration;
        if (state.socket) state.socket.close();
        setWritable(false);
        setSocketStatus('连接中', 'waiting');
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const socket = new WebSocket(`${protocol}//${location.host}/api/devices/console/ws/${encodeURIComponent(portKey)}`);
        state.socket = socket;
        socket.onopen = () => {
            if (generation !== state.socketGeneration) return;
            setSocketStatus('已连接', 'online');
        };
        socket.onmessage = event => {
            if (generation !== state.socketGeneration) return;
            let message;
            try { message = JSON.parse(event.data); } catch { return; }
            if (message.type === 'data' || message.type === 'backlog') appendOutput(message.data || '');
            if (message.type === 'backlog') setWritable(Boolean(message.writable));
            if (message.type === 'error') notice(friendlySerialError(message.error) || '串口操作失败', 'error');
        };
        socket.onerror = () => {
            if (generation !== state.socketGeneration) return;
            setWritable(false);
            setSocketStatus('连接失败', 'offline');
        };
        socket.onclose = event => {
            if (generation !== state.socketGeneration) return;
            state.socket = null;
            setWritable(false);
            setSocketStatus(event.code === 4404 ? '请先绑定' : '已断开', 'offline');
        };
    }

    async function openConsole(port) {
        state.selectedKey = port.port_key;
        state.paused = false;
        state.pendingOutput = '';
        $('pause-output').textContent = '暂停';
        $('console-tab').disabled = false;
        $('console-tab').textContent = `控制台 · ${portTitle(port)}`;
        updateConsoleHeading(port);
        renderPorts();
        switchView('console');
        ensureTerminal();
        clearTerminal();
        connectSocket(port.port_key);
        await loadHistory();
    }

    function closeConsole() {
        state.socketGeneration += 1;
        state.historyRequestGeneration += 1;
        if (state.socket) state.socket.close();
        state.socket = null;
        state.selectedKey = '';
        setWritable(false);
        $('console-tab').disabled = true;
        $('console-tab').textContent = '控制台';
        switchView('ports');
        renderPorts();
    }

    function sendInput(data, appendNewline) {
        if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
            setWritable(false);
            notice('串口尚未连接', 'error');
            return false;
        }
        try {
            state.socket.send(JSON.stringify({type: 'input', data, append_newline: appendNewline}));
            return true;
        } catch (error) {
            setWritable(false);
            notice(`串口发送失败：${error.message}`, 'error');
            return false;
        }
    }

    function sendInputField() {
        const input = $('terminal-input');
        if (sendInput(input.value, true)) input.value = '';
        input.focus();
    }

    async function loadHistory() {
        const portKey = state.selectedKey;
        if (!portKey) return;
        const generation = ++state.historyRequestGeneration;
        const selectedDate = $('log-date').value;
        const params = new URLSearchParams({tail: $('log-tail').value});
        if (selectedDate) params.set('date', selectedDate);
        try {
            const data = await api(`/api/devices/console/ports/${encodeURIComponent(portKey)}/logs?${params}`);
            if (generation !== state.historyRequestGeneration || state.selectedKey !== portKey) return;
            $('history-output').textContent = data.content || '暂无日志';
            const dateSelect = $('log-date');
            const effectiveDate = selectedDate || data.date || '';
            dateSelect.replaceChildren(new Option('最新', ''));
            (data.available_dates || []).forEach(date => dateSelect.add(new Option(date, date)));
            if (selectedDate && (data.available_dates || []).includes(selectedDate)) dateSelect.value = selectedDate;
            dateSelect.dataset.effectiveDate = effectiveDate;
        } catch (error) {
            if (generation !== state.historyRequestGeneration || state.selectedKey !== portKey) return;
            $('history-output').textContent = `日志读取失败：${error.message}`;
        }
    }

    function downloadLog() {
        if (!state.selectedKey) return;
        const params = new URLSearchParams();
        const date = $('log-date').value || $('log-date').dataset.effectiveDate;
        if (date) params.set('date', date);
        location.href = `/api/devices/console/ports/${encodeURIComponent(state.selectedKey)}/logs/download?${params}`;
    }

    async function clearLogs() {
        if (!state.selectedKey || !window.confirm('清空该串口的全部历史日志？此操作不可恢复。')) return;
        try {
            await api(`/api/devices/console/ports/${encodeURIComponent(state.selectedKey)}/logs`, {method: 'DELETE'});
            notice('串口日志已清空', 'success');
            await loadHistory();
        } catch (error) {
            notice(error.message, 'error');
        }
    }

    function bindEvents() {
        $('ports-tab').addEventListener('click', () => switchView('ports'));
        $('console-tab').addEventListener('click', () => switchView('console'));
        $('console-view-tabs').addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            const tabs = [$('ports-tab'), $('console-tab')].filter(tab => !tab.disabled);
            const index = tabs.indexOf(document.activeElement);
            if (index === -1 || !tabs.length) return;
            const nextIndex = event.key === 'Home' ? 0
                : event.key === 'End' ? tabs.length - 1
                    : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
            event.preventDefault();
            tabs[nextIndex].focus();
            switchView(tabs[nextIndex] === $('ports-tab') ? 'ports' : 'console');
        });
        $('refresh-ports').addEventListener('click', () => loadPorts());
        $('binding-form').addEventListener('submit', saveBinding);
        $('close-binding').addEventListener('click', closeBinding);
        $('cancel-binding').addEventListener('click', closeBinding);
        $('delete-binding').addEventListener('click', deleteBinding);
        $('binding-modal').addEventListener('click', event => { if (event.target === $('binding-modal')) closeBinding(); });
        $('close-console').addEventListener('click', closeConsole);
        $('clear-screen').addEventListener('click', clearTerminal);
        $('copy-output').addEventListener('click', async () => {
            try { await navigator.clipboard.writeText(terminalText()); notice('控制台内容已复制', 'success'); }
            catch (error) { notice(`复制失败：${error.message}`, 'error'); }
        });
        $('pause-output').addEventListener('click', () => {
            state.paused = !state.paused;
            $('pause-output').textContent = state.paused ? '继续' : '暂停';
            if (!state.paused && state.pendingOutput) {
                const pending = state.pendingOutput;
                state.pendingOutput = '';
                appendOutput(pending);
            }
        });
        $('send-input').addEventListener('click', sendInputField);
        $('terminal-input').addEventListener('keydown', event => {
            if (event.key === 'Enter') { event.preventDefault(); sendInputField(); }
        });
        $('send-ctrl-c').addEventListener('click', () => sendInput('\u0003', false));
        $('reload-log').addEventListener('click', loadHistory);
        $('log-date').addEventListener('change', loadHistory);
        $('log-tail').addEventListener('change', loadHistory);
        $('download-log').addEventListener('click', downloadLog);
        $('clear-logs').addEventListener('click', clearLogs);
        window.addEventListener('gms:embedded-visibility', event => {
            syncPortAutoRefresh(event.detail?.visible !== false);
        });
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) syncPortAutoRefresh(false);
            else if (!window.GmsEmbeddedWorkspace) syncPortAutoRefresh(true);
        });
        window.addEventListener('beforeunload', () => {
            syncPortAutoRefresh(false);
            state.historyRequestGeneration += 1;
            if (state.socket) state.socket.close();
            state.terminalResizeObserver?.disconnect();
            state.terminal?.dispose();
        });
    }

    async function initialize() {
        bindEvents();
        try { await loadPorts(); }
        finally { window.GmsEmbeddedWorkspace?.markReady(); }
        syncPortAutoRefresh(!document.hidden);
    }

    initialize();
})();