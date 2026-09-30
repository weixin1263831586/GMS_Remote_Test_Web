        // ==================== 客户端信息 ====================
        let clientInfo = { ip: 'unknown', username: 'unknown' };

        // 更新客户端显示
        function updateClientDisplay() {
            const identityEl = document.getElementById('client-identity');
            const deviceHostInput = document.getElementById('device-host');

            if (!identityEl) return;

            // 只有当用户名有效时才显示 username@ip
            if (clientInfo.username && clientInfo.username !== 'unknown') {
                const display = `${clientInfo.username}@${clientInfo.ip}`;
                identityEl.textContent = display;

                // 更新设备主机输入框
                if (deviceHostInput) {
                    deviceHostInput.value = display;
                    deviceHostInput.placeholder = "设备主机";
                }
            } else {
                // 只显示IP
                identityEl.textContent = clientInfo.ip || '检测中...';
                // 设备主机保持为空或显示占位符
                if (deviceHostInput) {
                    deviceHostInput.value = '';
                    deviceHostInput.placeholder = '等待客户端识别...';
                }
            }
        }

        // 初始化客户端信息
        async function initClientInfo() {
            try {
                // 获取IP
                const ipResp = await fetch('/api/users/current');
                const ipData = await ipResp.json();
                clientInfo.ip = ipData.ip;
                updateClientDisplay();

                // 检测用户名（按 IP 存储，避免跨 IP 混淆）
                const storageKey = `gms_username_${clientInfo.ip}`;
                const savedUser = localStorage.getItem(storageKey);
                if (savedUser && savedUser !== 'guest' && savedUser !== 'unknown') {
                    clientInfo.username = savedUser;
                } else {
                    const detectResp = await fetch('/api/users/detect', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ ip: clientInfo.ip })
                    });
                    const detectData = await detectResp.json();
                    if (detectData.success) {
                        clientInfo.username = detectData.username;
                        localStorage.setItem(storageKey, detectData.username);
                    } else {
                        if (typeof state !== 'undefined' && !state.usernameDetectShown) {
                            state.usernameDetectShown = true;
                            showUsernameDetectModal(clientInfo.ip);
                        }
                        return;
                    }
                }

                updateClientDisplay();

                // 已有本地缓存即已登记过: module load ≠ user mutation, 不再写服务器。
                if (savedUser && savedUser !== 'guest' && savedUser !== 'unknown') {
                    return;
                }

                // 记录到服务器（使用 /api/users/set-username 接口）
                const recordResp = await fetch('/api/users/set-username', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username: clientInfo.username, ip: clientInfo.ip })
                });
                await recordResp.json();

            } catch (e) {
                console.error('[ClientInfo]', e);
            }
        }

