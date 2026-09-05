// Shell 模块：终端功能（从 shell.html 内联脚本尾部提取）。
// ==================== 终端功能 ====================
const terminalConfig = {
    worker_id: 'ats-worker-controller',
    ssh_host: '{{ config.ubuntu_host }}',
    ssh_user: '{{ config.ubuntu_user }}'
};

async function loadTerminalClusterHosts() {
    const select = document.getElementById('terminal-host-select');
    if (!select) return;
    const localWorkerId = workspaceLocalWorkerId();
    terminalConfig.worker_id = localWorkerId;
    const fallback = {
        worker_id: localWorkerId, name: 'Controller / Local Worker',
        address: terminalConfig.ssh_host, ssh_user: terminalConfig.ssh_user,
        status: 'online'
    };
    let hosts = [fallback];
    try {
        const directoryHosts = await loadClusterHostDirectory();
        if (directoryHosts.length) {
            hosts = [...directoryHosts];
            if (!hosts.some(host => host.worker_id === localWorkerId)) hosts.unshift(fallback);
        }
    } catch (error) {
        console.debug('Cluster host directory unavailable; using local host', error);
    }
    window.terminalClusterHosts = hosts;
    const previous = sessionStorage.getItem('pending_adb_worker') ||
        window.GmsWorkspace?.get?.().worker_id ||
        localStorage.getItem('gms_terminal_worker') || terminalConfig.worker_id;
    select.innerHTML = hosts.map(host => {
        const disabled = host.status === 'offline' ? ' disabled' : '';
        const suffix = host.status === 'offline' ? '（离线）' : '';
        return `<option value="${escapeHtml(host.worker_id)}"${disabled}>${escapeHtml(host.worker_id)}${suffix}</option>`;
    }).join('');
    if (hosts.some(host => host.worker_id === previous && host.status !== 'offline')) select.value = previous;
    // Refreshing the host directory is not a user host switch. Keep
    // the current workspace and its terminal/noVNC sessions intact.
    applyTerminalHost(select.value, false, false);
}

function applyTerminalHost(workerId, reconnect = true, syncWorkspace = true) {
    const hosts = window.terminalClusterHosts || [];
    const host = hosts.find(item => item.worker_id === workerId);
    if (!host || !host.address || !host.ssh_user) return;
    terminalConfig.worker_id = host.worker_id;
    terminalConfig.ssh_host = host.address;
    terminalConfig.ssh_user = host.ssh_user;
    localStorage.setItem('gms_terminal_worker', host.worker_id);
    if (syncWorkspace) {
        window.GmsWorkspace?.update({
            worker_id: host.worker_id,
            origin_page: 'terminal'
        }, {source: 'terminal-host'});
    }
    const label = document.getElementById('terminal-connection-label');
    if (label) label.textContent = `${host.ssh_user}@${host.address}`;
    if (reconnect && terminalInitialized) reconnectTerminal();
}

function switchTerminalHost() {
    const select = document.getElementById('terminal-host-select');
    if (select) applyTerminalHost(select.value, true);
}

// 终端静默模式处理（统一管理ADB和路由命令）
let silentMode = {
    active: false,
    type: null,  // 'adb' | 'route'
    buffer: [],
    pendingCommand: null  // 仅用于route模式
};

let isReconnecting = false;

// 同步函数
function updateSilentMode(active, type = null, command = null) {
    silentMode.active = active;
    silentMode.type = type;
    silentMode.pendingCommand = command;
}

function updateTerminalStatus(connected) {
    isTerminalConnected = connected;
    const statusElement = document.getElementById('terminal-status');
    if (statusElement) {
        statusElement.textContent = connected ? '已连接' : '未连接';
        statusElement.className = 'terminal-status ' + (connected ? 'connected' : 'disconnected');
    }
}

