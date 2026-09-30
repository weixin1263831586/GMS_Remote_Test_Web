        // ==================== 设备管理功能 ====================
        let devicesRefreshInterval = null;
        let devicesManagementLoadPromise = null;
        let devicesManagementLoadScope = '';
        let devicesManagementClusterMode = false;
        let allDevices = [];  // 存储所有设备数据用于排序
        let currentSort = { field: null, direction: 'asc' };

        function stopDevicesAutoRefresh() {
            if (devicesRefreshInterval) clearInterval(devicesRefreshInterval);
            devicesRefreshInterval = null;
        }

        function requestedDevicesManagementScope() {
            return window.GmsWorkspace?.get?.().scope_mode === 'cluster'
                ? 'cluster' : 'single';
        }

        async function loadDevicesManagement() {
            const requestedScope = requestedDevicesManagementScope();
            if (devicesManagementLoadPromise) {
                if (devicesManagementLoadScope === requestedScope) {
                    return devicesManagementLoadPromise;
                }
                // A mode switch happened while the old scope was loading.
                // Wait for it to settle, then start a fresh scoped request.
                try { await devicesManagementLoadPromise; } catch (_) {}
                return loadDevicesManagement();
            }
            const pending = loadDevicesManagementOnce(requestedScope);
            devicesManagementLoadPromise = pending;
            devicesManagementLoadScope = requestedScope;
            try {
                return await pending;
            } finally {
                if (devicesManagementLoadPromise === pending) {
                    devicesManagementLoadPromise = null;
                    devicesManagementLoadScope = '';
                }
            }
        }

        async function loadDevicesManagementOnce(requestedScope) {
            try {
                const includeCluster = requestedScope === 'cluster';
                const [resp, clusterResp, hostsResp, statusResp] = await Promise.all([
                    fetch('/api/devices/management', { cache: 'no-store' }),
                    includeCluster
                        ? fetch('/api/cluster/devices', { cache: 'no-store' }).catch(() => null)
                        : Promise.resolve(null),
                    includeCluster
                        ? fetch('/api/cluster/hosts', { cache: 'no-store' }).catch(() => null)
                        : Promise.resolve(null),
                    includeCluster
                        ? fetch('/api/cluster/status', { cache: 'no-store' }).catch(() => null)
                        : Promise.resolve(null)
                ]);
                const data = resp.ok ? await resp.json() : {};
                let loadedDevices = Array.isArray(data.devices)
                    ? data.devices
                    : Array.isArray(data.data) ? data.data : [];
                if (!resp.ok || data.success === false) {
                    console.warn('[Devices] Controller device scan failed:', data.warning || data.error || resp.statusText);
                }
                const statusData = statusResp && statusResp.ok ? await statusResp.json() : {};
                const localWorkerId = statusData.local_worker_id || workspaceLocalWorkerId();
                const clusterMode = Boolean(includeCluster && statusData.enabled);
                const scopeHint = document.getElementById('devices-scope-hint');
                // 为本地设备补全 worker_id，使按主机分组时本地和远端设备都能正确归类
                loadedDevices.forEach(d => {
                    if (!d.worker_id) d.worker_id = localWorkerId;
                });
                loadedDevices = loadedDevices.filter(device =>
                    !['offline', 'unknown'].includes(
                        String(device.status || '').toLowerCase()
                    )
                );
                const hostsData = hostsResp && hostsResp.ok ? await hostsResp.json() : {};
                if (includeCluster && Array.isArray(hostsData.hosts)) {
                    // Reuse the directory fetched for device rendering when an
                    // action immediately opens a desktop/terminal. This avoids
                    // another /api/cluster/hosts round trip on the button path.
                    clusterHostDirectory.hosts = hostsData.hosts;
                    clusterHostDirectory.loadedAt = Date.now();
                }
                const hostDirectory = new Map((hostsData.hosts || []).map(host => [
                    host.worker_id,
                    host
                ]));
                const hostNames = new Map((hostsData.hosts || []).map(host => [
                    host.worker_id,
                    host.worker_id
                ]));
                loadedDevices.forEach(device => {
                    device.host_display_name = hostNames.get(device.worker_id)
                        || device.source_host
                        || device.worker_id;
                });
                if (clusterMode && clusterResp && clusterResp.ok) {
                    const clusterData = await clusterResp.json();
                    const remoteDevices = (clusterData.devices || [])
                        .filter(device =>
                            device.worker_id !== localWorkerId
                            && !['offline', 'unknown'].includes(device.state)
                        )
                        .map(device => {
                            const host = hostDirectory.get(device.worker_id) || {};
                            const capabilities = host.capabilities || {};
                            const properties = device.properties || {};
                            const isAdbProxy = device.transport === 'adb_proxy';
                            const isUsbip = (
                                device.transport === 'usbip'
                                || properties.is_usbip === true
                            );
                            const sourceWorkerId = String(
                                properties.adb_proxy_source_worker_id || ''
                            );
                            const targetHostName = hostNames.get(device.worker_id)
                                || device.worker_id;
                            const sourceHostName = sourceWorkerId
                                ? (hostNames.get(sourceWorkerId) || sourceWorkerId)
                                : '来源未知';
                            return {
                                device_id: device.id,
                                serial_no: device.serial,
                                worker_id: device.worker_id,
                                host_display_name: targetHostName,
                                source_host: isAdbProxy
                                    ? `${sourceHostName} → ${targetHostName}`
                                    : isUsbip
                                    ? (properties.usbip_source_host || '来源未知')
                                    : targetHostName,
                                source_type: isAdbProxy
                                    ? 'adb_proxy'
                                    : isUsbip ? 'usbip' : 'cluster',
                                transport: device.transport || 'local_usb',
                                is_usbip: isUsbip,
                                usbip_source_host:
                                    properties.usbip_source_host || '',
                                adb_proxy_source_worker_id: sourceWorkerId,
                                adb_proxy_source_serial:
                                    properties.adb_proxy_source_serial || '',
                                status: ['offline', 'unknown'].includes(device.state) ? 'offline' : 'online',
                                cluster_state: device.state || 'unknown',
                                cluster_shell_available: Boolean(
                                    host.status !== 'offline' && host.address && host.ssh_user
                                ),
                                cluster_device_inspection: Boolean(capabilities.device_inspection),
                                model: properties.model || properties.product,
                                android_version: properties.android_version,
                                battery_level: properties.battery_level,
                                soc_model: properties.soc_model,
                                claimed: Boolean(device.claimed),
                                claim_source_type: device.claim_source_type || '',
                                locked_by: device.claimed
                                    ? device.claimed_by || ({
                                        'cluster-firmware': '固件烧写',
                                        'cluster-job': '测试任务',
                                        'cluster-device-action': '设备操作'
                                    })[device.claim_source_type] || '平台操作'
                                    : ({
                                    allocated: 'Cluster 租约',
                                    reserved: 'ATS 预留',
                                    external_busy: '外部 Tradefed'
                                })[device.state] || '',
                                cluster_readonly: true
                            };
                        });
                    loadedDevices = loadedDevices.concat(remoteDevices);
                }

                // Do not apply a response after the user changed mode while it
                // was in flight. The scoped loader will immediately issue the
                // replacement request for the new mode.
                // 每次刷新都同步最新分组定义（后端持续自动补全会改动 auto 分组，
                // 不同步会导致新接入设备虽在 allDevices 中却不进组/不显示）
                await mgmtLoadGroups();
                // Group loading is another asynchronous boundary. Publish the
                // inventory only after re-checking scope so a completed cluster
                // request can never flash remote devices in single mode.
                if (requestedDevicesManagementScope() !== requestedScope) return;
                allDevices = loadedDevices;
                devicesManagementClusterMode = clusterMode;
                state.deviceGroupsLoaded = true;
                displayDevicesManagement(filteredDevicesForView());
                if (clusterMode || state.deviceGroups.length > 0) {
                    setupMgmtGroupDelegation();
                }

                // 启动自动刷新（每10秒）
                if (currentPage === 'devices' && !devicesRefreshInterval) {
                    devicesRefreshInterval = setInterval(() => {
                        if (currentPage === 'devices' && !document.hidden) {
                            loadDevicesManagement();
                        }
                    }, 10000);
                }
            } catch (e) {
                console.error('[Devices] Error loading devices:', e);
            }
        }

        function sortDevices(field) {
            // 切换排序方向
            if (currentSort.field === field) {
                currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
            } else {
                currentSort.field = field;
                currentSort.direction = 'asc';
            }

            // 更新排序指示器
            document.querySelectorAll('[id^="sort-"]').forEach(el => {
                el.textContent = '↕';
            });
            document.getElementById(`sort-${field}`).textContent = currentSort.direction === 'asc' ? '↑' : '↓';

            // 排序设备列表
            const sortedDevices = [...allDevices].sort((a, b) => {
                let aVal = a[field] || '';
                let bVal = b[field] || '';

                // 特殊处理source_type、status和battery_level
                if (field === 'source_type') {
                    aVal = a.source_type === 'local' ? '0' : '1';
                    bVal = b.source_type === 'local' ? '0' : '1';
                } else if (field === 'status') {
                    aVal = a.status === 'online' ? '0' : '1';
                    bVal = b.status === 'online' ? '0' : '1';
                } else if (field === 'battery_level') {
                    // 电池电量按数值排序
                    aVal = parseInt(a.battery_level) || 0;
                    bVal = parseInt(b.battery_level) || 0;
                    const result = aVal - bVal;
                    return currentSort.direction === 'asc' ? result : -result;
                }

                const result = aVal.localeCompare(bVal, undefined, { numeric: true });
                return currentSort.direction === 'asc' ? result : -result;
            });

            // 统一走 filteredDevicesForView（内部已按 currentSort 排序，并应用分组筛选）
            displayDevicesManagement(filteredDevicesForView());
        }

        function displayDevicesManagement(devices) {
            const tbody = document.getElementById('devices-table-body');
            if (!tbody) return;

            // 更新统计数据
            const totalCount = allDevices.length;
            const localCount = allDevices.filter(d =>
                !d.cluster_readonly
                && d.worker_id === workspaceLocalWorkerId()
            ).length;
            const usbipCount = allDevices.filter(d => d.source_type === 'usbip').length;
            const clusterCount = allDevices.filter(d => d.cluster_readonly).length;
            const fastbootCount = allDevices.filter(d =>
                d.protocol === 'fastboot'
                || d.status === 'fastboot'
                || d.cluster_state === 'fastboot'
            ).length;

            document.getElementById('total-devices-count').textContent = totalCount;
            document.getElementById('local-devices-count').textContent = localCount;
            document.getElementById('usbip-devices-count').textContent = usbipCount;
            document.getElementById('fastboot-devices-count').textContent = fastbootCount;
            document.getElementById('cluster-devices-count').textContent = clusterCount;

            if (devices.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="10" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                            暂无设备
                        </td>
                    </tr>
                `;
                return;
            }

            // 渲染设备列表
            const renderDeviceRow = (device) => {
                const isClusterRemote = Boolean(device.cluster_readonly);
                const serialNo = String(device.serial_no || device.device_id || '');
                // serial_no is presentation metadata (ro.serialno). ADB actions
                // must use the inventory transport id, which can be IP:port or
                // another value that differs from ro.serialno.
                const actionDeviceId = String(device.device_id || serialNo);
                const serialAttr = escapeIconAttr(actionDeviceId);
                const workerAttr = escapeIconAttr(device.worker_id || workspaceLocalWorkerId());
                const actionButtonStyle = 'width: 92px; min-width: 92px; font-size: 12px; padding: 4px 8px;';
                const releaseButton = device.cluster_readonly
                    ? `<span style="visibility: hidden; width: 92px; min-width: 92px;"></span>`
                    : device.locked_by
                    ? `<button class="btn-xxs" data-serial="${serialAttr}" data-click="forceReleaseDeviceLock" data-r0="dataset.serial" title="强制释放该设备的平台占用锁" style="background: var(--danger-color); color: white; ${actionButtonStyle}">释放设备</button>`
                    : `<span style="visibility: hidden; width: 92px; min-width: 92px;"></span>`;
                const sourceTypeBadge = device.source_type === 'adb_proxy'
                    ? '<span style="background:rgba(156,39,176,.16);color:#ce93d8;padding:2px 6px;border-radius:3px;font-size:12px;font-weight:600;">ADB Proxy</span>'
                    : device.source_type === 'cluster'
                    ? `<span style="background:rgba(33,150,243,.15);color:#42a5f5;padding:2px 6px;border-radius:3px;font-size:12px;font-weight:600;">${escapeHtml(device.worker_id)}</span>`
                    : device.source_type === 'local'
                    ? '<span style="background: rgba(76, 175, 80, 0.2); color: var(--success-color); padding: 2px 6px; border-radius: 3px; font-size: 12px; font-weight: 600;">测试主机</span>'
                    : '<span style="background: rgba(255, 152, 0, 0.2); color: var(--warning-color); padding: 2px 6px; border-radius: 3px; font-size: 12px; font-weight: 600;">USB/IP</span>';

                const clusterStatus = {
                    available: ['var(--success-color)', '● 可用'],
                    allocated: ['var(--warning-color)', '● 已分配'],
                    reserved: ['#ab47bc', '● 已预留'],
                    external_busy: ['var(--danger-color)', '● 外部占用'],
                    fastboot: ['#42a5f5', '● Fastboot'],
                    unauthorized: ['var(--danger-color)', '● 未授权'],
                    offline: ['var(--text-secondary)', '○ 离线'],
                    unknown: ['var(--text-secondary)', '○ 未知']
                }[device.cluster_state];
                const localStatus = {
                    fastboot: ['#42a5f5', '● Fastboot'],
                    unauthorized: ['var(--danger-color)', '● 未授权'],
                    offline: ['var(--text-secondary)', '○ 离线']
                }[device.status];
                const statusBadge = clusterStatus
                    ? `<span style="color: ${clusterStatus[0]}; font-weight: 600; font-size: 12px;">${clusterStatus[1]}</span>`
                    : localStatus
                    ? `<span style="color: ${localStatus[0]}; font-weight: 600; font-size: 12px;">${localStatus[1]}</span>`
                    : device.locked_by
                    ? '<span style="color: var(--warning-color); font-weight: 600; font-size: 12px;">● 已分配</span>'
                    : device.status === 'online'
                    ? '<span style="color: var(--success-color); font-weight: 600; font-size: 12px;">● 可用</span>'
                    : '<span style="color: var(--text-secondary); font-weight: 600; font-size: 12px;">○ 离线</span>';

                // 电池电量显示
                let batteryDisplay = '-';
                if (device.battery_level) {
                    const level = parseInt(device.battery_level);
                    if (!isNaN(level)) {
                        let batteryColor = 'var(--success-color)';
                        let batteryIcon = '🔋';
                        if (level < 20) {
                            batteryColor = 'var(--danger-color)';
                            batteryIcon = '🪫';
                        } else if (level < 50) {
                            batteryColor = 'var(--warning-color)';
                        }
                        batteryDisplay = `<span style="color: ${batteryColor}; font-weight: 600; font-size: 12px;">${batteryIcon} ${level}%</span>`;
                    }
                }

                // SOC 标签显示
                const socDisplay = device.soc_model
                    ? `<span style="background: rgba(33, 150, 243, 0.15); color: #42a5f5; padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; font-family: monospace;">${escapeHtml(String(device.soc_model))}</span>`
                    : '-';

                const isFastboot = device.protocol === 'fastboot'
                    || device.status === 'fastboot'
                    || device.cluster_state === 'fastboot';
                const clusterWorkerAttr = escapeIconAttr(device.worker_id || '');
                const clusterFullId = actionDeviceId.startsWith(`${device.worker_id || ''}:`)
                    ? actionDeviceId
                    : `${device.worker_id || ''}:${actionDeviceId}`;
                const clusterFullIdAttr = escapeIconAttr(clusterFullId);
                const clusterInspectable = !device.claimed
                    && !['offline', 'unknown', 'unauthorized', 'fastboot'].includes(device.cluster_state);
                const clusterMutable = device.cluster_state === 'available';
                const localClaimed = !isClusterRemote && Boolean(device.locked_by);
                const localClaimedByOther = localClaimed && !device.locked_by_self;
                const shellDisabled = isClusterRemote && !(clusterMutable && device.cluster_shell_available)
                    ? ' disabled' : '';
                const remoteInspectionDisabled = isClusterRemote
                    && !(clusterInspectable && device.cluster_device_inspection) ? ' disabled' : '';
                const remoteControlDisabled = isClusterRemote
                    && !(clusterMutable && device.cluster_device_inspection) ? ' disabled' : '';
                const adbShellDisabled = Boolean(shellDisabled || (!isClusterRemote && (isFastboot || localClaimed)));
                const adbInspectionDisabled = Boolean(remoteInspectionDisabled || (!isClusterRemote && (isFastboot || localClaimedByOther)));
                const adbControlDisabled = Boolean(remoteControlDisabled || (!isClusterRemote && (isFastboot || localClaimedByOther)));
                const shellStyle = adbShellDisabled ? 'opacity:.45;cursor:not-allowed;' : '';
                const remoteInspectionStyle = adbInspectionDisabled ? 'opacity:.45;cursor:not-allowed;' : '';
                const remoteControlStyle = adbControlDisabled ? 'opacity:.45;cursor:not-allowed;' : '';
                const inspectionTitle = device.claimed
                    ? '设备已被测试或平台操作占用，Device Info 会在释放后可用'
                    : device.cluster_device_inspection
                    ? '查看远程设备信息：configs / packages / features / props'
                    : 'Worker Agent 版本过旧或未上报 device_inspection 能力';
                const controlTitle = device.cluster_device_inspection
                    ? '仅可操控当前可用的集群设备'
                    : 'Worker Agent 版本过旧或未上报 device_inspection 能力';
                const localShellTitle = isFastboot
                    ? 'Fastboot 状态下不可使用 adb shell'
                    : localClaimed ? '设备已被测试或平台操作占用，释放后可打开 ADB Shell'
                    : '打开 adb shell';
                const localInspectionTitle = isFastboot
                    ? 'Fastboot 状态下不可读取 ADB 设备信息'
                    : localClaimedByOther ? '设备已被其他用户或任务占用'
                    : '查看设备信息：configs 配置资源 / packages 已装包 / features 特性 / 系统属性';
                const localControlTitle = isFastboot
                    ? 'Fastboot 状态下不可使用 UI 操控'
                    : localClaimedByOther ? '设备已被其他用户或任务占用'
                    : '智能 UI 操控：截图 + 布局元素 + 点按';
                const shellButton = isClusterRemote
                    ? `<button class="btn-xxs" data-serial="${clusterFullIdAttr}" data-worker="${clusterWorkerAttr}" data-click="openClusterDeviceShell" data-r0="dataset.serial" data-r1="dataset.worker" title="仅可对具备 SSH 元数据的可用 Worker 设备打开 adb shell" style="background: var(--primary-color); ${actionButtonStyle}${shellStyle}"${shellDisabled}>🐧 adb shell</button>`
                    : `<button class="btn-xxs" data-serial="${serialAttr}" data-click="openDeviceShell" data-r0="dataset.serial" title="${localShellTitle}" style="background: var(--primary-color); ${actionButtonStyle}${shellStyle}"${adbShellDisabled ? ' disabled' : ''}>🐧 adb shell</button>`;
                const deviceInfoButton = isClusterRemote
                    ? `<button class="btn-xxs" data-serial="${clusterFullIdAttr}" data-worker="${clusterWorkerAttr}" data-click="openClusterDeviceInfo" data-r0="dataset.serial" data-r1="dataset.worker" title="${inspectionTitle}" style="background: var(--success-color); color: white; ${actionButtonStyle}${remoteInspectionStyle}"${remoteInspectionDisabled}>ℹ️ device info</button>`
                    : `<button class="btn-xxs" data-serial="${serialAttr}" data-worker="${workerAttr}" data-click="openDeviceConfigExplorer" data-r0="dataset.serial" data-r1="dataset.worker" data-testid="device-config-open" title="${localInspectionTitle}" style="background: var(--success-color); color: white; ${actionButtonStyle}${remoteInspectionStyle}"${adbInspectionDisabled ? ' disabled' : ''}>ℹ️ device info</button>`;
                const uiControlButton = isClusterRemote
                    ? `<button class="btn-xxs" data-serial="${clusterFullIdAttr}" data-worker="${clusterWorkerAttr}" data-click="openClusterDeviceUiControl" data-r0="dataset.serial" data-r1="dataset.worker" title="${controlTitle}" style="background: var(--accent-color, #6c5ce7); color: white; ${actionButtonStyle}${remoteControlStyle}"${remoteControlDisabled}>🎯 UI 操控</button>`
                    : `<button class="btn-xxs" data-serial="${serialAttr}" data-click="openUiControl" data-r0="dataset.serial" title="${localControlTitle}" style="background: var(--accent-color, #6c5ce7); color: white; ${actionButtonStyle}${remoteControlStyle}"${adbControlDisabled ? ' disabled' : ''}>🎯 UI 操控</button>`;

                return `
                    <tr style="border-bottom: 1px solid var(--border-color);">
                        <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;">
                            ${escapeHtml(String(device.serial_no || '-'))}
                        </td>
                        <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;">
                            ${escapeHtml(String(device.source_host || '-'))}
                        </td>
                        <td style="padding: 4px 6px; text-align: center;">
                            ${sourceTypeBadge}
                        </td>
                        <td style="padding: 4px 6px; text-align: center; font-size: 12px;">
                            ${escapeHtml(String(device.model || '-'))}
                        </td>
                        <td style="padding: 4px 6px; text-align: center;">
                            ${socDisplay}
                        </td>
                        <td style="padding: 4px 6px; text-align: center; font-size: 12px;">
                            ${escapeHtml(String(device.android_version || '-'))}
                        </td>
                        <td style="padding: 4px 6px; text-align: center;">
                            ${batteryDisplay}
                        </td>
                        <td style="padding: 4px 6px; text-align: center;">
                            ${statusBadge}
                        </td>
                        <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;">
                            ${escapeHtml(String(device.locked_by || '-'))}
                        </td>
                        <td style="padding: 4px 6px; text-align: center; white-space: nowrap;">
                            <div style="display: grid; grid-template-columns: repeat(4, 92px); gap: 6px; justify-content: center; align-items: center;">
                                ${shellButton}
                                ${deviceInfoButton}
                                ${uiControlButton}
                                ${releaseButton}
                            </div>
                        </td>
                    </tr>
                `;
            };

            // 集群模式默认按实际接入 Worker 分段。选择自定义分组筛选后，
            // 仍按用户分组渲染，避免覆盖既有分组配置。
            if (
                devicesManagementClusterMode
                && !state.groupFilter
            ) {
                tbody.innerHTML = renderDevicesManagementByHost(devices, renderDeviceRow);
            } else if (state.deviceGroups.length > 0) {
                tbody.innerHTML = renderDevicesManagementGrouped(devices, renderDeviceRow);
            } else {
                tbody.innerHTML = devices.map(renderDeviceRow).join('');
            }
        }

