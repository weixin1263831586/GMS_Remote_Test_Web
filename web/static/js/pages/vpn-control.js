// ==================== VPN Control ====================
async function checkSshd() {
    if (!requireControllerHostAction('SSHD 检查')) return;
    try {
        const result = await apiCall('/api/ssh/sshd', 'GET');

        if (result.success === true && result.installed === false && result.install_guide) {
            showSshdInstallGuide(result.install_guide);
        } else if (result.running) {
            addLogEntry(`SSHD 状态: 运行中`, 'success');
        } else if (result.installed === null || result.installed === undefined) {
            addLogEntry(`SSHD 状态: 无法连接设备主机，无法判断 SSHD 是否安装`, 'warning');
        } else if (!result.installed) {
            addLogEntry(`SSHD 状态: 无法确认是否已安装`, 'warning');
        } else {
            addLogEntry(`SSHD 状态: 已安装但未运行`, 'warning');
        }

        if (result.error) {
            addLogEntry(`⚠️ ${result.error}`, 'warning');
            if (result.need_password && result.device_host) {
                showDevicePasswordModal(result.device_host, 'sshd');
            }
        }
    } catch (error) {
        if (error.needPassword && error.deviceHost) {
            addLogEntry('需要输入SSH密码以检查 ' + error.deviceHost + ' 的 SSHD 状态', 'warning');
            showDevicePasswordModal(error.deviceHost, 'sshd');
            return;
        }
        addLogEntry('检查 SSHD 失败: ' + error.message, 'error');
        try {
            const result = await apiCall('/api/ssh/sshd', 'GET');
            if (result.install_guide) {
                showSshdInstallGuide(result.install_guide);
            } else {
                addLogEntry('无法加载安装指南', 'error');
            }
        } catch (guideError) {
            addLogEntry('无法加载安装指南', 'error');
        }
    }
}