function connectTerminalSocket() {
    debugLog('Connecting to terminal WebSocket...');
    updateTerminalStatus(false);

    // 生成唯一的client_id
    const clientId = 'terminal_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);

    // 创建WebSocket连接
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/api/system/websocket/${clientId}`;
    const socket = new WebSocket(wsUrl);
    terminalSocket = socket;

    socket.onopen = () => {
        if (socket !== terminalSocket) return;
        debugLog('Terminal WebSocket connected');
        isReconnecting = false;
        updateTerminalStatus(true);

        // 检查是否为路由命令模式
        if (silentMode.type === 'route' && silentMode.pendingCommand) {
            debugLog('Route command mode: enabling silent mode');
            updateSilentMode(true, 'route', silentMode.pendingCommand);
            socket.send(JSON.stringify({
                type: 'terminal_connect',
                mode: 'ssh',
                worker_id: terminalConfig.worker_id
            }));
            return;
        }

        // 检查是否为ADB shell模式
        if (pendingAdbDevice) {
            debugLog('Opening ADB shell for device:', pendingAdbDevice);
            const adbDevice = pendingAdbDevice;
            updateSilentMode(true, 'adb');
            socket.send(JSON.stringify({
                type: 'terminal_connect',
                mode: 'adb',
                serial_no: adbDevice,
                worker_id: sessionStorage.getItem('pending_adb_worker') || terminalConfig.worker_id
            }));
            pendingAdbDevice = null;
            sessionStorage.removeItem('pending_adb_device');
            sessionStorage.removeItem('pending_adb_worker');
        } else {
            // 请求SSH连接
            socket.send(JSON.stringify({
                type: 'terminal_connect',
                mode: 'ssh',
                worker_id: terminalConfig.worker_id
            }));
        }
    };

    socket.onclose = () => {
        if (socket !== terminalSocket) return;
        debugLog('Terminal WebSocket disconnected');
        updateTerminalStatus(false);
        // 只有在非重新连接时才显示断开消息
        if (terminal && !isReconnecting) {
            terminal.writeln('\r\n\x1b[31m⚠️ 连接已断开\x1b[0m\r\n');
        }
    };

    socket.onerror = (error) => {
        if (socket !== terminalSocket) return;
        console.error('WebSocket error:', error);
        updateTerminalStatus(false);
    };

    socket.onmessage = (event) => {
        if (socket !== terminalSocket) return;
        try {
            const msg = JSON.parse(event.data);

            if (msg.type === 'terminal_data') {
                if (terminal) {
                    // 静默模式:缓冲输出,等待提示符
                    if (silentMode.active) {
                        silentMode.buffer.push(msg.data);

                        // 优化：只检查最后10条消息，避免O(n²)性能问题
                        const recentText = silentMode.buffer.length > 10
                            ? silentMode.buffer.slice(-10).join('')
                            : silentMode.buffer.join('');

                        // 路由命令模式:检测Linux shell提示符
                        if (silentMode.type === 'route' && silentMode.pendingCommand) {
                            if (/^[\w-]+@[\w-]+:.+[$#]\s*$/.test(recentText) ||
                                /@.+[$#]\s*$/.test(recentText) ||
                                recentText.includes(':~$') || recentText.includes(':~#')) {

                                debugLog('Shell prompt detected for route command');
                                const commandToSend = silentMode.pendingCommand;

                                terminal.clear();
                                terminal.writeln('\x1b[33m========================================\x1b[0m');
                                terminal.writeln('\x1b[33m📋 路由命令已准备就绪\x1b[0m');
                                terminal.writeln('\x1b[33m========================================\x1b[0m\r\n');
                                terminal.writeln('\x1b[36m命令: ' + commandToSend + '\x1b[0m\r\n');
                                terminal.writeln('\x1b[90m提示: 正在自动执行命令，可能需要输入 sudo 密码\x1b[0m\r\n');
                                terminal.writeln('\x1b[33m========================================\x1b[0m\r\n');

                                setTimeout(() => {
                                    if (terminalSocket && terminalSocket.readyState === WebSocket.OPEN && commandToSend) {
                                        terminalSocket.send(JSON.stringify({
                                            type: 'terminal_input',
                                            input: commandToSend + '\r'
                                        }));
                                        debugLog('Route command sent:', commandToSend);
                                    }
                                }, 500);

                                terminal.focus();
                                updateSilentMode(false);
                            }
                        } else if (silentMode.type === 'adb') {
                            if (/:\/ [$#]/.test(recentText) || /\w+:\S+ [$#]/.test(recentText)) {
                                debugLog('ADB shell prompt detected');
                                const text = silentMode.buffer.join('');
                                const lines = text.split('\r\n');
                                terminal.clear();
                                terminal.write(lines.slice(-3).join('\r\n'));
                                updateSilentMode(false);
                            }
                        }
                    } else {
                        terminal.write(msg.data);
                    }
                }
            } else if (msg.type === 'terminal_error') {
                console.error('Terminal error:', msg.error);
                if (terminal) {
                    terminal.writeln(`\r\n\x1b[31m❌ 错误: ${msg.error}\x1b[0m\r\n`);
                }
                updateTerminalStatus(false);
                updateSilentMode(false);  // 出错时退出静默模式
            } else if (msg.type === 'terminal_connected') {
                debugLog('Terminal connected, mode:', msg.mode);
                // 路由命令模式和ADB模式:不显示连接消息,让输出直接显示
                if (msg.mode !== 'adb' && silentMode.type !== 'route') {
                    if (terminal) {
                        terminal.clear();
                        terminal.writeln('\x1b[32m✅ 已连接到 Ubuntu 主机\x1b[0m');
                        terminal.writeln('\x1b[90m快捷键: Ctrl+C=复制/中断 Ctrl+V/右键=粘贴 Ctrl+D=EOF Ctrl+L=清屏\x1b[0m\r\n');
                    }
                }
                // ADB模式和路由命令模式:保持静默模式,等待检测到提示符
                updateTerminalStatus(true);

                // 连接建立后立即发送当前终端大小，确保PTY大小匹配
                if (terminal && terminalSocket && terminalSocket.readyState === WebSocket.OPEN) {
                    const dims = { cols: terminal.cols, rows: terminal.rows };
                    debugLog('Sending initial terminal size:', dims);
                    terminalSocket.send(JSON.stringify({
                        type: 'terminal_resize',
                        cols: dims.cols,
                        rows: dims.rows
                    }));
                }
            }
        } catch (e) {
            console.error('Error parsing WebSocket message:', e);
        }
    };
}

function initTerminal() {
    if (terminalInitialized) return;

    debugLog('Initializing terminal...');

    // 动态加载 xterm.js（如果还没加载）
    if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') {
        loadXTermScripts()
            .then(() => initTerminal())
            .catch((error) => {
                console.error('xterm scripts load failed:', error);
                document.getElementById('terminal').innerHTML = `
                    <div style="color:white;padding:40px;text-align:center;">
                        <div style="font-size:48px;margin-bottom:20px;">❌</div>
                        <div style="font-size:18px;margin-bottom:10px;">xterm.js 库加载失败</div>
                        <div style="color:var(--text-muted);font-size:14px;margin-bottom:20px;">
                            可能原因：<br>
                            1. 网络连接问题<br>
                            2. 静态资源加载失败<br>
                            3. 防火墙阻止
                        </div>
                        <button onclick="location.reload()" style="padding:10px 20px;font-size:14px;cursor:pointer;background:var(--success-color);color:white;border:none;border-radius:4px;">
                            🔄 刷新页面重试
                        </button>
                    </div>
                `;
            });
        return;
    }

    if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') {
        console.error('Terminal or FitAddon is not defined!');
        return;
    }

    // 创建终端
    terminal = new Terminal({
        cursorBlink: true,
        fontSize: 14,
        fontFamily: 'Consolas, "Courier New", monospace',
        theme: createTerminalTheme(),
        scrollback: 1000,
        convertEol: false,  // 不转换行尾，保持原始格式
        ignoreBracketedPasteMode: true,  // 粘贴时不包裹 \x1b[200~...\x1b[201~
        termName: 'xterm-256color'  // 设置终端类型，确保正确的转义序列处理
    });

    // 加载FitAddon
    try {
        const fitAddon = new FitAddon.FitAddon();
        terminal.loadAddon(fitAddon);

        const container = document.getElementById('terminal');
        terminal.open(container);
        fitAddon.fit();

        window.addEventListener('resize', () => {
            if (currentPage === 'terminal') {
                fitAddon.fit();
            }
        });

        debugLog('FitAddon loaded');
    } catch (e) {
        console.error('FitAddon error:', e);
        const container = document.getElementById('terminal');
        terminal.open(container);
    }

    // 设备提示符出现后再显示 ADB 启动输出。
    // 路由命令模式同样保持画布空白：silentMode 检测到 shell 提示符后会
    // terminal.clear() 再写入路由横幅，提前打印"正在连接"会造成清屏闪烁。
    const isAdbLaunch = Boolean(pendingAdbDevice);
    const isRouteLaunch = silentMode.type === 'route' && Boolean(silentMode.pendingCommand);
    if (!isAdbLaunch && !isRouteLaunch) {
        terminal.writeln('\x1b[33m⏳ 正在连接到 Ubuntu 主机...\x1b[0m');
        terminal.writeln(`\x1b[90m主机: ${terminalConfig.ssh_user}@${terminalConfig.ssh_host}\x1b[0m\r\n`);
    } else {
        terminal.clear();
    }

    // 设置数据处理器
    terminal.onData((data) => {
        if (isTerminalConnected && terminalSocket && terminalSocket.readyState === WebSocket.OPEN) {
            // 确保正确处理方向键等特殊按键的ANSI转义序列
            // onData已经正确地将方向键转换为转义序列（如\x1b[A）
            // 直接发送原始数据，不做任何额外处理
            sendTerminalInput(data);
        }
    });

    let lastPasteText = '';
    let lastPasteSentAt = 0;

    function sendTerminalInput(input, source = 'keyboard') {
        if (!isTerminalConnected || !terminalSocket || terminalSocket.readyState !== WebSocket.OPEN) {
            return false;
        }
        if (source !== 'paste' && input && input === lastPasteText && Date.now() - lastPasteSentAt < 700) {
            return false;
        }
        terminalSocket.send(JSON.stringify({
            type: 'terminal_input',
            input: input
        }));
        return true;
    }

    function copyTerminalSelection() {
        if (!terminal || !terminal.hasSelection()) {
            return false;
        }
        const selection = terminal.getSelection();
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(selection).catch(() => fallbackCopyText(selection));
        } else {
            fallbackCopyText(selection);
        }
        terminal.clearSelection();
        terminal.focus();
        debugLog('复制成功:', selection);
        return true;
    }

    function fallbackCopyText(text) {
        const textArea = document.createElement('textarea');
        textArea.value = text;
        textArea.style.position = 'fixed';
        textArea.style.left = '-999999px';
        textArea.style.top = '-999999px';
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        document.execCommand('copy');
        document.body.removeChild(textArea);
    }

    function pasteTextToTerminal(text) {
        if (!text) return false;
        const normalizedText = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        lastPasteText = normalizedText;
        lastPasteSentAt = Date.now();
        return sendTerminalInput(normalizedText, 'paste');
    }

    let suppressPasteEventUntil = 0;

    async function pasteFromClipboard() {
        try {
            if (!navigator.clipboard || !navigator.clipboard.readText) {
                debugLog('浏览器不允许直接读取剪贴板');
                return false;
            }
            const text = await navigator.clipboard.readText();
            return pasteTextToTerminal(text);
        } catch (error) {
            debugLog('读取剪贴板失败:', error);
            return false;
        } finally {
            terminal.focus();
        }
    }

    // 添加完整终端快捷键支持
    terminal.attachCustomKeyEventHandler((event) => {
        if (event.type !== 'keydown') {
            return true;
        }

        const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
        const isCtrlOnly = event.ctrlKey && !event.altKey && !event.metaKey;

        // Ctrl+C / Ctrl+Shift+C: 有选区时复制；无选区时发送中断信号。
        if (isCtrlOnly && key === 'c') {
            if (terminal.hasSelection()) {
                copyTerminalSelection();
            } else {
                sendTerminalInput('\x03');
            }
            return false;
        }

        // 读取剪贴板后短暂抑制 paste 事件，防止重复发送。
        if (isCtrlOnly && key === 'v') {
            suppressPasteEventUntil = Date.now() + 500;
            pasteFromClipboard();
            return false;
        }

        // 将浏览器占用的 Ctrl+字母组合按终端控制字符发送。
        if (isCtrlOnly && /^[a-z]$/.test(key)) {
            sendTerminalInput(String.fromCharCode(key.charCodeAt(0) - 96));
            return false;
        }

        // 确保方向键和其他特殊按键不被浏览器拦截
        // 方向键: ArrowUp, ArrowDown, ArrowLeft, ArrowRight
        if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) {
            // 让终端正常处理这些按键，不做任何拦截
            return true;
        }

        return true;
    });

    // 处理终端调整大小
    terminal.onResize(({ cols, rows }) => {
        if (terminalSocket && terminalSocket.readyState === WebSocket.OPEN) {
            terminalSocket.send(JSON.stringify({
                type: 'terminal_resize',
                cols: cols,
                rows: rows
            }));
        }
    });

    // 聚焦终端
    terminal.focus();

    // 点击聚焦
    const terminalDiv = document.getElementById('terminal');
    if (terminalDiv) {
        if (terminalDomEventController) {
            terminalDomEventController.abort();
        }
        terminalDomEventController = new AbortController();
        const terminalDomListenerOptions = { signal: terminalDomEventController.signal };
        const terminalDomCaptureOptions = { signal: terminalDomEventController.signal, capture: true };

        terminalDiv.addEventListener('click', () => {
            terminal.focus();
        }, terminalDomListenerOptions);

        terminalDiv.addEventListener('paste', (e) => {
            if (Date.now() < suppressPasteEventUntil) {
                e.preventDefault();
                e.stopPropagation();
                return;
            }
            const text = e.clipboardData ? e.clipboardData.getData('text/plain') : '';
            if (text) {
                e.preventDefault();
                e.stopPropagation();
                pasteTextToTerminal(text);
                terminal.focus();
            }
        }, terminalDomCaptureOptions);

        terminalDiv.addEventListener('contextmenu', (e) => {
            if (navigator.clipboard && navigator.clipboard.readText) {
                e.preventDefault();
                e.stopPropagation();
                pasteFromClipboard();
            }
        }, terminalDomListenerOptions);
    }

    // 添加拖拽文件推送功能
    const terminalContainer = terminalDiv ? terminalDiv.parentElement : null;
    if (!terminalContainer) {
        console.error('Terminal container not found');
        return;
    }

    terminalContainer.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.stopPropagation();
        terminalContainer.style.border = '3px dashed var(--primary-color)';
        terminalContainer.style.background = 'rgba(76, 175, 80, 0.1)';
    });

    terminalContainer.addEventListener('dragleave', (e) => {
        e.preventDefault();
        e.stopPropagation();
        terminalContainer.style.border = '1px solid var(--border-color)';
        terminalContainer.style.background = 'var(--light-bg)';
    });

    terminalContainer.addEventListener('drop', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        terminalContainer.style.border = '1px solid var(--border-color)';
        terminalContainer.style.background = 'var(--light-bg)';

        const files = e.dataTransfer.files;
        if (files.length === 0) return;

        const file = files[0];
        const fileName = file.name;

        if (!await ensureTerminalElevation(
            false,
            '上传文件到主机',
            '主机文件上传'
        )) return;
        terminal.writeln(`\r\n\x1b[33m📤 正在上传文件: ${fileName} (${formatFileSize(file.size)})\x1b[0m`);

        try {
            const formData = new FormData();
            formData.append('file', file);
            formData.append('worker_id', terminalConfig.worker_id);

            const result = await apiCall(
                '/api/terminal/push',
                'POST',
                formData
            );
            terminal.writeln(`\x1b[32m✅ 文件已上传到: ${result.remote_path}\x1b[0m`);
            terminal.writeln(`\x1b[90m使用: adb push ${result.remote_path} <目标路径>\x1b[0m`);
            terminal.writeln(`\x1b[90m示例: adb push ${result.remote_path} /data/local/tmp/\x1b[0m`);

            // 发送回车键,刷新提示符
            if (typeof terminalSocket !== 'undefined' && terminalSocket.readyState === WebSocket.OPEN) {
                terminalSocket.send(JSON.stringify({
                    type: 'terminal_input',
                    input: '\r'
                }));
            }
        } catch (error) {
            terminal.writeln(`\x1b[31m❌ 上传失败: ${error.message}\x1b[0m`);
            // 发送回车键
            if (typeof terminalSocket !== 'undefined' && terminalSocket.readyState === WebSocket.OPEN) {
                terminalSocket.send(JSON.stringify({
                    type: 'terminal_input',
                    data: '\r'
                }));
            }
        }
    });

    // 连接WebSocket
    connectTerminalSocket();

    terminalInitialized = true;
    debugLog('Terminal initialized');
}

// 格式化文件大小
function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

// 终端控制函数
function clearTerminal() {
    if (terminal) {
        terminal.clear();
    }
}

async function reconnectTerminal() {
    if (!await ensureTerminalElevation()) return;
    isReconnecting = true;
    if (terminalSocket) {
        terminalSocket.close();
        terminalSocket = null;
    }
    if (terminal) {
        terminal.clear();
        terminal.writeln('\x1b[33m🔄 正在重新连接...\x1b[0m\r\n');
    }
    connectTerminalSocket();
}

// 键盘快捷键
document.addEventListener('keydown', (e) => {
    if (currentPage !== 'terminal') return;

    if (e.ctrlKey && e.key === 'l') {
        e.preventDefault();
        clearTerminal();
    }
});
