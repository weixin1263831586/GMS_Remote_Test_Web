        // ==================== 智能 UI 操控（截图 + 布局元素 + 点按）====================
        let uiControlSerial = null;
        let uiControlElements = [];
        let uiControlRefreshId = 0;

        function clusterManagedDevice(fullSerial, workerId) {
            const rawSerial = String(fullSerial || '').startsWith(`${workerId}:`)
                ? String(fullSerial).slice(workerId.length + 1)
                : String(fullSerial || '');
            return allDevices.find(item =>
                item.cluster_readonly
                && item.worker_id === workerId
                && (item.serial_no === rawSerial
                    || item.device_id === `${workerId}:${rawSerial}`)
            );
        }

        async function openClusterDeviceShell(fullSerial, workerId) {
            if (requestedDevicesManagementScope() !== 'cluster') {
                showToast('当前为单机模式，请切换到集群模式后再打开远程 ADB Shell', 'warning');
                return;
            }
            const managed = clusterManagedDevice(fullSerial, workerId);
            if (!managed || managed.cluster_state !== 'available' || !managed.cluster_shell_available) {
                showToast('该远程设备或 Worker 当前不可用于 ADB Shell，请刷新设备列表', 'warning');
                return;
            }
            await openDeviceShell(fullSerial, workerId);
        }
        window.openClusterDeviceShell = openClusterDeviceShell;

        async function openClusterDeviceInfo(fullSerial, workerId) {
            if (!fullSerial || !workerId || isLocalWorkspaceWorker(workerId)) {
                showToast('远程设备信息需要有效的 Worker', 'error');
                return;
            }
            if (requestedDevicesManagementScope() !== 'cluster') {
                showToast('当前为单机模式，请切换到集群模式后再查看远程 Device Info', 'warning');
                return;
            }
            const managed = clusterManagedDevice(fullSerial, workerId);
            if (!managed) {
                showToast('远程设备已不在当前 Worker 清单中，请刷新设备列表', 'warning');
                return;
            }
            if (!managed.cluster_device_inspection) {
                showToast(`${workerId} 的 Worker Agent 版本过旧，请在主机集群页重新部署后再查看 Device Info`, 'warning');
                return;
            }
            if (managed.claimed) {
                showToast('设备正被测试或平台操作占用，请释放后再查看 Device Info', 'warning');
                return;
            }
            const rawSerial = String(fullSerial).startsWith(`${workerId}:`)
                ? String(fullSerial).slice(workerId.length + 1)
                : String(fullSerial);
            // Device Info 是独立的 inspection 上下文：只维护 dcfg* 状态，
            // 绝不修改 state.selectedDevices / 全局测试工作区（否则在
            // Worker A 测试页选中的设备会被 Worker B 的 Device Info 覆盖，
            // 随后 startTest 会提交跨 Worker 的 devices 组合）。
            await openDeviceConfigExplorer(`${workerId}:${rawSerial}`, workerId);
        }
        window.openClusterDeviceInfo = openClusterDeviceInfo;

        async function openClusterDeviceUiControl(serial, workerId) {
            if (!serial || !workerId || isLocalWorkspaceWorker(workerId)) {
                showToast('远程设备操控需要有效的 Worker 信息', 'error');
                return;
            }
            if (requestedDevicesManagementScope() !== 'cluster') {
                showToast('当前为单机模式，请切换到集群模式后再操控远程设备', 'warning');
                return;
            }
            const managed = clusterManagedDevice(serial, workerId);
            if (!managed || managed.cluster_state !== 'available') {
                showToast('远程设备当前不可操控，请刷新设备列表', 'warning');
                return;
            }
            if (!managed.cluster_device_inspection) {
                showToast(`${workerId} 的 Worker Agent 版本过旧，请在主机集群页重新部署后再使用 UI 操控`, 'warning');
                return;
            }
            const fullSerial = serial.startsWith(`${workerId}:`)
                ? serial
                : `${workerId}:${serial}`;
            // UI 操控以浮层展示，保持在设备管理页面：它只是 inspection 上下文，
            // 绝不修改全局 Test Workspace（worker_id / device_ids），
            // 也不触碰 state.selectedDevices——否则当前 Worker A 的测试上下文
            // 会被 Worker B 的 UI 操控静默切走。openUiControl 自己持有
            // uiControlSerial/uiControlRefreshId，按设备直连 API，无需全局状态。
            await openUiControl(fullSerial);
        }
        window.openClusterDeviceUiControl = openClusterDeviceUiControl;

        async function openUiControl(serialNo) {
            if (!serialNo || serialNo === '-') {
                showToast('设备序列号无效', 'error');
                return;
            }
            uiControlSerial = serialNo;
            uiControlElements = [];
            const serialEl = document.getElementById('ui-control-serial');
            if (serialEl) serialEl.textContent = `· ${serialNo}`;
            ModalManager.open('ui-control-modal');
            await refreshUiControl();
        }

        function closeUiControl() {
            ModalManager.close('ui-control-modal');
            uiControlSerial = null;
            uiControlElements = [];
            uiControlRefreshId += 1;
        }

        // 远端 device action 统一等待：Controller 等待窗口超时返回
        // accepted + command_id 时继续轮询命令终态，避免把 accepted
        // 误当最终数据（截图/布局/包列表显示为空）。
        async function executeClusterCommandAndWait(payload, pollContext = null) {
            const result = await apiCall('/api/cluster/devices/actions', 'POST', payload);
            if (!result || !result.accepted) return result;
            const commandId = result.command_id;
            const deadline = Date.now() + 200000;
            while (Date.now() < deadline) {
                if (pollContext && !pollContext()) {
                    throw new Error('操作已取消（页面已切换）');
                }
                await new Promise(resolve => setTimeout(resolve, 2000));
                const status = await apiCall(
                    `/api/cluster/commands/${encodeURIComponent(commandId)}`
                );
                const command = status.command || {};
                if (command.status === 'completed') {
                    return command.result || {};
                }
                if (['failed', 'cancelled'].includes(command.status)) {
                    throw new Error(command.error || '远端设备操作失败');
                }
            }
            throw new Error('远端设备操作超时');
        }

        async function refreshUiControl() {
            if (!uiControlSerial) return;
            const refreshId = ++uiControlRefreshId;
            const serial = uiControlSerial;
            const managedDevice = allDevices.find(item => item.device_id === serial || item.serial_no === serial);
            const workerId = managedDevice?.worker_id || (serial.includes(':') ? serial.split(':', 1)[0] : '');
            const isRemote = Boolean(workerId && !isLocalWorkspaceWorker(workerId));
            const statusEl = document.getElementById('ui-control-status');
            const imgEl = document.getElementById('ui-control-screenshot');
            const listEl = document.getElementById('ui-control-elements');
            if (statusEl) statusEl.textContent = '正在截图并解析布局...';
            if (listEl) listEl.innerHTML = '<div style="padding:20px;color:var(--text-secondary);text-align:center;">加载中...</div>';
            // 截图和布局独立结算：其中一项失败时，另一项仍正常展示。
            // accepted 异步结果统一由 executeClusterCommandAndWait 轮询。
            const pollAlive = () => refreshId === uiControlRefreshId && serial === uiControlSerial;
            const [shotResult, layoutResult] = await Promise.allSettled([
                isRemote
                    ? executeClusterCommandAndWait({worker_id: workerId, devices: [serial], action: 'screenshot'}, pollAlive)
                    : apiCall('/api/devices/ui/screenshot', 'POST', { serial }),
                isRemote
                    ? executeClusterCommandAndWait({worker_id: workerId, devices: [serial], action: 'layout'}, pollAlive)
                    : apiCall('/api/devices/ui/layout', 'POST', { serial }),
            ]);
            if (refreshId !== uiControlRefreshId || serial !== uiControlSerial) return;

            const errors = [];
            if (shotResult.status === 'fulfilled' && shotResult.value?.success && imgEl) {
                imgEl.src = shotResult.value.image;
            } else {
                errors.push(`截图失败：${shotResult.reason?.message || shotResult.value?.error || '未知错误'}`);
            }

            if (layoutResult.status === 'fulfilled' && layoutResult.value?.success) {
                const layoutRes = layoutResult.value;
                uiControlElements = layoutRes.elements || [];
                const actionableCount = uiControlElements.filter(isUiControlActionable).length;
                const source = layoutRes.source === 'uiautomator2' ? 'UIAutomator2' : 'Android CLI';
                if (statusEl) statusEl.textContent = `已加载 ${uiControlElements.length} 个元素，${actionableCount} 个可操作 · ${source}`;
                renderUiControlElements();
            } else {
                uiControlElements = [];
                errors.push(`布局失败：${layoutResult.reason?.message || layoutResult.value?.error || '未知错误'}`);
                renderUiControlElements();
            }
            if (errors.length && statusEl) statusEl.textContent = errors.join('；');
        }

        function isUiControlActionable(el) {
            const interactions = el.interactions || [];
            return Boolean(el.center && el.enabled !== false && interactions.some(value =>
                ['clickable', 'long_clickable', 'checkable', 'focusable', 'editable'].includes(value)
            ));
        }

        function renderUiControlElements(elements = uiControlElements) {
            const listEl = document.getElementById('ui-control-elements');
            if (!listEl) return;
            const query = (document.getElementById('ui-control-search')?.value || '').trim().toLowerCase();
            const actionableOnly = document.getElementById('ui-control-actionable-only')?.checked ?? true;
            elements = elements.filter(el => {
                if (actionableOnly && !isUiControlActionable(el)) return false;
                if (!query) return true;
                return [el.text, el.content_desc, el.resource_id, el.class_name]
                    .some(value => String(value || '').toLowerCase().includes(query));
            });
            if (!elements.length) {
                listEl.innerHTML = '<div style="padding:20px;color:var(--text-secondary);text-align:center;">没有符合条件的 UI 元素，可取消“仅可操作”或修改搜索词</div>';
                return;
            }
            // 可点击的排前面，便于操作。
            const sorted = [...elements].sort((a, b) => {
                const ac = (a.interactions || []).includes('clickable') ? 0 : 1;
                const bc = (b.interactions || []).includes('clickable') ? 0 : 1;
                return ac - bc;
            });
            listEl.innerHTML = sorted.map((el, idx) => {
                const label = el.text || el.resource_id || '(无文本)';
                const c = el.center ? `${el.center[0]},${el.center[1]}` : '-';
                const actionable = isUiControlActionable(el);
                const tag = actionable ? '👆' : '•';
                const type = String(el.class_name || '').split('.').pop();
                return `<div class="ui-ctrl-el" data-x="${el.center ? el.center[0] : ''}" data-y="${el.center ? el.center[1] : ''}" style="padding:8px 10px;border-bottom:1px solid var(--border-color);cursor:${el.center ? 'pointer' : 'default'};font-size:12px;" class="hover-soft">
                    <div>${tag} ${escapeHtml(String(label)).slice(0, 80)} <span style="color:var(--text-secondary);">[${escapeHtml(c)}]</span></div>
                    <div style="color:var(--text-secondary);font-size:11px;margin-top:2px;">${escapeHtml(type)}${el.resource_id ? ` · ${escapeHtml(el.resource_id)}` : ''}</div>
                </div>`;
            }).join('');
            // 绑定点按：仅有点击有坐标的行。
            listEl.querySelectorAll('.ui-ctrl-el').forEach(row => {
                const x = row.dataset.x, y = row.dataset.y;
                if (x === '' || y === '') return;
                row.addEventListener('click', () => uiControlTap(Number(x), Number(y)));
            });
        }

        async function uiControlTap(x, y) {
            if (!uiControlSerial) return;
            const statusEl = document.getElementById('ui-control-status');
            if (statusEl) statusEl.textContent = `点按 (${x}, ${y}) 中...`;
            try {
                const managedDevice = allDevices.find(item => item.device_id === uiControlSerial || item.serial_no === uiControlSerial);
                const workerId = managedDevice?.worker_id || (uiControlSerial.includes(':') ? uiControlSerial.split(':', 1)[0] : '');
                const res = workerId && !isLocalWorkspaceWorker(workerId)
                    ? await apiCall('/api/cluster/devices/actions', 'POST', {worker_id: workerId, devices: [uiControlSerial], action: 'tap', x, y})
                    : await apiCall('/api/devices/ui/tap', 'POST', { serial: uiControlSerial, x, y });
                if (res && res.success) {
                    // 点按后等界面刷新，再重新截图+解析。
                    setTimeout(refreshUiControl, 600);
                } else {
                    if (statusEl) statusEl.textContent = `点按失败: ${(res && (res.error || res.detail)) || '未知'}`;
                }
            } catch (err) {
                if (statusEl) statusEl.textContent = `点按失败: ${err.message || err}`;
            }
        }