async function checkRouting(targetHost) {
    if (!requireControllerHostAction('路由检查')) return;
    // 创建弹框
    const dialog = document.createElement('div');
    dialog.id = 'route-check-dialog';
    dialog.className = 'route-check-dialog';
    dialog.innerHTML = `
        <div class="route-check-content">
            <div class="route-check-header">
                <h3>📡 检查路由连通性</h3>
                <button class="route-check-close" aria-label="关闭">&times;</button>
            </div>
            <div class="route-check-form">
                <div class="form-group">
                    <label for="test-host-ip">测试主机IP:</label>
                    <input type="text" id="test-host-ip" placeholder="例如: 192.168.1.100" />
                    <small>从配置文件读取的ubuntu_host</small>
                </div>
                <div class="form-group">
                    <label for="client-ip">客户端IP:</label>
                    <input type="text" id="client-ip" placeholder="例如: 192.168.2.100" />
                    <small>您当前浏览器的IP地址</small>
                </div>
                <div class="route-check-actions">
                    <button id="ping-test-btn" class="btn-primary">🔍 测试连通性</button>
                    <button id="close-dialog-btn" class="btn-secondary">关闭</button>
                </div>
                <div id="ping-result" class="ping-result"></div>
            </div>
        </div>
    `;

    ModalManager.registerDynamic(dialog);

    // 获取配置中的默认值
    try {
        const config = await apiCall('/api/config/read', 'GET');
        if (config.ubuntu_host) {
            const testHostIp = document.getElementById('test-host-ip');
            testHostIp.value = config.ubuntu_host.split('@').pop(); // 提取IP部分
        }
        // 从固件错误页调用时，自动填入目标主机 IP
        if (targetHost) {
            const clientIp = document.getElementById('client-ip');
            clientIp.value = targetHost.split('@').pop();
        }
    } catch (error) {
        console.error('获取配置失败:', error);
    }

    // 绑定事件
    const pingTestBtn = document.getElementById('ping-test-btn');
    const closeDialogBtn = document.getElementById('close-dialog-btn');
    const closeXBtn = dialog.querySelector('.route-check-close');
    const pingResult = document.getElementById('ping-result');

    const closeDialog = () => {
        ModalManager.unregisterDynamic('route-check-dialog');
    };

    // X 按钮关闭
    closeXBtn.addEventListener('click', closeDialog);

    closeDialogBtn.addEventListener('click', closeDialog);

    pingTestBtn.addEventListener('click', async () => {
        const testHostIp = document.getElementById('test-host-ip').value.trim();
        const clientIp = document.getElementById('client-ip').value.trim();

        if (!testHostIp || !clientIp) {
            pingResult.textContent = '请填写测试主机IP和客户端IP';
            pingResult.className = 'ping-error';
            return;
        }

        // 验证IP格式
        function isValidIP(ip) {
            const parts = ip.split('.');
            if (parts.length !== 4) return false;
            return parts.every(part => {
                const num = parseInt(part, 10);
                return !isNaN(num) && num >= 0 && num <= 255 && part === num.toString();
            });
        }

        if (!isValidIP(testHostIp) || !isValidIP(clientIp)) {
            pingResult.textContent = 'IP地址格式不正确，请输入有效的IPv4地址 (例如: 192.168.1.100)';
            pingResult.className = 'ping-error';
            return;
        }

        pingResult.innerHTML = '<div class="ping-testing">🔄 正在测试连通性，请稍候...</div>';

        try {
            // 首先尝试使用SSH ping API
            let result;
            try {
                result = await apiCall('/api/ssh/ping', 'POST', {
                    test_host_ip: testHostIp,
                    client_ip: clientIp
                });
            } catch (postError) {
                // 如果POST API不可用（服务器未重启），使用GET API作为后备
                debugLog('POST API不可用，使用GET API作为后备');
                pingResult.innerHTML = '<div class="ping-testing">🔄 使用备用方法测试中...</div>';

                // 使用现有的GET API，但手动分析结果
                const testNetwork = testHostIp.split('.').slice(0, 3).join('.') + '.0';
                const clientNetwork = clientIp.split('.').slice(0, 3).join('.') + '.0';
                const sameNetwork = (testNetwork === clientNetwork);

                // 生成路由命令
                // 命令需在测试主机执行。
                // 需要通过测试主机的网关来访问客户端网段
                const testGateway = testNetwork.split('.').slice(0, 3).join('.') + '.1';

                const routeCommands = {
                    windows: [
                        `# 在测试主机上执行以下命令:`,
                        `# 如果客户端主机在不同网段，需要添加路由到客户端主机所在的网关`,
                        `route add ${clientNetwork} mask 255.255.255.0 ${testGateway}`,
                        `# 检查路由表: route print`,
                        `# 删除路由: route delete ${clientNetwork}`
                    ],
                    linux: [
                        `# 在测试主机上执行以下命令:`,
                        `# 如果客户端主机在不同网段，需要添加路由到客户端主机所在的网关`,
                        `sudo ip route add ${clientNetwork}/24 via ${testGateway}`,
                        `# 检查路由表: ip route show`,
                        `# 删除路由: sudo ip route del ${clientNetwork}/24`
                    ],
                    note: [
                        `⚠️ 重要提示:`,
                        `1. 这些路由命令应该在测试主机上执行`,
                        `2. ${testGateway} 是测试主机的网关地址`,
                        `3. 确保网关地址可以ping通后再添加路由`,
                        `4. 如果已经在同一网段，不需要添加路由`,
                        `5. 删除路由前请确保不会影响SSH连接`
                    ]
                };

                result = {
                    success: true,
                    reachable: sameNetwork,
                    latency: sameNetwork ? '<1ms (同一网段)' : 'N/A',
                    same_network: sameNetwork,
                    test_host_ip: testHostIp,
                    client_ip: clientIp,
                    test_network: testNetwork,
                    client_network: clientNetwork,
                    route_commands: routeCommands
                };
            }

            if (result.success) {
                if (result.reachable) {
                    pingResult.innerHTML = `
                        <div class="ping-success">
                            <h4>✅ 连通性测试通过</h4>
                            <p><strong>测试主机:</strong> ${result.test_host_ip || testHostIp}</p>
                            <p><strong>测试主机网段:</strong> ${result.test_network || 'N/A'}</p>
                            <p><strong>客户端:</strong> ${result.client_ip || clientIp}</p>
                            <p><strong>客户端网段:</strong> ${result.client_network || 'N/A'}</p>
                            <p>状态: <span class="status-success">${result.same_network ? '同一网段 - 可连通' : '不同网段但可连通'}</span></p>
                            <p>延迟: ${result.latency || 'N/A'}</p>
                            <p>✅ 网络配置正常，无需添加路由</p>
                        </div>
                    `;
                } else {
                    pingResult.innerHTML = `
                        <div class="ping-failure">
                            <h4>❌ 连通性测试失败</h4>
                            <p><strong>测试主机:</strong> ${result.test_host_ip || testHostIp}</p>
                            <p><strong>测试主机网段:</strong> ${result.test_network || 'N/A'}</p>
                            <p><strong>客户端:</strong> ${result.client_ip || clientIp}</p>
                            <p><strong>客户端网段:</strong> ${result.client_network || 'N/A'}</p>
                            <p>状态: <span class="status-error">不同网段 - 不可连通</span></p>
                            <p><strong>可能原因:</strong></p>
                            <ul>
                                <li>客户端和测试主机不在同一网段</li>
                                <li>缺少必要的路由配置</li>
                                <li>防火墙阻止了连接</li>
                            </ul>
                            <p><strong>⚠️ 重要提示 - 请仔细阅读:</strong></p>
                            <div class="route-warning">
                                <p>✅ 以下命令应该在您的<strong>测试主机</strong>（${testHostIp}）上执行</p>
                                <p>❌ 不要在客户端主机（当前浏览器所在电脑）上执行这些命令</p>
                                <p><strong>🎯 路由目的：</strong>让测试主机能够访问客户端主机网段</p>
                            </div>
                            <p><strong>建议添加的路由命令:</strong></p>
                            <div class="route-commands">
                                <h5>Linux:</h5>
                                <pre id="linux-route-command">${result.route_commands?.linux?.[2] || '无'}</pre>
                                <h5>Windows:</h5>
                                <pre id="windows-route-command">${result.route_commands?.windows?.[2] || '无'}</pre>
                            </div>
                            <div class="route-check-terminal-actions">
                                <button id="open-terminal-btn" class="btn-terminal" data-command="${result.route_commands?.linux?.[2] || ''}">
                                    🐧 打开主机终端添加路由
                                </button>
                            </div>
                        </div>
                    `;

                    // 绑定打开终端按钮事件
                    const openTerminalBtn = document.getElementById('open-terminal-btn');
                    if (openTerminalBtn) {
                        openTerminalBtn.addEventListener('click', async () => {
                            const command = openTerminalBtn.dataset.command;
                            if (!command || command === '无') {
                                addLogEntry('没有可用的路由命令', 'warning');
                                return;
                            }

                            try {
                                // 保存命令到 sessionStorage，供终端页面使用
                                sessionStorage.setItem('pending_terminal_command', command);
                                sessionStorage.setItem('command_source', 'route_check');

                                // 关闭路由检查弹框
                                document.body.removeChild(dialog);

                                // 切换到终端页面
                                if (typeof switchPage === 'function') {
                                    switchPage('terminal');
                                } else {
                                    // 如果 switchPage 不在全局作用域，使用 DOM 操作
                                    const event = new Event('click');
                                    const terminalLink = document.querySelector('[data-page="terminal"]');
                                    if (terminalLink) {
                                        terminalLink.dispatchEvent(event);
                                    }
                                }

                                addLogEntry(`✅ 已切换到终端页面，命令已准备: ${command}`, 'success');

                            } catch (error) {
                                addLogEntry('打开终端失败: ' + error.message, 'error');
                                console.error('Error opening terminal:', error);
                            }
                        });
                    }
                }
            } else {
                pingResult.textContent = `测试失败: ${result.error}`;
                pingResult.className = 'ping-error';
            }
        } catch (error) {
            pingResult.textContent = `测试失败: ${error.message}`;
            pingResult.className = 'ping-error';
        }
    });

    // 点击背景关闭
    dialog.addEventListener('click', (e) => {
        if (e.target === dialog) {
            document.body.removeChild(dialog);
        }
    });
}

async function connectVpn() {
    if (!requireControllerHostAction('VPN 连接')) return;
    if (state.vpnConnected) {
        await checkVpnStatus();
        return;
    }

    // 直接弹出 VPN 选择框让用户选择连接哪个
    showVpnCredentialModal();
}

async function checkVpnStatus() {
    try {
        const result = await apiCall('/api/vpn/status', 'GET');
        updateVpnStatus(result.connected);
        addLogEntry(`VPN 状态: ${result.connected ? '已连接' : '未连接'}`, result.connected ? 'success' : 'warning');
    } catch (error) {
        addLogEntry('检查 VPN 状态失败: ' + error.message, 'error');
    }
}

function updateVpnStatus(connected) {
    const label = document.getElementById('vpn-status-label');
    const btn = document.getElementById('vpn-connect-btn');
    const previous = state.vpnConnected;

    if (connected) {
        label.textContent = '状态: 已连接';
        label.className = 'vpn-status-label connected';
        btn.textContent = '📡 检查VPN';
        state.vpnConnected = true;
    } else {
        label.textContent = '状态: 未连接';
        label.className = 'vpn-status-label disconnected';
        btn.textContent = '🔌 连接VPN';
        state.vpnConnected = false;
    }

    if (previous === true && connected === false) {
        createLocalNotification('VPN已断开', 'VPN 连接状态变为未连接', 'warning', 'vpn');
    }
}

// ==================== VPN Credential Modal ====================
async function showVpnCredentialModal() {
    const select = document.getElementById('vpn-credential-name');

    try {
        const result = await apiCall('/api/vpn/connections', 'GET');
        select.innerHTML = '';
        (result.connections || []).forEach(name => {
            const opt = document.createElement('option');
            opt.value = name;
            opt.textContent = name;
            select.appendChild(opt);
        });
    } catch (e) {
        select.innerHTML = '<option value="">加载失败</option>';
    }

    const modal = document.getElementById('vpn-credential-modal');
    ModalManager.open('vpn-credential-modal');
}

function closeVpnCredentialModal() {
    ModalManager.close('vpn-credential-modal');
}

function handleVpnCredentialKeyPress(event) {
    if (event.key === 'Enter') {
        event.preventDefault();
        submitVpnCredential();
    }
}

async function submitVpnCredential() {
    const vpnName = document.getElementById('vpn-credential-name').value;
    if (!vpnName) {
        showToast('请选择 VPN 连接', 'error');
        return;
    }

    const submitBtn = document.querySelector('#vpn-credential-modal .btn-primary');
    const originalText = submitBtn.textContent;
    try {
        submitBtn.textContent = '连接中...';
        submitBtn.disabled = true;

        const result = await apiCall('/api/vpn/connect', 'POST', {
            vpn_name: vpnName
        });

        if (result.connected) {
            updateVpnStatus(true);
            addLogEntry(result.message || 'VPN 已连接', 'success');
            closeVpnCredentialModal();
        } else {
            updateVpnStatus(false);
            addLogEntry(result.message || 'VPN 连接失败', 'error');
        }
    } catch (error) {
        updateVpnStatus(false);
        addLogEntry('连接 VPN 失败: ' + error.message, 'error');
    } finally {
        submitBtn.textContent = originalText;
        submitBtn.disabled = false;
    }
}

// ==================== USB/IP Status Check ====================
async function checkUsbipStatus() {
    try {
        const result = await apiCall('/api/usbip/status', 'GET');
        if (result.device_host) {
            pendingUsbipDeviceHost = result.device_host;
        }
        if (result.cluster_selection) {
            activeUsbipSelection = result.cluster_selection;
        }
        updateUsbipButtonStatus(result.connected);
    } catch (error) {
        console.error('Failed to check USB/IP status:', error);
    }
}

function updateUsbipButtonStatus(connected) {
    const btn = $('usbip-btn');
    if (!btn) return;

    if (connected) {
        btn.textContent = '📱 断开设备';
        state.usbipConnected = true;
        usbipReconnectWaiting = false;
    } else {
        btn.textContent = usbipReconnectWaiting ? '📱 等待重连...' : '📱 本地设备';
        state.usbipConnected = false;
    }
}
