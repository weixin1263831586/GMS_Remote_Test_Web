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

        // ==================== 主机管理功能 ====================
        let desktopHosts = [];
        let currentHost = null;
        const CLUSTER_HOST_DIRECTORY_TTL_MS = 10000;
        let clusterHostDirectory = {hosts: [], loadedAt: 0, promise: null};

        function createTerminalTheme() {
            return {
                background: '#000000',
                foreground: '#ffffff',
                cursor: '#ffffff',
                cursorAccent: '#000000',
                // xterm 默认灰色选区会压低 ANSI 彩色文本的对比度；固定前景色后，
                // 鼠标拖选路径和命令时仍能清楚辨认内容及选区边界。
                selectionBackground: 'rgba(124, 92, 255, 0.55)',
                selectionForeground: '#ffffff',
                selectionInactiveBackground: 'rgba(124, 92, 255, 0.35)'
            };
        }

        function workspaceHostForWorker(workerId = '') {
            const requested = workerId || window.GmsWorkspace?.get?.().worker_id || workspaceLocalWorkerId();
            return isLocalWorkspaceWorker(requested)
                ? desktopHosts.find(host => host.id === 'default')
                : desktopHosts.find(host => host.worker_id === requested);
        }

        async function loadClusterHostDirectory(force = false) {
            const fresh = clusterHostDirectory.loadedAt
                && Date.now() - clusterHostDirectory.loadedAt < CLUSTER_HOST_DIRECTORY_TTL_MS;
            if (!force && fresh) return clusterHostDirectory.hosts;
            if (clusterHostDirectory.promise) {
                const pending = clusterHostDirectory.promise;
                if (!force) return pending;
                // A forced refresh must observe registrations that happened
                // after an older request started. Wait for that request, then
                // issue a new one instead of inheriting its stale result.
                try {
                    await pending;
                } catch (_) {
                    // The forced request below is the recovery attempt.
                }
                if (clusterHostDirectory.promise && clusterHostDirectory.promise !== pending) {
                    return clusterHostDirectory.promise;
                }
            }
            clusterHostDirectory.promise = (async () => {
                const response = await fetch('/api/cluster/hosts', {cache: 'no-store'});
                const payload = await response.json();
                if (!response.ok || !payload.success || !Array.isArray(payload.hosts)) {
                    throw new Error(payload.error || '主机目录加载失败');
                }
                clusterHostDirectory.hosts = payload.hosts;
                clusterHostDirectory.loadedAt = Date.now();
                return clusterHostDirectory.hosts;
            })().finally(() => {
                clusterHostDirectory.promise = null;
            });
            return clusterHostDirectory.promise;
        }
        window.loadClusterHostDirectory = loadClusterHostDirectory;

        let terminalElevationPromise = null;
        async function ensureTerminalElevation(
            force = false,
            actionLabel = '打开主机终端',
            resourceLabel = '主机终端'
        ) {
            if (!state.authReady) {
                debugLog('[Elevation] skipped while authentication state is pending');
                return false;
            }
            if (!state.currentUser && state.authRequired) {
                showAuthGate(false);
                return false;
            }
            if (force) {
                state.elevated = false;
                state.elevatedUntil = null;
            }
            if (state.elevated) return true;
            // Recover a server grant created before this tab updated its local
            // state. Expired local grants are rejected by the protected API
            // and handled by recoverTerminalElevation.
            if (!force) {
                try {
                    const status = await fetchAuthStatus();
                    if (status.elevated) {
                        _markElevated(status.elevated_until);
                        return true;
                    }
                    state.elevated = false;
                    state.elevatedUntil = null;
                } catch (error) {
                    debugLog('[Elevation] status verification failed; protected request will verify it', error);
                }
            }
            if (!terminalElevationPromise) {
                terminalElevationPromise = requestElevatedAccess(actionLabel).finally(() => {
                    terminalElevationPromise = null;
                });
            }
            return terminalElevationPromise;
        }

        function recoverTerminalElevation(instance, actionLabel, reconnect) {
            if (!instance || instance.disposed || instance.elevationRecoveryPending) return;
            instance.elevationRecoveryPending = true;
            ensureTerminalElevation(true, actionLabel)
                .then(granted => {
                    if (granted && !instance.disposed) reconnect();
                })
                .finally(() => { instance.elevationRecoveryPending = false; });
        }

        function updateWorkspaceForHost(host, originPage) {
            if (!host) return;
            const workerId = host.worker_id || workspaceLocalWorkerId();
            window.GmsWorkspace?.update({
                worker_id: workerId,
                origin_page: originPage
            }, {source: `${originPage}-host`});
        }

        async function requestNovncAccess(workerId = '') {
            const result = await apiCall('/api/desktop/novnc/access', 'POST', {
                worker_id: workerId || workspaceLocalWorkerId()
            });
            if (!result.success || !result.url) {
                throw new Error(result.error || result.detail || '无法获取桌面访问授权');
            }
            return result.url;
        }

        // 初始化主机列表。单机模式可先使用本机条目挂载桌面，并行刷新
        // 集群目录；集群模式仍等待完整目录，避免错误主机闪现。
        let desktopHostsLoadPromise = null;
        async function initDesktopHosts() {
            if (desktopHostsLoadPromise) return desktopHostsLoadPromise;
            desktopHostsLoadPromise = (async () => {
            desktopHosts = [];

            // 确保默认主机（测试主机）始终存在且在列表首位
            // 外置脚本不经 Jinja 渲染：从模板已渲染的连接标签读取默认
            // 主机，缺失时退回 bootstrap 链的 localWorkerId（单一真值）。
            const defaultHost = (document.getElementById('terminal-connection-label') || {}).textContent
                || window.__GMS_BOOTSTRAP__?.localWorkerId
                || '';
            const defaultIndex = desktopHosts.findIndex(h => h.id === 'default');

            if (defaultIndex === -1) {
                // 不存在默认主机，添加到首位
                desktopHosts.unshift({
                    id: 'default',
                    name: defaultHost,
                    connection: defaultHost
                });
            } else if (defaultIndex !== 0) {
                // 默认主机存在但不在首位，移到首位
                const defaultHostObj = desktopHosts.splice(defaultIndex, 1)[0];
                desktopHosts.unshift(defaultHostObj);
            }
            const normalizedDefaultHost = desktopHosts.find(h => h.id === 'default');
            if (normalizedDefaultHost) {
                normalizedDefaultHost.name = defaultHost;
                normalizedDefaultHost.connection = defaultHost;
            }

            const contextHost = workspaceHostForWorker();
            if (contextHost) {
                currentHost = contextHost;
            } else {
                // 默认选择第一个主机（通常是测试主机）
                currentHost = desktopHosts[0];
            }

            await mergeClusterDesktopHosts();
            })();
            try {
                await desktopHostsLoadPromise;
            } finally {
                desktopHostsLoadPromise = null;
            }
        }

        async function mergeClusterDesktopHosts(force = false) {
            try {
                const hosts = await loadClusterHostDirectory(force);
                const selectedId = currentHost && currentHost.id;
                const clusterHosts = hosts
                    .filter(host => host.address && host.ssh_user)
                    .map(host => ({
                        id: `cluster:${host.worker_id}`,
                        worker_id: host.worker_id,
                        name: `${host.worker_id}${host.status === 'offline' ? '（离线）' : ''}`,
                        connection: `${host.ssh_user}@${host.address}`,
                        cluster_managed: true,
                        offline: host.status === 'offline'
                    }));
                // Keep the stable default id, but resolve the Controller through
                // the cluster directory so it shows its reachable LAN address
                // instead of the loopback bootstrap fallback.
                const localAlias = clusterHosts.find(host => host.worker_id === workspaceLocalWorkerId());
                const retained = desktopHosts.filter(host => !host.cluster_managed);
                const defaultEntry = retained.find(host => host.id === 'default');
                if (defaultEntry && localAlias) {
                    defaultEntry.name = localAlias.name;
                    defaultEntry.connection = localAlias.connection;
                    defaultEntry.worker_id = localAlias.worker_id;
                    defaultEntry.offline = localAlias.offline;
                }
                desktopHosts = retained.concat(clusterHosts.filter(host => !localAlias || host.id !== localAlias.id));
                const contextHost = workspaceHostForWorker();
                currentHost = contextHost || desktopHosts.find(host => host.id === selectedId) || currentHost || desktopHosts[0];
                if (contextHost && window.hostWorkspaceInitialized && hostWorkspace.layout === 'single' && hostWorkspace.panes.length) {
                    hostWorkspace.panes[0] = {type: 'desktop', hostId: contextHost.id};
                }
                if (contextHost && window.terminalWorkspaceInitialized && terminalWorkspace.layout === 'single' && terminalWorkspace.panes.length) {
                    const currentPane = terminalWorkspace.panes[0];
                    // ADB is a device target, not the saved SSH host mode.
                    // Cluster-directory refreshes can arrive while the ADB
                    // socket is still opening; replacing this pane with a
                    // plain host pane disposes it before terminal_connect.
                    // Keep every active ADB target and only refresh its host
                    // metadata, even when the workspace context is changing
                    // at the same time.
                    terminalWorkspace.panes[0] = currentPane?.mode === 'adb'
                        && currentPane.serialNo && currentPane.workerId
                        // Changing hostId changes the render signature. While
                        // a pane is mounted (or mounting), keep its identity
                        // stable so a directory refresh cannot dispose the
                        // socket before its first terminal_connect frame.
                        ? {...currentPane, hostId: currentPane.hostId || contextHost.id}
                        : {hostId: contextHost.id};
                }
                if (window.hostWorkspaceInitialized && currentPage === 'desktop') renderHostWorkspace();
                if (window.terminalWorkspaceInitialized && currentPage === 'terminal') renderTerminalWorkspace();
                if (window.hostWorkspaceInitialized) refreshHostWorkspaceHostSelectors();
                if (window.terminalWorkspaceInitialized) refreshTerminalWorkspaceHostSelectors();
            } catch (error) {
                console.debug('Cluster desktop hosts unavailable; preserving local host list', error);
            }
        }

        async function refreshClusterHostDirectory(force = true) {
            await mergeClusterDesktopHosts(force);
            return desktopHosts;
        }

        function refreshClusterHostDirectoryForWorker(workerId, status = '') {
            const normalizedWorkerId = String(workerId || '');
            if (!normalizedWorkerId) return Promise.resolve(desktopHosts);
            const cached = clusterHostDirectory.hosts.find(
                host => host.worker_id === normalizedWorkerId
            );
            if (cached && (!status || cached.status === status)) {
                return Promise.resolve(desktopHosts);
            }
            return refreshClusterHostDirectory(true);
        }
        Object.assign(window, {
            refreshClusterHostDirectory,
            refreshClusterHostDirectoryForWorker,
        });

        // 首次初始化或确保 VNC 已加载（桌面页面切换时调用）
        async function ensureDesktopInitialized() {
            if (!window.desktopHostsInitialized) {
                const hostsReady = initDesktopHosts();
                const context = window.GmsWorkspace?.get?.() || {};
                const canUseLocalBootstrap = context.scope_mode !== 'cluster'
                    && isLocalWorkspaceWorker(context.worker_id || workspaceLocalWorkerId())
                    && Boolean(workspaceHostForWorker(context.worker_id));
                if (canUseLocalBootstrap) {
                    // initDesktopHosts() synchronously creates the local entry
                    // before its first await. Mount now so directory latency
                    // does not delay the first visible desktop.
                    ensureHostWorkspaceInitialized();
                    hostsReady.then(() => {
                        window.desktopHostsInitialized = true;
                        debugLog('[Desktop] Hosts refreshed in background');
                        if (currentPage === 'desktop') ensureHostWorkspaceInitialized();
                    }).catch(error => {
                        debugLog('[Desktop] Background host refresh failed:', error);
                    });
                    return;
                }
                await hostsReady;
                window.desktopHostsInitialized = true;
                debugLog('[Desktop] Hosts initialized');
                ensureHostWorkspaceInitialized();
                return;
            }
            // The iframe/WebSocket is intentionally retained while hidden.
            // Restore it before refreshing directory metadata so re-entry is
            // not gated by a cluster API round trip.
            ensureHostWorkspaceInitialized();
            mergeClusterDesktopHosts().catch(error =>
                debugLog('[Desktop] Background host refresh failed:', error));
        }

        // ==================== 多主机工作区 ====================
        const HOST_WORKSPACE_COUNTS = {single: 1, horizontal: 2, vertical: 2, quad: 4};
        let hostWorkspaceClusterEnabled = false;
        let hostWorkspaceScopeModeInitialized = false;
        const hostWorkspace = {layout: 'single', panes: [], instances: new Map(), paneGenerations: new Map(), maximized: null, renderGeneration: 0, clusterState: null};

        function setHostWorkspaceSurfaceReady(pageName, ready) {
            document.getElementById(`page-${pageName}`)?.classList.toggle(
                'host-workspace-ready', Boolean(ready)
            );
        }

        function snapshotHostClusterState() {
            return {
                layout: hostWorkspace.layout,
                panes: hostWorkspace.panes.map(pane => ({...pane})),
                maximized: hostWorkspace.maximized,
            };
        }

        function useSingleHostWorkspaceState() {
            const preferred = workspaceHostForWorker(workspaceLocalWorkerId())
                || desktopHosts.find(host => host.id === 'default')
                || desktopHosts[0];
            hostWorkspace.layout = 'single';
            hostWorkspace.maximized = null;
            hostWorkspace.panes = [{type: 'desktop', hostId: preferred?.id || 'default'}];
        }

        function loadHostWorkspaceState() {
            try {
                const saved = JSON.parse(localStorage.getItem('gms_host_workspace') || '{}');
                if (HOST_WORKSPACE_COUNTS[saved.layout]) hostWorkspace.layout = saved.layout;
                if (Array.isArray(saved.panes)) hostWorkspace.panes = saved.panes.slice(0, 4);
            } catch (error) {
                console.warn('[Workspace] Invalid saved layout:', error);
            }
            normalizeHostWorkspacePanes();
            if (hostWorkspaceClusterEnabled) {
                const preferred = workspaceHostForWorker();
                if (preferred && hostWorkspace.layout === 'single' && hostWorkspace.panes.length) {
                    hostWorkspace.panes[0] = {type: 'desktop', hostId: preferred.id};
                }
            }
            hostWorkspace.clusterState = snapshotHostClusterState();
            if (!hostWorkspaceClusterEnabled) useSingleHostWorkspaceState();
        }

        function normalizeHostWorkspacePanes() {
            const count = HOST_WORKSPACE_COUNTS[hostWorkspace.layout] || 1;
            const fallbackHost = currentHost?.id || desktopHosts[0]?.id || 'default';
            while (hostWorkspace.panes.length < count) {
                const used = new Set(hostWorkspace.panes.filter(p => p.type !== 'empty').map(p => p.hostId));
                const nextHost = desktopHosts.find(host => !host.offline && !used.has(host.id));
                hostWorkspace.panes.push(nextHost ? {type: 'desktop', hostId: nextHost.id} : {type: 'empty', hostId: ''});
            }
            const assigned = new Set();
            hostWorkspace.panes = hostWorkspace.panes.slice(0, count).map(pane => {
                let hostId = pane?.type === 'empty' ? '' : pane?.hostId;
                if (!desktopHosts.some(host => host.id === hostId) || assigned.has(hostId)) {
                    hostId = desktopHosts.find(host => !host.offline && !assigned.has(host.id))?.id || '';
                }
                if (hostId) assigned.add(hostId);
                return {type: hostId ? 'desktop' : 'empty', hostId};
            });
        }

        function saveHostWorkspaceState() {
            if (hostWorkspaceClusterEnabled) {
                hostWorkspace.clusterState = snapshotHostClusterState();
            }
            const stateToSave = hostWorkspace.clusterState || snapshotHostClusterState();
            localStorage.setItem('gms_host_workspace', JSON.stringify({
                layout: stateToSave.layout,
                panes: stateToSave.panes
            }));
        }

        function ensureHostWorkspaceInitialized() {
            if (!window.hostWorkspaceInitialized) {
                loadHostWorkspaceState();
                window.hostWorkspaceInitialized = true;
            }
            renderHostWorkspace();
        }

        function disposeHostWorkspaceInstance(index) {
            const instance = hostWorkspace.instances.get(index);
            if (!instance) return;
            instance.disposed = true;
            if (instance.resizeObserver) instance.resizeObserver.disconnect();
            if (instance.socket) {
                instance.socket.onclose = null;
                instance.socket.close();
            }
            if (instance.frame) {
                instance.frame.onload = null;
                instance.frame.src = 'about:blank';
                instance.frame.remove();
            }
            // xterm schedules a viewport refresh after fit/open. Disposing in
            // the same task can race that refresh during rapid page switches.
            if (instance.terminal) {
                const oldTerminal = instance.terminal;
                setTimeout(() => { try { oldTerminal.dispose(); } catch (_) {} }, 50);
            }
            hostWorkspace.instances.delete(index);
        }

        function disposeAllHostWorkspaceInstances() {
            Array.from(hostWorkspace.instances.keys()).forEach(disposeHostWorkspaceInstance);
        }

        function hostWorkspaceHostOptions(selectedId, panes = [], paneIndex = -1) {
            const emptyOption = selectedId ? '' : '<option value="" selected>请选择主机</option>';
            return emptyOption + desktopHosts.map(host => {
                const assignedElsewhere = panes.some((pane, index) =>
                    index !== paneIndex && pane?.hostId === host.id
                );
                const disabled = host.offline || assignedElsewhere ? ' disabled' : '';
                const selected = host.id === selectedId ? ' selected' : '';
                return `<option value="${escapeHtml(host.id)}"${selected}${disabled}>${escapeHtml(host.name)}</option>`;
            }).join('');
        }

        function refreshHostWorkspaceHostSelectors() {
            hostWorkspace.panes.forEach((pane, index) => {
                const select = document.querySelector(
                    `[data-workspace-pane="${index}"] select[aria-label="主机"]`
                );
                if (!select) return;
                select.innerHTML = hostWorkspaceHostOptions(
                    pane.hostId,
                    hostWorkspace.panes,
                    index
                );
                select.disabled = pane.type === 'empty';
            });
        }

        function hostWorkspaceRenderSignature() {
            return JSON.stringify({
                layout: hostWorkspace.layout,
                panes: hostWorkspace.panes,
                maximized: hostWorkspace.maximized,
            });
        }

        function renderHostWorkspace() {
            const grid = document.getElementById('host-workspace-grid');
            if (!grid) return;
            normalizeHostWorkspacePanes();
            // If the pane layout hasn't changed since the last render and the
            // DOM still has children, keep existing VNC connections alive and
            // skip the dispose + remount cycle.
            const signature = hostWorkspaceRenderSignature();
            if (hostWorkspace.renderedSignature === signature && grid.children.length) {
                // The grid may have been rendered while this page was hidden.
                // In that case the delayed mount is intentionally skipped and
                // the pane is left at "准备中". Resume only those never-started
                // panes; keep live VNC/terminal instances untouched.
                if (currentPage === 'desktop') {
                    const pendingPanes = hostWorkspace.panes
                        .map((pane, index) => ({pane, index}))
                        .filter(({index}) => {
                            const status = document.getElementById(`host-workspace-status-${index}`);
                            return !hostWorkspace.instances.has(index) && status?.textContent === '准备中';
                        });
                    if (pendingPanes.length) {
                        const generation = ++hostWorkspace.renderGeneration;
                        pendingPanes.forEach(({pane, index}) => {
                            mountHostWorkspacePane(index, pane, generation);
                        });
                    }
                }
                setHostWorkspaceSurfaceReady('desktop', true);
                return;
            }
            hostWorkspace.renderedSignature = signature;
            const generation = ++hostWorkspace.renderGeneration;
            saveHostWorkspaceState();
            const reconnectDelay = hostWorkspace.instances.size ? 350 : 0;
            disposeAllHostWorkspaceInstances();
            grid.className = `host-workspace-grid layout-${hostWorkspace.layout}`;
            document.querySelectorAll('[data-workspace-layout]').forEach(button => {
                button.classList.toggle('active', button.dataset.workspaceLayout === hostWorkspace.layout);
            });
            grid.classList.toggle('pane-maximized', hostWorkspace.maximized !== null);
            grid.innerHTML = hostWorkspace.panes.map((pane, index) => `
                <section class="host-workspace-pane${hostWorkspace.maximized === index ? ' maximized' : ''}" data-workspace-pane="${index}">
                    <div class="host-workspace-pane-header">
                        <span data-multi-host-control style="font-size:11px;">🖥️ 桌面</span>
                        <select data-multi-host-control aria-label="主机" data-change="changeHostWorkspacePaneHost" data-a0="${index}" data-r1="value"${pane.type === 'empty' ? ' disabled' : ''}>
                            ${hostWorkspaceHostOptions(pane.hostId, hostWorkspace.panes, index)}
                        </select>
                        <span class="host-workspace-pane-status" id="host-workspace-status-${index}">准备中</span>
                        <button class="btn-xs host-workspace-refresh-btn" data-click="refreshHostWorkspacePane" data-a0="${index}" title="重新连接" aria-label="刷新主机桌面">🔄</button>
                        <button data-multi-host-control class="btn-xs" data-click="maximizeHostWorkspacePane" data-a0="${index}" title="${hostWorkspace.maximized === index ? '恢复布局' : '单屏显示'}">${hostWorkspace.maximized === index ? '▦' : '⛶'}</button>
                        <button data-multi-host-control class="btn-xs" data-click="closeHostWorkspacePane" data-a0="${index}" title="关闭">×</button>
                    </div>
                    <div class="host-workspace-pane-body" id="host-workspace-body-${index}">
                        <div class="host-workspace-empty">${pane.type === 'empty' ? '桌面未打开' : '正在连接桌面…'}</div>
                    </div>
                </section>`).join('');
            // Give remote single-session VNC servers time to release the old
            // iframe socket. The generation guard also cancels mounts from a
            // superseded render, preventing duplicate connections.
            const autoMountIndex = hostWorkspace.maximized ?? 0;
            hostWorkspace.panes.forEach((pane, index) => {
                const paneGeneration = hostWorkspace.paneGenerations.get(index) || 0;
                const host = desktopHosts.find(item => item.id === pane.hostId);
                if (index !== autoMountIndex && pane.type !== 'empty' && host && !host.offline) {
                    const body = document.getElementById(`host-workspace-body-${index}`);
                    if (body) {
                        body.innerHTML = `<div class="host-workspace-empty"><button class="btn-xs" data-click="refreshHostWorkspacePane" data-a0="${index}">连接此桌面</button></div>`;
                    }
                    setHostWorkspaceStatus(index, '按需连接');
                    return;
                }
                setTimeout(() => {
                    if (generation === hostWorkspace.renderGeneration && currentPage === 'desktop') {
                        mountHostWorkspacePane(index, pane, generation, paneGeneration);
                    }
                }, reconnectDelay);
            });
            setHostWorkspaceSurfaceReady('desktop', true);
        }

        function setHostWorkspaceStatus(index, text, connected = false) {
            const status = document.getElementById(`host-workspace-status-${index}`);
            if (!status) return;
            status.textContent = text;
            status.style.color = connected ? 'var(--success-color)' : 'var(--text-secondary)';
            // 单机模式下 pane header 被隐藏，状态文字和刷新按钮移到标题栏；
            // pane 0 是单机唯一的桌面，同步更新标题栏的镜像状态。
            const singleStatus = document.getElementById('host-workspace-status-single');
            if (singleStatus && index === 0) {
                singleStatus.textContent = text;
                singleStatus.style.color = connected ? 'var(--success-color)' : 'var(--text-secondary)';
            }
        }

        async function mountHostWorkspacePane(index, pane, generation = hostWorkspace.renderGeneration, paneGeneration = hostWorkspace.paneGenerations.get(index) || 0) {
            const body = document.getElementById(`host-workspace-body-${index}`);
            if (!body) return;
            if (paneGeneration !== (hostWorkspace.paneGenerations.get(index) || 0)) return;
            const host = desktopHosts.find(item => item.id === pane.hostId);
            if (pane.type === 'empty') {
                body.innerHTML = `<div class="host-workspace-empty"><button class="btn-xs" data-click="reopenHostWorkspacePane" data-a0="${index}">重新打开桌面</button></div>`;
                setHostWorkspaceStatus(index, '未打开');
                return;
            }
            if (!host || host.offline) {
                body.innerHTML = '<div class="host-workspace-empty">主机不可用或已离线</div>';
                setHostWorkspaceStatus(index, '离线');
                return;
            }
            await mountHostWorkspaceDesktop(index, host, body, generation, pane.hostId, paneGeneration);
        }

        function hostWorkspaceMountIsCurrent(index, body, generation, expectedHostId, paneGeneration) {
            return Boolean(
                body?.isConnected
                && document.getElementById(`host-workspace-body-${index}`) === body
                && generation === hostWorkspace.renderGeneration
                && paneGeneration === (hostWorkspace.paneGenerations.get(index) || 0)
                && hostWorkspace.panes[index]?.hostId === expectedHostId
            );
        }

        async function resolveWorkspaceVncUrl(host, elevationRetried = false) {
            const workerId = host.worker_id || (host.id === 'default' ? workspaceLocalWorkerId() : '');
            if (!workerId) {
                throw new Error('该主机不在服务端授权目录中');
            }
            if (isLocalWorkspaceWorker(workerId)) {
                try {
                    // Grant issuance only validates the authorized Worker and
                    // session; it does not depend on VNC runtime state. Run it
                    // alongside the status request to remove one round trip
                    // from the common already-running path.
                    const [status, url] = await Promise.all([
                        apiCall('/api/desktop/vnc/status'),
                        requestNovncAccess(workerId),
                    ]);
                    if (!status.running) {
                        await apiCall('/api/desktop/vnc/start', 'POST', {worker_id: workerId});
                    }
                    return url;
                } catch (error) {
                    debugLog('[Desktop] Local VNC status/start failed:', error);
                    if (error?.status === 403 && !elevationRetried) {
                        const granted = await ensureTerminalElevation(true, '打开主机桌面', '主机桌面');
                        if (granted) return resolveWorkspaceVncUrl(host, true);
                    }
                    if (error?.status === 401 || /Authentication required/i.test(error?.message || '')) {
                        showAuthGate(false);
                    }
                    throw error;
                }
            }
            return requestNovncAccess(workerId);
        }

        async function mountHostWorkspaceDesktop(index, host, body, generation, expectedHostId, paneGeneration) {
            setHostWorkspaceStatus(index, '正在连接桌面…');
            try {
                const granted = await ensureTerminalElevation(false, '打开主机桌面', '主机桌面');
                if (!hostWorkspaceMountIsCurrent(
                    index, body, generation, expectedHostId, paneGeneration
                )) return;
                if (!granted) {
                    body.innerHTML = '<div class="host-workspace-empty">需要管理员认证后才能打开主机桌面</div>';
                    setHostWorkspaceStatus(index, '等待管理员认证');
                    return;
                }
                const url = await resolveWorkspaceVncUrl(host);
                if (!hostWorkspaceMountIsCurrent(
                    index, body, generation, expectedHostId, paneGeneration
                )) return;
                const frame = document.createElement('iframe');
                frame.allowFullscreen = true;
                frame.allow = 'clipboard-read; clipboard-write';
                frame.src = url;
                frame.onload = () => {
                    const instance = hostWorkspace.instances.get(index);
                    if (instance?.frame === frame && !instance.disposed
                        && hostWorkspaceMountIsCurrent(
                            index, body, generation, expectedHostId, paneGeneration
                        )) {
                        setHostWorkspaceStatus(index, '桌面已连接', true);
                    }
                };
                body.replaceChildren(frame);
                hostWorkspace.instances.set(index, {
                    type: 'desktop', hostId: expectedHostId, frame, disposed: false
                });
            } catch (error) {
                if (!hostWorkspaceMountIsCurrent(
                    index, body, generation, expectedHostId, paneGeneration
                )) return;
                if (error?.status === 401 || /Authentication required/i.test(error?.message || '')) {
                    showAuthGate(false);
                }
                body.innerHTML = `<div class="host-workspace-empty">${escapeHtml(error.message)}</div>`;
                setHostWorkspaceStatus(index, '连接失败');
            }
        }

        async function mountHostWorkspaceTerminal(index, host, body) {
            setHostWorkspaceStatus(index, '正在加载终端…');
            try {
                await loadXtermScripts();
            } catch (error) {
                body.innerHTML = '<div class="host-workspace-empty">xterm.js 加载失败</div>';
                setHostWorkspaceStatus(index, '加载失败');
                return;
            }
            if (!body.isConnected || currentPage !== 'desktop') return;
            const terminalElement = document.createElement('div');
            terminalElement.className = 'host-workspace-terminal';
            body.replaceChildren(terminalElement);
            const term = new Terminal({cursorBlink: true, fontSize: 13, fontFamily: 'Consolas, "Courier New", monospace',
                theme: createTerminalTheme(), scrollback: 2000, termName: 'xterm-256color'});
            const fit = new FitAddon.FitAddon();
            term.loadAddon(fit);
            term.open(terminalElement);
            fit.fit();
            const instance = {
                type: 'terminal', hostId: host.id, terminal: term, fit,
                socket: null, resizeObserver: null, disposed: false,
                lastResizeCols: 0, lastResizeRows: 0
            };
            hostWorkspace.instances.set(index, instance);
            const [user = '', address = ''] = String(host.connection || '').split('@');
            const workerId = host.worker_id || (host.id === 'default' ? workspaceLocalWorkerId() : '');
            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const clientId = `workspace_${index}_${Date.now()}_${Math.random().toString(36).slice(2)}`;
            const socket = new WebSocket(`${protocol}//${location.host}/api/system/websocket/${clientId}`);
            instance.socket = socket;
            term.writeln(`\x1b[33m⏳ 正在连接 ${user}@${address}...\x1b[0m`);
            term.onData(input => {
                if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type: 'terminal_input', input}));
            });
            socket.onopen = () => {
                if (!instance.disposed) {
                    socket.send(JSON.stringify({type: 'terminal_connect', mode: 'ssh', worker_id: workerId}));
                }
            };
            socket.onmessage = event => {
                if (instance.disposed) return;
                try {
                    const message = JSON.parse(event.data);
                    if (message.type === 'terminal_data') term.write(message.data || '');
                    else if (message.type === 'terminal_connected') {
                        setHostWorkspaceStatus(index, `${user}@${address}`, true);
                        setTimeout(() => resizeHostWorkspaceTerminal(instance), 0);
                    } else if (message.type === 'terminal_error' || message.type === 'error') {
                        term.writeln(`\r\n\x1b[31m${message.error || message.message || '终端连接失败'}\x1b[0m`);
                        if (message.elevation_required) {
                            setHostWorkspaceStatus(index, '等待管理员认证');
                            recoverTerminalElevation(instance, '重新连接主机终端', () => refreshHostWorkspacePane(index));
                        } else if (message.credential_required && message.device_host && typeof showDevicePasswordModal === 'function') {
                            setHostWorkspaceStatus(index, '等待 SSH 凭据');
                            showDevicePasswordModal(message.device_host, 'terminal', () => refreshHostWorkspacePane(index));
                        } else {
                            setHostWorkspaceStatus(index, '连接失败');
                        }
                    }
                } catch (error) { console.error('[Workspace terminal] Invalid message:', error); }
            };
            socket.onclose = () => {
                if (!instance.disposed) {
                    term.writeln('\r\n\x1b[31m⚠️ 连接已断开\x1b[0m');
                    setHostWorkspaceStatus(index, '已断开');
                }
            };
            socket.onerror = () => {
                if (!instance.disposed) setHostWorkspaceStatus(index, '连接错误');
            };
            instance.resizeObserver = new ResizeObserver(() => resizeHostWorkspaceTerminal(instance));
            instance.resizeObserver.observe(body);
        }

        function resizeHostWorkspaceTerminal(instance) {
            if (!instance || instance.disposed || !instance.terminal?.element?.isConnected) return;
            const page = instance.terminal.element.closest('.page-content');
            const body = instance.terminal.element.closest('.host-workspace-pane-body');
            // A hidden page reports a zero/tiny geometry. Fitting and sending
            // that transient size generates SIGWINCH in the shell; readline
            // then redraws the prompt, which looked like an extra prompt every
            // time the user switched back to the terminal page.
            if (page && !page.classList.contains('active')) return;
            if (!body || body.clientWidth < 20 || body.clientHeight < 20) return;
            try {
                instance.fit.fit();
                const cols = instance.terminal.cols;
                const rows = instance.terminal.rows;
                if (cols < 2 || rows < 1) return;
                // Cache the last size that actually reached the backend PTY,
                // not merely the last browser measurement. ResizeObserver can
                // run before WebSocket.open; caching that early measurement
                // caused the post-connect resize to be skipped, leaving the
                // PTY at 80 columns while xterm rendered a much wider surface.
                // Readline history/cursor redraw then wrapped at different
                // columns on each side and left prompts/commands overlapping.
                if (instance.socket?.readyState !== WebSocket.OPEN) return;
                if (cols === instance.lastResizeCols && rows === instance.lastResizeRows) return;
                instance.socket.send(JSON.stringify({type: 'terminal_resize', cols, rows}));
                instance.lastResizeCols = cols;
                instance.lastResizeRows = rows;
            } catch (_) {}
        }

        function setHostWorkspaceLayout(layout) {
            if (!HOST_WORKSPACE_COUNTS[layout]) return;
            if (!hostWorkspaceClusterEnabled && layout !== 'single') return;
            hostWorkspace.layout = layout;
            renderHostWorkspace();
        }
        function changeHostWorkspacePaneHost(index, hostId) {
            const pane = hostWorkspace.panes[index];
            const host = desktopHosts.find(item => item.id === hostId);
            const select = document.querySelector(`[data-workspace-pane="${index}"] select[aria-label="主机"]`);
            if (!pane || !host || host.offline) {
                if (select && pane) select.value = pane.hostId;
                return;
            }
            if (hostWorkspace.panes.some((item, paneIndex) => paneIndex !== index && item.hostId === hostId)) {
                if (select) select.value = pane.hostId;
                showToast('该主机已在其他桌面窗格中显示', 'warning');
                return;
            }
            if (pane.hostId === hostId) return;
            pane.hostId = hostId;
            pane.type = 'desktop';
            if (hostWorkspace.layout === 'single') {
                currentHost = host;
                updateWorkspaceForHost(host, 'desktop');
            }
            saveHostWorkspaceState();
            hostWorkspace.renderedSignature = hostWorkspaceRenderSignature();
            hostWorkspace.panes.forEach((item, paneIndex) => {
                const paneSelect = document.querySelector(`[data-workspace-pane="${paneIndex}"] select[aria-label="主机"]`);
                if (paneSelect) paneSelect.innerHTML = hostWorkspaceHostOptions(item.hostId, hostWorkspace.panes, paneIndex);
            });
            refreshHostWorkspacePane(index);
        }
        function refreshHostWorkspacePane(index) {
            const pane = hostWorkspace.panes[index];
            const body = document.getElementById(`host-workspace-body-${index}`);
            if (!pane || !body) return;
            disposeHostWorkspaceInstance(index);
            const paneGeneration = (hostWorkspace.paneGenerations.get(index) || 0) + 1;
            hostWorkspace.paneGenerations.set(index, paneGeneration);
            body.innerHTML = '<div class="host-workspace-empty">正在连接桌面…</div>';
            setHostWorkspaceStatus(index, '正在连接桌面…');
            mountHostWorkspacePane(index, pane, hostWorkspace.renderGeneration, paneGeneration);
        }
        function closeHostWorkspacePane(index) { hostWorkspace.panes[index].type = 'empty'; renderHostWorkspace(); }
        function reopenHostWorkspacePane(index) { hostWorkspace.panes[index].type = 'desktop'; renderHostWorkspace(); }
        function maximizeHostWorkspacePane(index) { hostWorkspace.maximized = hostWorkspace.maximized === index ? null : index; renderHostWorkspace(); }

        window.setHostWorkspaceLayout = setHostWorkspaceLayout;
        window.changeHostWorkspacePaneHost = changeHostWorkspacePaneHost;
        window.refreshHostWorkspacePane = refreshHostWorkspacePane;
        window.closeHostWorkspacePane = closeHostWorkspacePane;
        window.reopenHostWorkspacePane = reopenHostWorkspacePane;
        window.maximizeHostWorkspacePane = maximizeHostWorkspacePane;

        // ==================== 多主机终端工作区 ====================
        const terminalWorkspace = {layout:'single', panes:[], instances:new Map(), mountingPanes:new Map(), paneGenerations:new Map(), maximized:null, generation:0, clusterState:null, pendingAdbTarget:null};

        function refreshTerminalWorkspaceHostSelectors() {
            terminalWorkspace.panes.forEach((pane, index) => {
                const select = document.querySelector(
                    `[data-terminal-pane="${index}"] select[aria-label="主机"]`
                );
                if (!select) return;
                select.innerHTML = hostWorkspaceHostOptions(
                    pane.hostId,
                    terminalWorkspace.panes,
                    index
                );
            });
        }
        function snapshotTerminalClusterState() {
            return {
                layout: terminalWorkspace.layout,
                // ADB Shell targets are one-shot sessions. Persist only host
                // selections so a mode change or reload cannot reopen/claim a
                // previously selected device automatically.
                panes: terminalWorkspace.panes.map(pane => ({hostId: pane.hostId})),
                maximized: terminalWorkspace.maximized,
            };
        }
        function useSingleTerminalWorkspaceState() {
            const preferred=workspaceHostForWorker(workspaceLocalWorkerId())
                ||desktopHosts.find(host=>host.id==='default')||desktopHosts[0];
            terminalWorkspace.layout='single';
            terminalWorkspace.maximized=null;
            terminalWorkspace.panes=[{hostId:preferred?.id||'default'}];
        }
        function normalizeTerminalWorkspace() {
            const count = HOST_WORKSPACE_COUNTS[terminalWorkspace.layout] || 1;
            const fallback = currentHost?.id || desktopHosts[0]?.id || 'default';
            while (terminalWorkspace.panes.length < count) terminalWorkspace.panes.push({hostId:''});
            const assigned = new Set();
            terminalWorkspace.panes = terminalWorkspace.panes.slice(0, count).map(p => {
                let hostId = p?.hostId;
                const adbSerial = String(p?.serialNo || '').trim();
                const adbWorkerId = String(p?.workerId || '').trim();
                if (p?.mode === 'adb' && adbSerial && adbWorkerId) {
                    if (hostId) assigned.add(hostId);
                    return {hostId, mode:'adb', serialNo:adbSerial, workerId:adbWorkerId};
                }
                if (!desktopHosts.some(h => h.id === hostId) || assigned.has(hostId)) {
                    hostId = (!assigned.has(fallback) && desktopHosts.some(h => h.id === fallback && !h.offline)
                        ? fallback
                        : desktopHosts.find(h => !h.offline && !assigned.has(h.id))?.id) || '';
                }
                if (hostId) assigned.add(hostId);
                return {hostId};
            });
        }
        function saveTerminalWorkspace() {
            if(hostWorkspaceClusterEnabled)terminalWorkspace.clusterState=snapshotTerminalClusterState();
            const stateToSave=terminalWorkspace.clusterState||snapshotTerminalClusterState();
            localStorage.setItem('gms_terminal_workspace',JSON.stringify({layout:stateToSave.layout,panes:stateToSave.panes}));
        }
        function initializeTerminalWorkspaceState() {
            if (window.terminalWorkspaceInitialized) return;
            try {
                const saved=JSON.parse(localStorage.getItem('gms_terminal_workspace')||'{}');
                if(HOST_WORKSPACE_COUNTS[saved.layout])terminalWorkspace.layout=saved.layout;
                if(Array.isArray(saved.panes))terminalWorkspace.panes=saved.panes;
            } catch(_) {}
            normalizeTerminalWorkspace();
            terminalWorkspace.clusterState=snapshotTerminalClusterState();
            if(!hostWorkspaceClusterEnabled)useSingleTerminalWorkspaceState();
            window.terminalWorkspaceInitialized=true;
        }
        async function ensureTerminalWorkspaceInitialized() {
            if (!window.desktopHostsInitialized) {
                const hostsReady = initDesktopHosts();
                const context = window.GmsWorkspace?.get?.() || {};
                const canUseLocalBootstrap = context.scope_mode !== 'cluster'
                    && isLocalWorkspaceWorker(context.worker_id || workspaceLocalWorkerId())
                    && Boolean(workspaceHostForWorker(context.worker_id));
                if (!canUseLocalBootstrap) {
                    await hostsReady;
                } else {
                    hostsReady.catch(error =>
                        debugLog('[Terminal] Background host refresh failed:', error));
                }
                window.desktopHostsInitialized = true;
            }
            initializeTerminalWorkspaceState();
            const preferred=workspaceHostForWorker();
            if(preferred&&terminalWorkspace.layout==='single'&&terminalWorkspace.panes.length){
                const currentPane=terminalWorkspace.panes[0],preferredWorkerId=preferred.worker_id||workspaceLocalWorkerId();
                terminalWorkspace.panes[0]=currentPane?.mode==='adb'&&currentPane.workerId===preferredWorkerId
                    ? {...currentPane,hostId:preferred.id}:{hostId:preferred.id};
            }
            renderTerminalWorkspace();
        }
        function disposeTerminalWorkspaceInstance(index) {
            const instance=terminalWorkspace.instances.get(index);if(!instance)return;
            instance.disposed=true;clearTerminalWorkspaceStartupTimer(instance);instance.resizeObserver?.disconnect();
            if(instance.socket){instance.socket.onclose=null;instance.socket.close();}
            if(instance.terminal){const oldTerminal=instance.terminal;setTimeout(()=>{try{oldTerminal.dispose();}catch(_){}},50);}
            terminalWorkspace.instances.delete(index);
        }
        function disposeTerminalWorkspace() {
            terminalWorkspace.generation+=1;
            [...terminalWorkspace.instances.keys()].forEach(disposeTerminalWorkspaceInstance);
            terminalWorkspace.mountingPanes.clear();
            terminalWorkspace.paneGenerations.clear();
        }
        function renderTerminalWorkspace() {
            const grid=document.getElementById('terminal-workspace-grid'); if(!grid)return;
            normalizeTerminalWorkspace(); saveTerminalWorkspace();
            // Keep existing terminal sessions alive when the layout hasn't
            // changed. Matches renderHostWorkspace: switching away no longer
            // disposes instances, so revisiting should reuse them.
            const signature=terminalWorkspaceRenderSignature();
            if(terminalWorkspace.renderedSignature===signature && grid.children.length) {
                // A hidden-page mount stops after xterm finishes loading. On
                // return, resume panes that never produced a live instance;
                // refreshTerminalWorkspacePane invalidates any older pending
                // mount so duplicate WebSockets cannot be created.
                if(currentPage==='terminal') terminalWorkspace.panes.forEach((pane,index)=>{
                    const status=document.getElementById(`terminal-workspace-status-${index}`)?.textContent;
                    if(!terminalWorkspace.instances.has(index)&&!terminalWorkspace.mountingPanes.has(index)&&(status==='准备中'||status==='正在加载终端…')) {
                        refreshTerminalWorkspacePane(index);
                    }
                });
                setHostWorkspaceSurfaceReady('terminal',true);
                return;
            }
            // Workspace-context/host-directory updates may legitimately alter
            // only the host metadata while an ADB session is opening. A full
            // render would dispose the live WebSocket before terminal_connect
            // is sent. Keep the mounted one-shot ADB target alive when its
            // device identity is unchanged; a real device/layout change still
            // takes the normal teardown path below.
            const activeAdbInstance = terminalWorkspace.instances.get(0);
            const activeAdbPane = terminalWorkspace.panes[0];
            const pendingAdb = terminalWorkspace.pendingAdbTarget;
            if (grid.children.length && terminalWorkspace.layout === 'single'
                    && pendingAdb?.serialNo === activeAdbPane?.serialNo
                    && pendingAdb?.workerId === activeAdbPane?.workerId
                    && (!activeAdbInstance || activeAdbInstance.mode === 'adb'
                        || terminalWorkspace.mountingPanes.has(0))
                    && activeAdbPane?.mode === 'adb') {
                // xterm loading happens before the instance is registered.
                // Keep the already-mounted ADB DOM/socket lifecycle intact
                // during that gap as well.
                terminalWorkspace.renderedSignature=signature;
                refreshTerminalWorkspaceHostSelectors();
                setHostWorkspaceSurfaceReady('terminal',true);
                return;
            }
            if (grid.children.length && terminalWorkspace.layout === 'single'
                    && activeAdbInstance?.mode === 'adb'
                    && !activeAdbInstance.disposed
                    && activeAdbPane?.mode === 'adb'
                    && activeAdbPane.serialNo === activeAdbInstance.serialNo
                    && activeAdbPane.workerId === activeAdbInstance.workerId) {
                terminalWorkspace.renderedSignature=signature;
                refreshTerminalWorkspaceHostSelectors();
                setHostWorkspaceSurfaceReady('terminal',true);
                return;
            }
            terminalWorkspace.renderedSignature=signature;
            disposeTerminalWorkspace();
            grid.className=`host-workspace-grid layout-${terminalWorkspace.layout}${terminalWorkspace.maximized!==null?' pane-maximized':''}`;
            document.querySelectorAll('[data-terminal-layout]').forEach(b=>b.classList.toggle('active',b.dataset.terminalLayout===terminalWorkspace.layout));
            grid.innerHTML=terminalWorkspace.panes.map((p,i)=>`<section class="host-workspace-pane${terminalWorkspace.maximized===i?' maximized':''}" data-terminal-pane="${i}">
                <div class="host-workspace-pane-header"><span data-multi-host-control data-terminal-pane-mode-label style="font-size:11px;">🐧 终端</span>
                <select data-multi-host-control aria-label="主机" data-change="changeTerminalWorkspaceHost" data-a0="${i}" data-r1="value">${hostWorkspaceHostOptions(p.hostId,terminalWorkspace.panes,i)}</select>
                <span class="host-workspace-pane-status" id="terminal-workspace-status-${i}">准备中</span>
                <button data-multi-host-control data-terminal-pane-host-mode class="btn-xs" data-click="restoreHostTerminalWorkspacePane" data-a0="${i}" title="关闭当前 ADB Shell，返回这台主机的 SSH 终端" style="${p.mode==='adb'?'':'display:none'}">↩ 主机终端</button>
                <button class="btn-xs host-workspace-refresh-btn" data-click="refreshTerminalWorkspacePane" data-a0="${i}" title="重新连接" aria-label="刷新主机终端">🔄</button>
                <button data-multi-host-control class="btn-xs" data-click="maximizeTerminalWorkspacePane" data-a0="${i}">${terminalWorkspace.maximized===i?'▦':'⛶'}</button></div>
                <div class="host-workspace-pane-body" id="terminal-workspace-body-${i}"><div class="host-workspace-empty">${p.mode==='adb'?'正在打开 ADB Shell…':'正在加载终端…'}</div></div></section>`).join('');
            const singleHostModeButton=document.getElementById('terminal-workspace-host-mode-btn');
            if(singleHostModeButton)singleHostModeButton.style.display=terminalWorkspace.panes[0]?.mode==='adb'?'':'none';
            const generation=terminalWorkspace.generation;
            const autoMountIndex=terminalWorkspace.maximized??0;
            terminalWorkspace.panes.forEach((p,i)=>{
                const host=desktopHosts.find(item=>item.id===p.hostId);
                if(i!==autoMountIndex&&host&&!host.offline){
                    const body=document.getElementById(`terminal-workspace-body-${i}`);
                    if(body)body.innerHTML=`<div class="host-workspace-empty"><button class="btn-xs" data-click="refreshTerminalWorkspacePane" data-a0="${i}">连接此终端</button></div>`;
                    terminalWorkspaceStatus(i,'按需连接');
                    return;
                }
                mountTerminalWorkspacePane(i,p,generation);
            });
            setHostWorkspaceSurfaceReady('terminal',true);
        }
        function terminalWorkspaceStatus(i,text,ok=false){
            const color=ok?'var(--success-color)':'var(--text-secondary)';
            const e=document.getElementById(`terminal-workspace-status-${i}`);
            if(e){e.textContent=text;e.style.color=color;}
            const singleStatus=document.getElementById('terminal-workspace-status-single');
            if(singleStatus&&i===0){singleStatus.textContent=text;singleStatus.style.color=color;}
        }
        async function mountTerminalWorkspacePane(index,pane,generation,paneGeneration=terminalWorkspace.paneGenerations.get(index)||0) {
            const body=document.getElementById(`terminal-workspace-body-${index}`),host=desktopHosts.find(h=>h.id===pane.hostId); if(!body)return;
            if(!host){body.innerHTML='<div class="host-workspace-empty">暂无更多可用主机</div>';terminalWorkspaceStatus(index,'空闲');return;}
            if(host.offline){body.innerHTML='<div class="host-workspace-empty">主机已离线</div>';terminalWorkspaceStatus(index,'离线');return;}
            const terminalMode=pane.mode==='adb'?'adb':'ssh',serialNo=String(pane.serialNo||'').trim();
            if(terminalMode==='adb'&&!serialNo){body.innerHTML='<div class="host-workspace-empty">设备序列号无效</div>';terminalWorkspaceStatus(index,'连接失败');return;}
            const mountToken={generation,paneGeneration};
            const pendingMount=terminalWorkspace.mountingPanes.get(index);
            if(pendingMount&&pendingMount.generation===generation&&pendingMount.paneGeneration===paneGeneration)return;
            terminalWorkspace.mountingPanes.set(index,mountToken);
            terminalWorkspaceStatus(index,terminalMode==='adb'?'正在打开 ADB Shell…':'正在加载终端…');
            try{await loadXTermScripts();}catch(error){if(terminalWorkspace.mountingPanes.get(index)===mountToken)terminalWorkspace.mountingPanes.delete(index);console.error('[Terminal workspace] xterm.js load failed:',error);if(generation!==terminalWorkspace.generation||paneGeneration!==(terminalWorkspace.paneGenerations.get(index)||0)||currentPage!=='terminal'||!body.isConnected)return;body.innerHTML='<div class="host-workspace-empty">xterm.js 加载失败</div>';terminalWorkspaceStatus(index,'加载失败');return;}
            if(terminalWorkspace.mountingPanes.get(index)===mountToken)terminalWorkspace.mountingPanes.delete(index);
            if(generation!==terminalWorkspace.generation||paneGeneration!==(terminalWorkspace.paneGenerations.get(index)||0)||currentPage!=='terminal'||!body.isConnected)return;
            // terminal_ 前缀 WS 在认证部署下要求已提权 session（服务端握手
            // 直接 403）。switchPage 的门卫覆盖不了恢复/复用路径（提权过期
            // 后切回终端页），这里在建立 WS 前兜底确认，拒绝时给出可见
            // 状态而不是哑失败。
            if(!await ensureTerminalElevation(false,'打开主机终端','主机终端')){
                if(generation===terminalWorkspace.generation&&paneGeneration===(terminalWorkspace.paneGenerations.get(index)||0)&&currentPage==='terminal'&&body.isConnected){
                    body.innerHTML='<div class="host-workspace-empty">需要管理员认证后才能打开主机终端</div>';
                    terminalWorkspaceStatus(index,'等待管理员认证');
                }
                return;
            }
            const el=document.createElement('div');el.className='host-workspace-terminal';body.replaceChildren(el);
            const term=new Terminal({cursorBlink:true,fontSize:13,fontFamily:'Consolas, "Courier New", monospace',theme:createTerminalTheme(),scrollback:2000,termName:'xterm-256color'});
            const fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open(el);fit.fit();
            const workerId=pane.workerId||host.worker_id||(host.id==='default'?workspaceLocalWorkerId():'');
            const instance={type:'terminal',mode:terminalMode,paneIndex:index,hostId:host.id,workerId,serialNo,terminal:term,fit,socket:null,resizeObserver:null,disposed:false,initialData:'',shellReady:false,startupFailed:false,startupFailureStatus:'',startupTimer:null,lastResizeCols:0,lastResizeRows:0};terminalWorkspace.instances.set(index,instance);
            const parts=String(host.connection||'').split('@'),user=parts.shift()||'',address=parts.join('@');
            body.addEventListener('dragover',event=>{event.preventDefault();body.style.outline='2px dashed var(--primary-color)';});
            body.addEventListener('dragleave',()=>{body.style.outline='';});
            body.addEventListener('drop',async event=>{event.preventDefault();body.style.outline='';const file=event.dataTransfer?.files?.[0];if(!file)return;if(!await ensureTerminalElevation(false,'上传文件到主机','主机文件上传'))return;term.writeln(`\r\n\x1b[33m📤 正在上传 ${file.name}...\x1b[0m`);const form=new FormData();form.append('file',file);form.append('worker_id',workerId);try{const result=await apiCall('/api/terminal/push','POST',form);term.writeln(`\r\n\x1b[32m✅ 已上传到 ${result.remote_path}\x1b[0m`);}catch(error){term.writeln(`\r\n\x1b[31m❌ 上传失败: ${error.message}\x1b[0m`);}finally{if(socket.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:'terminal_input',input:'\r'}));term.focus();}});
            const protocol=location.protocol==='https:'?'wss:':'ws:',socket=new WebSocket(`${protocol}//${location.host}/api/system/websocket/terminal_workspace_${index}_${Date.now()}_${Math.random().toString(36).slice(2)}`);instance.socket=socket;
            // ADB 启动时宿主 Shell 会依次回显 clear 和 adb shell 命令。
            // 状态栏已经提供连接反馈，终端画布等设备提示符就绪后再一次性显示，
            // 避免“加载文字 -> 清屏 -> 宿主输出 -> 再清屏”的闪烁。
            if(terminalMode!=='adb')term.writeln(`\x1b[33m⏳ 正在连接 ${user}@${address}...\x1b[0m`);
            term.onData(input=>{if(!instance.startupFailed&&socket.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:'terminal_input',input}));});
            let suppressPasteUntil=0;
            const sendInput=input=>{if(instance.startupFailed||socket.readyState!==WebSocket.OPEN)return false;socket.send(JSON.stringify({type:'terminal_input',input}));return true;};
            const pasteText=text=>sendInput(String(text||'').replace(/\r\n/g,'\n').replace(/\r/g,'\n'));
            const pasteClipboard=async()=>{try{if(navigator.clipboard?.readText)pasteText(await navigator.clipboard.readText());}catch(error){console.debug('[Terminal workspace] clipboard read failed',error);}finally{term.focus();}};
            term.attachCustomKeyEventHandler(event=>{if(event.type!=='keydown')return true;const key=event.key.length===1?event.key.toLowerCase():event.key,isCtrl=event.ctrlKey&&!event.altKey&&!event.metaKey;if(isCtrl&&key==='c'){if(term.hasSelection()){const text=term.getSelection();navigator.clipboard?.writeText(text);term.clearSelection();term.focus();}else sendInput('\x03');return false;}if(isCtrl&&key==='v'){suppressPasteUntil=Date.now()+500;pasteClipboard();return false;}if(isCtrl&&/^[a-z]$/.test(key)){sendInput(String.fromCharCode(key.charCodeAt(0)-96));return false;}return true;});
            el.addEventListener('click',()=>term.focus());
            el.addEventListener('paste',event=>{if(Date.now()<suppressPasteUntil){event.preventDefault();event.stopPropagation();return;}const text=event.clipboardData?.getData('text/plain');if(text){event.preventDefault();event.stopPropagation();pasteText(text);term.focus();}},true);
            el.addEventListener('contextmenu',event=>{if(navigator.clipboard?.readText){event.preventDefault();event.stopPropagation();pasteClipboard();}});
            socket.onopen=()=>{if(!instance.disposed)socket.send(JSON.stringify({type:'terminal_connect',mode:terminalMode,worker_id:workerId,...(terminalMode==='adb'?{serial_no:serialNo}:{})}));};
            socket.onmessage=e=>{if(instance.disposed)return;try{const m=JSON.parse(e.data);if(m.type==='terminal_data')writeTerminalWorkspaceData(instance,m.data||'');else if(m.type==='terminal_connected'){if(terminalMode==='adb'){terminalWorkspaceStatus(index,'正在进入 ADB Shell…');clearTerminalWorkspaceStartupTimer(instance);if(!instance.shellReady)instance.startupTimer=setTimeout(()=>failTerminalWorkspaceStartup(instance,'ADB Shell 启动超时'),15000);}else{terminalWorkspaceStatus(index,`${user}@${address}`,true);}setTimeout(()=>resizeHostWorkspaceTerminal(instance),0);}else if(m.type==='terminal_error'||m.type==='error'){clearTerminalWorkspaceStartupTimer(instance);const error=m.error||m.message||'连接失败';if(terminalMode==='adb'&&!m.elevation_required&&!m.credential_required){failTerminalWorkspaceStartup(instance,'ADB Shell 连接失败',error);return;}term.writeln(`\r\n\x1b[31m${error}\x1b[0m`);if(m.elevation_required){terminalWorkspaceStatus(index,'等待管理员认证');recoverTerminalElevation(instance,'重新连接主机终端',()=>refreshTerminalWorkspacePane(index));}else if(m.credential_required&&m.device_host&&typeof showDevicePasswordModal==='function'){terminalWorkspaceStatus(index,'等待 SSH 凭据');showDevicePasswordModal(m.device_host,'terminal',()=>refreshTerminalWorkspacePane(index));}else{terminalWorkspaceStatus(index,'连接失败');}}}catch(_){}};
            socket.onclose=()=>{if(!instance.disposed){clearTerminalWorkspaceStartupTimer(instance);if(instance.startupFailed){terminalWorkspaceStatus(index,instance.startupFailureStatus||'ADB Shell 启动失败');return;}term.writeln('\r\n\x1b[31m⚠️ 连接已断开\x1b[0m');terminalWorkspaceStatus(index,'已断开');}};
            socket.onerror=()=>{if(!instance.disposed)terminalWorkspaceStatus(index,instance.startupFailed?(instance.startupFailureStatus||'ADB Shell 启动失败'):'连接错误');};instance.resizeObserver=new ResizeObserver(()=>resizeHostWorkspaceTerminal(instance));instance.resizeObserver.observe(body);
        }
        function clearTerminalWorkspaceStartupTimer(instance){
            if(instance?.startupTimer){clearTimeout(instance.startupTimer);instance.startupTimer=null;}
        }
        function terminalWorkspaceAdbStartupDetail(instance){
            const plain=String(instance?.initialData||'')
                .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g,'')
                .replace(/\x1b\[[0-?]*[ -\/]*[@-~]/g,'');
            const adbCommand=/\badb\s+-s\s+(?:"[^"]*"|'[^']*'|[^\s]+)\s+shell(?:\s|$)/.exec(plain);
            if(!adbCommand)return '';
            const lines=plain.slice(adbCommand.index+adbCommand[0].length).split(/\r\n|\n|\r/);
            const hostPrompt=lines.findIndex(line=>/^[^\s@]+@[^\s:]+:[^\r\n]*[$#]\s*$/.test(line));
            return lines.slice(0,hostPrompt<0?lines.length:hostPrompt)
                .map(line=>line.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g,'').trim())
                .filter(Boolean).join('\r\n').slice(-4096);
        }
        function terminalWorkspaceFailureStatus(reason,detail){
            const summary=String(detail||'').replace(/[\x00-\x1f\x7f]/g,' ').replace(/\s+/g,' ').trim();
            return summary?`${reason}：${summary.slice(0,180)}`:reason;
        }
        function failTerminalWorkspaceStartup(instance,reason,detail=''){
            if(!instance||instance.disposed||instance.shellReady||instance.startupFailed)return;
            clearTerminalWorkspaceStartupTimer(instance);
            instance.startupFailed=true;
            const failureDetail=detail||terminalWorkspaceAdbStartupDetail(instance);
            instance.startupFailureStatus=terminalWorkspaceFailureStatus(reason,failureDetail);
            instance.initialData='';
            instance.terminal.write(`\x1b[31m❌ ${reason}\x1b[0m${failureDetail?`\r\n\r\n${failureDetail}`:''}`);
            terminalWorkspaceStatus(instance.paneIndex,instance.startupFailureStatus);
            if(instance.socket?.readyState===WebSocket.OPEN)instance.socket.close();
        }
        function writeTerminalWorkspaceData(instance,data){
            if(instance.shellReady){instance.terminal.write(data);return;}
            if(instance.startupFailed)return;
            instance.initialData=(instance.initialData+String(data||'')).slice(-32768);
            const plain=instance.initialData
                .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g,'')
                .replace(/\x1b\[[0-?]*[ -\/]*[@-~]/g,'');
            const lines=plain.split(/\r\n|\n|\r/);
            if(instance.mode==='adb'){
                // The backend enters ADB through an interactive host shell.
                // Do not paint its welcome text, clear sequence, command echo,
                // and intermediate prompts. Reveal only once Android is ready.
                const adbCommand=/\badb\s+-s\s+(?:"[^"]*"|'[^']*'|[^\s]+)\s+shell(?:\s|$)/.exec(plain);
                if(!adbCommand)return;
                const adbLines=plain.slice(adbCommand.index+adbCommand[0].length).split(/\r\n|\n|\r/);
                const devicePromptIndex=adbLines.findIndex(line=>/^(?:[\w.-]+@)?[^\s:]+:\/[^\r\n]*[$#]\s*$/.test(line));
                const hostPromptIndex=adbLines.findIndex(line=>/^[^\s@]+@[^\s:]+:[^\r\n]*[$#]\s*$/.test(line));
                if(hostPromptIndex>=0&&(devicePromptIndex<0||hostPromptIndex<devicePromptIndex)){
                    failTerminalWorkspaceStartup(instance,'ADB Shell 启动失败',terminalWorkspaceAdbStartupDetail(instance));
                    return;
                }
                if(devicePromptIndex<0)return;
                const prompt=adbLines[devicePromptIndex];
                clearTerminalWorkspaceStartupTimer(instance);
                instance.shellReady=true;
                instance.initialData='';
                instance.terminal.write(`\x1b[32m✅ ADB Shell · ${instance.serialNo}\x1b[0m\r\n\r\n${prompt}`);
                terminalWorkspaceStatus(instance.paneIndex,`ADB · ${instance.serialNo}`,true);
                return;
            }
            const prompt=[...lines].reverse().find(line=>/@[^\s:]+:[^\r\n]*[$#]\s*$/.test(line));
            if(prompt){instance.shellReady=true;instance.initialData='';instance.terminal.write(prompt);}
        }
        function terminalWorkspaceRenderSignature(){return JSON.stringify({layout:terminalWorkspace.layout,panes:terminalWorkspace.panes,maximized:terminalWorkspace.maximized});}
        function setTerminalWorkspaceLayout(v){if(HOST_WORKSPACE_COUNTS[v]&&(hostWorkspaceClusterEnabled||v==='single')){terminalWorkspace.layout=v;terminalWorkspace.maximized=null;renderTerminalWorkspace();}}
        function changeTerminalWorkspaceHost(i,v){
            const pane=terminalWorkspace.panes[i],host=desktopHosts.find(item=>item.id===v),select=document.querySelector(`[data-terminal-pane="${i}"] select[aria-label="主机"]`);
            if(!pane||!host||host.offline){if(select&&pane)select.value=pane.hostId;return;}
            if(terminalWorkspace.panes.some((item,paneIndex)=>paneIndex!==i&&item.hostId===v)){
                if(select)select.value=pane.hostId;
                showToast('该主机已在其他终端窗格中连接','warning');
                return;
            }
            if(pane.hostId===v)return;
            terminalWorkspace.panes[i]={hostId:v};
            if(terminalWorkspace.layout==='single'){
                currentHost=host;
                updateWorkspaceForHost(host,'terminal');
            }
            saveTerminalWorkspace();
            terminalWorkspace.renderedSignature=terminalWorkspaceRenderSignature();
            terminalWorkspace.panes.forEach((item,paneIndex)=>{
                const paneSelect=document.querySelector(`[data-terminal-pane="${paneIndex}"] select[aria-label="主机"]`);
                if(paneSelect)paneSelect.innerHTML=hostWorkspaceHostOptions(item.hostId,terminalWorkspace.panes,paneIndex);
            });
            refreshTerminalWorkspacePane(i);
        }
        function refreshTerminalWorkspacePane(i){
            const pane=terminalWorkspace.panes[i],body=document.getElementById(`terminal-workspace-body-${i}`);if(!pane||!body)return;
            disposeTerminalWorkspaceInstance(i);
            const paneGeneration=(terminalWorkspace.paneGenerations.get(i)||0)+1;terminalWorkspace.paneGenerations.set(i,paneGeneration);
            // clone-replace 清除上一次 mount 残留的 dragover/drop 等事件监听器，
            // 避免刷新 N 次后拖放文件触发 N 次上传。
            body.replaceWith(body.cloneNode(false));
            terminalWorkspaceStatus(i,'正在刷新…');
            mountTerminalWorkspacePane(i,pane,terminalWorkspace.generation,paneGeneration);
        }
        function restoreHostTerminalWorkspacePane(i){
            const pane=terminalWorkspace.panes[i];if(!pane||pane.mode!=='adb')return;
            terminalWorkspace.panes[i]={hostId:pane.hostId};
            saveTerminalWorkspace();
            // Only reconnect the selected pane. Other terminals in a multi-pane
            // layout must keep their PTY/WebSocket sessions alive.
            terminalWorkspace.renderedSignature=terminalWorkspaceRenderSignature();
            const paneRoot=document.querySelector(`[data-terminal-pane="${i}"]`);
            const hostModeButton=paneRoot?.querySelector('[data-terminal-pane-host-mode]');
            if(hostModeButton)hostModeButton.style.display='none';
            const singleHostModeButton=document.getElementById('terminal-workspace-host-mode-btn');
            if(singleHostModeButton&&i===0)singleHostModeButton.style.display='none';
            refreshTerminalWorkspacePane(i);
        }
        function maximizeTerminalWorkspacePane(i){terminalWorkspace.maximized=terminalWorkspace.maximized===i?null:i;renderTerminalWorkspace();}
        function applyHostWorkspaceScopeMode(clusterEnabled) {
            const nextClusterEnabled = Boolean(clusterEnabled);
            // An ADB pane is a user-requested, one-shot target. Scope
            // initialization can race with openDeviceShell and otherwise
            // restore the saved SSH pane over it, disposing its WebSocket
            // before terminal_connect is sent.
            const activeAdbPane = terminalWorkspace.panes.find(pane =>
                pane?.mode === 'adb' && pane.serialNo && pane.workerId
            );
            // Workspace context also changes when a user selects a Worker.
            // Restoring the saved layout on every context event rolls the
            // first host selection back to the previous pane; only restore
            // layouts when the single/cluster scope itself actually changes.
            if (hostWorkspaceScopeModeInitialized && nextClusterEnabled === hostWorkspaceClusterEnabled) {
                return;
            }
            hostWorkspaceScopeModeInitialized = true;
            setHostWorkspaceSurfaceReady('desktop', false);
            setHostWorkspaceSurfaceReady('terminal', false);
            if (!nextClusterEnabled && hostWorkspaceClusterEnabled) {
                hostWorkspace.clusterState = snapshotHostClusterState();
                terminalWorkspace.clusterState = snapshotTerminalClusterState();
            }
            hostWorkspaceClusterEnabled = nextClusterEnabled;
            if (nextClusterEnabled) {
                if (hostWorkspace.clusterState) {
                    hostWorkspace.layout = hostWorkspace.clusterState.layout;
                    hostWorkspace.panes = hostWorkspace.clusterState.panes.map(pane => ({...pane}));
                    hostWorkspace.maximized = hostWorkspace.clusterState.maximized;
                }
                if (terminalWorkspace.clusterState) {
                    terminalWorkspace.layout = terminalWorkspace.clusterState.layout;
                    terminalWorkspace.panes = terminalWorkspace.clusterState.panes.map(pane => ({...pane}));
                    terminalWorkspace.maximized = terminalWorkspace.clusterState.maximized;
                }
            } else {
                useSingleHostWorkspaceState();
                useSingleTerminalWorkspaceState();
            }
            if (activeAdbPane && (nextClusterEnabled || isLocalWorkspaceWorker(activeAdbPane.workerId))) {
                const targetHost = workspaceHostForWorker(activeAdbPane.workerId);
                terminalWorkspace.layout = 'single';
                terminalWorkspace.maximized = null;
                terminalWorkspace.panes = [{
                    ...activeAdbPane,
                    // Preserve the pane identity while its WebSocket is
                    // mounting; changing hostId here changes the render
                    // signature and tears down the pending ADB connection.
                    ...(targetHost && !terminalWorkspace.instances.has(0)
                        && !terminalWorkspace.mountingPanes.has(0)
                        ? {hostId: targetHost.id} : {}),
                }];
            }
            if (window.hostWorkspaceInitialized) renderHostWorkspace();
            if (window.terminalWorkspaceInitialized) renderTerminalWorkspace();
        }
        Object.assign(window,{setTerminalWorkspaceLayout,changeTerminalWorkspaceHost,refreshTerminalWorkspacePane,restoreHostTerminalWorkspacePane,maximizeTerminalWorkspacePane,applyHostWorkspaceScopeMode});

        // ==================== 用户管理功能 ====================
        let usersRefreshInterval = null;
        let usersHasLoaded = false;
        let usersListCache = [];
        let usersStatusFilter = '';
        let usersLocalDevicesReloadTimer = null;
        // 用户取消提权后置位：自动刷新不再每 10 秒重复探测/弹框；
        // 提权成功或列表加载成功时清除。
        let usersAccessDenied = false;

        async function loadUsersList(elevationRetried = false) {
            const tbody = document.getElementById('users-table-body');
            if (tbody) tbody.setAttribute('aria-busy', 'true');
            try {
                // 本地提权状态已知为否时，先向服务端确认（新标签页可能
                // 丢失本地标记），仍未提权则直接进入提权流程，避免发出
                // 一次必然 403 的 /api/users/list 探测请求。
                if (!elevationRetried && state.authRequired && !state.elevated) {
                    let elevated = false;
                    try {
                        const status = await fetchAuthStatus();
                        if (status.elevated) {
                            _markElevated(status.elevated_until);
                            elevated = true;
                        }
                    } catch (error) {
                        debugLog('[Users] elevation status check failed; falling back to probe', error);
                    }
                    if (!elevated) {
                        const granted = window.requestElevatedAccess
                            ? await window.requestElevatedAccess('查看用户管理')
                            : false;
                        if (granted) {
                            usersAccessDenied = false;
                            return loadUsersList(true);
                        }
                        usersAccessDenied = true;
                        displayUsersListAccessMessage('需要管理员提权后查看用户管理');
                        return;
                    }
                }

                const resp = await fetch('/api/users/list', { credentials: 'same-origin' });
                const data = await resp.json();

                if (resp.status === 403 && data.detail?.elevation_required && !elevationRetried) {
                    const granted = window.requestElevatedAccess
                        ? await window.requestElevatedAccess('查看用户管理')
                        : false;
                    if (granted) {
                        usersAccessDenied = false;
                        return loadUsersList(true);
                    }
                    usersAccessDenied = true;
                    displayUsersListAccessMessage('需要管理员提权后查看用户管理');
                    return;
                }

                if (!resp.ok) {
                    const detail = typeof data.detail === 'object'
                        ? data.detail.message
                        : data.detail;
                    throw new Error(detail || data.error || `HTTP ${resp.status}`);
                }
                usersAccessDenied = false;
                if (data.users) {
                    displayUsersList(data.users);
                }
            } catch (e) {
                console.error('[Users] Error loading users:', e);
                if (usersHasLoaded) showToast(`用户列表刷新失败：${e.message}`, 'error');
                else displayUsersListAccessMessage(`用户列表加载失败：${e.message}`);
            } finally {
                if (tbody) tbody.setAttribute('aria-busy', 'false');
            }
        }

        function displayUsersListAccessMessage(message) {
            const tbody = document.getElementById('users-table-body');
            if (!tbody) return;
            tbody.innerHTML = `<tr><td colspan="9" class="users-empty-message">${escapeHtml(message)}</td></tr>`;
        }

        // 移除配置型用户（从 config_runtime.client_hosts 删除）。二次确认后调 DELETE。
        async function removeUser(ip, btn) {
            if (!ip) return;
            if (!await showConfirmDialog(
                '移除用户',
                `确定移除用户 ${ip} 吗？\n仅删除其配置映射，不影响当前会话；测试中的用户不可移除。`
            )) return;
            // Sensitive operation: require temporary admin elevation.
            const granted = window.requestElevatedAccess
                ? await window.requestElevatedAccess(`移除用户 ${ip}`)
                : true;
            if (!granted) return;
            if (btn) { btn.disabled = true; btn.textContent = '...'; }
            try {
                await apiCall('/api/users/remove', 'DELETE', { ip });
                showToast(`已移除用户 ${ip}`, 'success');
                loadUsersList();
            } catch (e) {
                showToast(`移除失败: ${e.message}`, 'error');
                if (btn) { btn.disabled = false; btn.textContent = '移除'; }
            }
        }

        // 启动用户列表自动刷新（仅当在 users 页面时）
        function startUsersAutoRefresh() {
            if (usersRefreshInterval) return;
            usersRefreshInterval = setInterval(() => {
                // 用户已取消提权时不再反复探测；重新进入页面会再次提示。
                if (currentPage === 'users'
                    && !document.hidden
                    && !usersAccessDenied
                    && !window.agentAccessPanelIsOpen?.()) {
                    loadUsersList();
                }
            }, 10000);
        }

        // 停止用户列表自动刷新
        function stopUsersAutoRefresh() {
            if (usersRefreshInterval) {
                clearInterval(usersRefreshInterval);
                usersRefreshInterval = null;
            }
            if (usersLocalDevicesReloadTimer) {
                clearTimeout(usersLocalDevicesReloadTimer);
                usersLocalDevicesReloadTimer = null;
            }
        }

        function getActiveUserClusterJob(user) {
            return (user.cluster_jobs || []).find(job =>
                ['created','queued','leasing','assigned','dispatching','running','stopping','collecting','worker_lost'].includes(job.status));
        }

        function getUserDisplayStatus(user) {
            return getActiveUserClusterJob(user)
                ? 'testing'
                : (user.status || (user.running ? 'testing' : 'offline'));
        }

        function setUsersStatusFilter(status) {
            usersStatusFilter = ['online', 'testing', 'offline'].includes(status) ? status : '';
            filterUsersList();
        }

        function filterUsersList() {
            const status = usersStatusFilter;
            const filtered = usersListCache.filter(user => !status || getUserDisplayStatus(user) === status);
            document.querySelectorAll('[data-user-status-card]').forEach(card => {
                card.classList.toggle('active', card.dataset.userStatusCard === status);
            });
            renderUsersList(filtered);
        }

        function displayUsersList(users) {
            const tbody = document.getElementById('users-table-body');
            if (!tbody) return;
            usersListCache = Array.isArray(users) ? users : [];

            // 更新统计数据
            const totalCount = usersListCache.length;
            const activeCount = usersListCache.filter(u => getUserDisplayStatus(u) === 'online').length;
            const testingCount = usersListCache.filter(u => getUserDisplayStatus(u) === 'testing').length;

            document.getElementById('total-users-count').textContent = totalCount;
            document.getElementById('active-users-count').textContent = activeCount;
            document.getElementById('testing-users-count').textContent = testingCount;
            filterUsersList();

            // 有用户直连设备仍在后台 SSH 枚举（显示"枚举中…"）时，
            // 3 秒后补一次加载，不等 10 秒轮询。
            if (currentPage === 'users'
                && !window.agentAccessPanelIsOpen?.()
                && usersListCache.some(user => user.local_devices === null)) {
                clearTimeout(usersLocalDevicesReloadTimer);
                usersLocalDevicesReloadTimer = setTimeout(() => {
                    if (currentPage === 'users' && !document.hidden && !window.agentAccessPanelIsOpen?.()) {
                        loadUsersList();
                    }
                }, 3000);
            }
        }

        function renderUsersList(users) {
            const tbody = document.getElementById('users-table-body');
            if (!tbody) return;
            if (users.length === 0) {
                const filtersActive = Boolean(usersStatusFilter);
                tbody.innerHTML = `
                    <tr>
                        <td colspan="9" class="users-empty-message">
                            ${filtersActive ? '没有匹配当前筛选条件的用户' : '暂无用户'}
                        </td>
                    </tr>
                `;
                usersHasLoaded = true;
                return;
            }

            // 渲染用户列表
            tbody.innerHTML = users.map(user => {
                const activeClusterJob = getActiveUserClusterJob(user);
                const normalizedStatus = getUserDisplayStatus(user);
                const statusLabels = { testing: '测试中', online: '在线', offline: '离线' };
                const statusColors = {
                    testing: 'var(--warning-color)',
                    online: 'var(--success-color)',
                    offline: 'var(--text-secondary)'
                };
                const statusText = activeClusterJob
                    ? `集群测试中 · ${activeClusterJob.worker_id || '-'}`
                    : (statusLabels[normalizedStatus] || '离线');
                const statusColor = statusColors[normalizedStatus] || 'var(--text-secondary)';

                const lastSeen = user.last_seen ? formatTime(user.last_seen) : '-';
                const createdAt = user.created_at ? formatTime(user.created_at) : '-';
                const devices = user.devices && user.devices.length > 0 ? user.devices.join(', ') : '-';
                const localInventory = user.local_devices || null;
                // local_devices 为 null 表示后台 SSH 枚举尚未完成（首次查看
                // 需数秒），显示"枚举中…"而非"-"；枚举完成后显示设备清单。
                const localDevicesPending = localInventory === null;
                const localDevices = localDevicesPending
                    ? '枚举中…'
                    : (localInventory.devices && localInventory.devices.length > 0
                        ? localInventory.devices.join(', ')
                        : '-');
                const localDevicesStyle = localDevicesPending
                    ? ' color: var(--text-secondary); font-style: italic;'
                    : '';
                const localDevicesTitle = localInventory && localInventory.available === false && localInventory.error
                    ? `title="${escapeHtml(localInventory.error)}"`
                    : '';
                const sourceLabel = user.source_label || '-';
                const sourceColor = user.source === 'internal' ? 'var(--primary-color)' : (user.source === 'public' ? 'var(--warning-color)' : 'var(--text-secondary)');
                const removeCell = renderUserRemoveCell(user, normalizedStatus);
                const clusterCell = activeClusterJob
                    ? `<button class="btn-xxs" style="width:44px;" data-cluster-job="${escapeHtml(activeClusterJob.id)}" data-worker-id="${escapeHtml(activeClusterJob.worker_id || '')}" data-attempt-id="${escapeHtml(activeClusterJob.attempt_id || '')}">任务</button>`
                    : '<span class="user-action-placeholder" aria-hidden="true">-</span>';

                return `
                    <tr>
                        <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;">
                            ${escapeHtml(user.client_id || '')}
                        </td>
                        <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;">
                            ${escapeHtml(user.ip || '')}
                        </td>
                        <td style="padding: 4px 6px; text-align: center; font-size: 12px; color: ${sourceColor}; font-weight: 600;">
                            ${escapeHtml(sourceLabel)}
                        </td>
                        <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;${localDevicesStyle}" ${localDevicesTitle}>
                            ${escapeHtml(localDevices)}
                        </td>
                        <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;">
                            ${escapeHtml(devices)}
                        </td>
                        <td style="padding: 4px 6px; text-align: center;">
                            <span style="color: ${statusColor}; font-weight: 600; font-size: 12px;">●</span>
                            <span style="margin-left: 6px; font-size: 12px;">${escapeHtml(statusText)}</span>
                        </td>
                        <td style="padding: 4px 6px; text-align: center; font-size: 12px;">
                            ${createdAt}
                        </td>
                        <td style="padding: 4px 6px; text-align: center; font-size: 12px;">
                            ${lastSeen}
                        </td>
                        <td style="padding: 4px 6px; text-align: center;">
                            <div class="user-actions-grid">
                                ${clusterCell}${removeCell}
                            </div>
                        </td>
                    </tr>
                `;
            }).join('');
            tbody.querySelectorAll('[data-cluster-job]').forEach(button => {
                button.addEventListener('click', () => window.GmsWorkspace?.navigate('cluster', {
                    scope_mode: 'cluster',
                    worker_id: button.dataset.workerId || workspaceLocalWorkerId(),
                    cluster_job_id: button.dataset.clusterJob || '',
                    attempt_id: button.dataset.attemptId || '',
                    origin_page: 'users'
                }));
            });
            tbody.querySelectorAll('[data-remove-user]').forEach(button => {
                button.addEventListener('click', () => removeUser(
                    button.dataset.removeUser || '',
                    button
                ));
            });
            usersHasLoaded = true;
        }

        function formatTime(isoString) {
            if (!isoString || isoString === '-') return '-';

            try {
                const date = new Date(isoString);
                const now = new Date();
                const diff = now - date;

                if (diff < 60000) { // 小于1分钟
                    return '刚刚';
                } else if (diff < 3600000) { // 小于1小时
                    const minutes = Math.floor(diff / 60000);
                    return `${minutes}分钟前`;
                } else if (diff < 86400000) { // 小于24小时
                    const hours = Math.floor(diff / 3600000);
                    return `${hours}小时前`;
                } else {
                    return date.toLocaleString('zh-CN', {
                        month: '2-digit',
                        day: '2-digit',
                        hour: '2-digit',
                        minute: '2-digit'
                    });
                }
            } catch (e) {
                return '-';
            }
        }

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

        // ===== 设备分组：交互与渲染（绑定设备管理页 allDevices / 表格）=====

        // 拉取分组定义，刷新筛选下拉与开关按钮态
        async function mgmtLoadGroups() {
            try {
                const res = await apiCall('/api/device-groups', 'GET');
                state.deviceGroups = res?.data?.groups || [];
            } catch (e) {
                console.error('[mgmtLoadGroups] error:', e);
                state.deviceGroups = [];
            }
            mgmtUpdateFilterDropdown();
            const btn = document.getElementById('btn-toggle-groupview');
            if (btn) btn.classList.toggle('active', state.groupView);
        }

        // 刷新"按组筛选"下拉
        function mgmtUpdateFilterDropdown() {
            const sel = document.getElementById('device-group-filter');
            if (!sel) return;
            const cur = state.groupFilter;
            sel.innerHTML = '<option value="">全部设备</option>' +
                managementGroupsForView().map(g =>
                    `<option value="${g.id}"${g.id === cur ? ' selected' : ''}>${escapeHtml(g.name)}</option>`
                ).join('');
            if (cur && !managementGroupsForView().some(group => group.id === cur)) {
                state.groupFilter = '';
                sel.value = '';
            }
        }

        function managementGroupsForView() {
            if (devicesManagementClusterMode) return state.deviceGroups || [];
            const visibleDeviceIds = new Set(allDevices.map(mgmtDeviceKey));
            return (state.deviceGroups || []).filter(group => {
                const memberIds = (group.device_ids || []).map(String);
                return memberIds.length === 0 || memberIds.some(id => visibleDeviceIds.has(id));
            });
        }

        // 按 currentSort 排序 allDevices（复用 sortDevices 的比较逻辑）
        function sortedAllDevices() {
            const field = currentSort.field;
            if (!field) return [...allDevices];
            const dir = currentSort.direction === 'asc' ? 1 : -1;
            return [...allDevices].sort((a, b) => {
                let aVal = a[field] || '';
                let bVal = b[field] || '';
                if (field === 'source_type') {
                    aVal = a.source_type === 'local' ? '0' : '1';
                    bVal = b.source_type === 'local' ? '0' : '1';
                } else if (field === 'status') {
                    aVal = a.status === 'online' ? '0' : '1';
                    bVal = b.status === 'online' ? '0' : '1';
                } else if (field === 'battery_level') {
                    return dir * ((parseInt(a.battery_level) || 0) - (parseInt(b.battery_level) || 0));
                }
                return dir * String(aVal).localeCompare(String(bVal), undefined, { numeric: true });
            });
        }

        // 筛选 + 排序后，交给渲染的设备列表
        function filteredDevicesForView() {
            let list = sortedAllDevices();
            if (state.groupFilter) {
                // 直接使用最新分组定义，远端设备没有 management API 的 groups 字段。
                const group = (state.deviceGroups || []).find(g => g.id === state.groupFilter);
                const memberIds = new Set((group?.device_ids || []).map(String));
                list = list.filter(d => memberIds.has(mgmtDeviceKey(d)));
            }
            return list;
        }

        // 本地设备以 serial 为主键；集群设备必须使用 worker:serial 命名空间，
        // 否则不同 Worker 的同名设备会冲突，且无法匹配后端自动分组结果。
        function mgmtDeviceKey(device) {
            return String(device?.device_id || device?.serial_no || '');
        }

        // 分组视图渲染：每个分组一行标题（colspan）+ 组内设备行；未分组归"未分组"段
        // 表头由表格 <thead> 统一提供（顶部唯一），分组内不再重复表头
        function renderDevicesManagementGrouped(devices, renderRow) {
            const entryBySerial = new Map(devices.map(d => [mgmtDeviceKey(d), d]));
            const sections = [];

            const groupsToShow = managementGroupsForView().filter(g =>
                !state.groupFilter || g.id === state.groupFilter
            );

            groupsToShow.forEach(group => {
                const members = (group.device_ids || [])
                    .map(id => entryBySerial.get(String(id)))
                    .filter(Boolean);
                if (members.length === 0 && state.groupFilter) return;
                sections.push(mgmtGroupTitleRow(group, members.length, false));
                if (!state.collapsedGroups.has(group.id)) {
                    sections.push(members.map(renderRow).join(''));
                }
            });

            // 全部模式下，未分组设备单独一段。
            // 以 state.deviceGroups.device_ids 为准，避免分组变更后设备重复显示。
            if (!state.groupFilter) {
                const assignedIds = new Set();
                (state.deviceGroups || []).forEach(g => {
                    (g.device_ids || []).forEach(id => assignedIds.add(String(id)));
                });
                const ungrouped = devices.filter(d => {
                    const id = mgmtDeviceKey(d);
                    return id && !assignedIds.has(id);
                });
                if (ungrouped.length > 0) {
                    const ug = { id: '__ungrouped__', name: '未分组', color: '#888', followed: false, device_ids: ungrouped.map(mgmtDeviceKey) };
                    sections.push(mgmtGroupTitleRow(ug, ungrouped.length, true));
                    if (!state.collapsedGroups.has('__ungrouped__')) {
                        sections.push(ungrouped.map(renderRow).join(''));
                    }
                }
            }
            return sections.join('');
        }

        function renderDevicesManagementByHost(devices, renderRow) {
            const byWorker = new Map();
            devices.forEach(device => {
                const workerId = String(device.worker_id || workspaceLocalWorkerId());
                if (!byWorker.has(workerId)) byWorker.set(workerId, []);
                byWorker.get(workerId).push(device);
            });
            const localWorkerId = workspaceLocalWorkerId();
            return [...byWorker.entries()]
                .sort(([left], [right]) => {
                    if (left === localWorkerId) return -1;
                    if (right === localWorkerId) return 1;
                    return left.localeCompare(right, undefined, {numeric: true});
                })
                .map(([workerId, members]) => {
                    const hostName = members[0]?.host_display_name || workerId;
                    const label = hostName === workerId
                        ? workerId
                        : `${hostName} · ${workerId}`;
                    const group = {
                        id: `__host__${workerId}`,
                        name: label,
                        color: '#42a5f5'
                    };
                    const collapsed = state.collapsedGroups.has(group.id);
                    return mgmtGroupTitleRow(group, members.length, true)
                        + (collapsed ? '' : members.map(renderRow).join(''));
                })
                .join('');
        }

        // 分组标题行（跨全部列）：组名/色块/计数/关注/编辑/删除/折叠
        function mgmtGroupTitleRow(group, count, isUngrouped) {
            const collapsed = state.collapsedGroups.has(group.id);
            const followed = !!group.followed;
            const toggle = `<span class="device-group-toggle" data-mgmt-toggle="${group.id}" style="cursor:pointer;">${collapsed ? '▶' : '▼'}</span>`;
            const color = `<span class="device-group-color" style="background:${group.color}"></span>`;
            const name = `<span class="device-group-name">${escapeHtml(group.name)}</span>`;
            const cnt = `<span class="device-group-count">${count}</span>`;
            const followBtn = isUngrouped ? '' :
                `<button class="btn-xxs" data-mgmt-follow="${group.id}" title="关注后主页ADB设备区只显示关注的分组" style="padding:2px 6px;font-size:11px;${followed ? 'background:var(--warning-color);' : ''}">${followed ? '★ 已关注' : '☆ 关注'}</button>`;
            const ops = isUngrouped ? '' :
                `<span style="margin-left:auto; display:flex; gap:4px;">
                    ${followBtn}
                    <button class="btn-xxs" data-mgmt-edit="${group.id}" style="padding:2px 6px;font-size:11px;">✏️ 编辑</button>
                    <button class="btn-xxs" data-mgmt-del="${group.id}" style="padding:2px 6px;font-size:11px;">🗑️ 删除</button>
                </span>`;
            return `<tr class="device-group-row" style="background: var(--lighter-bg);">
                <td colspan="10" style="padding:6px 8px;">
                    <div class="device-group-header" style="background:transparent;border:none;">${toggle}${color}${name}${cnt}${ops}</div>
                </td>
            </tr>`;
        }

        // 表格事件委托：折叠/编辑/删除（仅绑定一次）
        function setupMgmtGroupDelegation() {
            const tbody = document.getElementById('devices-table-body');
            if (!tbody || tbody._mgmtDelegated) return;
            tbody._mgmtDelegated = true;
            tbody.addEventListener('click', async (e) => {
                const tEl = e.target.closest('[data-mgmt-toggle]');
                if (tEl) {
                    const gid = tEl.dataset.mgmtToggle;
                    if (state.collapsedGroups.has(gid)) state.collapsedGroups.delete(gid);
                    else state.collapsedGroups.add(gid);
                    localStorage.setItem('gms_collapsed_groups', JSON.stringify([...state.collapsedGroups]));
                    displayDevicesManagement(filteredDevicesForView());
                    return;
                }
                const editEl = e.target.closest('[data-mgmt-edit]');
                if (editEl) { openEditGroupModal(editEl.dataset.mgmtEdit); return; }
                const followEl = e.target.closest('[data-mgmt-follow]');
                if (followEl) {
                    const gid = followEl.dataset.mgmtFollow;
                    const g = (state.deviceGroups || []).find(x => x.id === gid);
                    if (!g) return;
                    const nextFollowed = !g.followed;
                    try {
                        await apiCall('/api/device-groups', 'POST', { action: 'update', id: gid, followed: nextFollowed });
                        await mgmtLoadGroups();
                        displayDevicesManagement(filteredDevicesForView());
                        showToast(nextFollowed ? '已关注，主页ADB区将只显示关注分组' : '已取消关注', 'success');
                    } catch (err) { showToast(`操作失败: ${err.message}`, 'error'); }
                    return;
                }
                const delEl = e.target.closest('[data-mgmt-del]');
                if (delEl) {
                    const gid = delEl.dataset.mgmtDel;
                    const g = (state.deviceGroups || []).find(x => x.id === gid);
                    if (!g) return;
                    if (!await showConfirmDialog(
                        '删除设备分组',
                        `确定删除分组「${g.name}」？\n组内设备将变为未分组。`
                    )) return;
                    try {
                        await apiCall('/api/device-groups', 'POST', { action: 'delete', id: gid });
                        // 同步刷新设备列表的 groups 字段，否则被删组里的设备会同时
                        // 残留在未分组段直到下次轮询
                        await loadDevicesManagement();
                        showToast('分组已删除', 'success');
                    } catch (err) { showToast(`删除失败: ${err.message}`, 'error'); }
                }
            });
        }

        // ===== 工具栏交互 =====
        function toggleGroupView() {
            state.groupView = !state.groupView;
            localStorage.setItem('gms_group_view', state.groupView ? '1' : '0');
            const btn = document.getElementById('btn-toggle-groupview');
            if (btn) btn.classList.toggle('active', state.groupView);
            setupMgmtGroupDelegation();
            displayDevicesManagement(filteredDevicesForView());
        }
        window.toggleGroupView = toggleGroupView;

        function onGroupFilterChange() {
            const sel = document.getElementById('device-group-filter');
            state.groupFilter = sel ? sel.value : '';
            displayDevicesManagement(filteredDevicesForView());
        }
        window.onGroupFilterChange = onGroupFilterChange;

        // ===== 新建/编辑分组弹框 =====
        function openCreateGroupModal() {
            const modal = document.getElementById('device-group-modal');
            if (!modal) return;
            document.getElementById('device-group-modal-title').textContent = '新建分组';
            document.getElementById('device-group-edit-id').value = '';
            document.getElementById('device-group-name').value = '';
            // 预填：筛选模式下预选当前筛选组的成员；否则预选当前可见设备
            const selectedGroup = (state.deviceGroups || []).find(g => g.id === state.groupFilter);
            const preset = selectedGroup ? selectedGroup.device_ids || [] : [];
            renderGroupDevicePicker(preset);
            ModalManager.open('device-group-modal');
            setTimeout(() => document.getElementById('device-group-name').focus(), 50);
        }
        window.openCreateGroupModal = openCreateGroupModal;

        function openEditGroupModal(groupId) {
            const group = (state.deviceGroups || []).find(g => g.id === groupId);
            if (!group) return;
            const modal = document.getElementById('device-group-modal');
            if (!modal) return;
            document.getElementById('device-group-modal-title').textContent = '编辑分组';
            document.getElementById('device-group-edit-id').value = group.id;
            document.getElementById('device-group-name').value = group.name;
            renderGroupDevicePicker(group.device_ids);
            ModalManager.open('device-group-modal');
        }

        // 弹框设备勾选列表：全部已知设备（在线 + 组内离线）
        function renderGroupDevicePicker(checkedIds) {
            const container = document.getElementById('device-group-picklist');
            if (!container) return;
            const checked = new Set((checkedIds || []).map(String));
            const onlineIds = allDevices.map(mgmtDeviceKey);
            const known = new Set([...onlineIds, ...checked]);
            const onlineSet = new Set(onlineIds);
            const html = [...known].map(id => {
                const isOnline = onlineSet.has(id);
                return `<label class="group-pick-item">
                    <input type="checkbox" value="${escapeHtml(id)}" ${checked.has(id) ? 'checked' : ''}/>
                    <span>${escapeHtml(id)}</span>
                    ${isOnline ? '' : '<span class="group-pick-off" title="当前离线">（离线）</span>'}
                </label>`;
            }).join('');
            container.innerHTML = html || '<div class="empty-message">暂无设备</div>';
        }

        async function submitDeviceGroup() {
            const id = document.getElementById('device-group-edit-id').value.trim();
            const name = document.getElementById('device-group-name').value.trim();
            if (!name) { showToast('请输入分组名称', 'error'); return; }
            const picked = [...document.getElementById('device-group-picklist')
                .querySelectorAll('input[type=checkbox]:checked')].map(cb => cb.value);
            const body = id
                ? { action: 'update', id, name, device_ids: picked }
                : { action: 'create', name, device_ids: picked };
            try {
                await apiCall('/api/device-groups', 'POST', body);
                showToast(id ? '分组已更新' : '分组已创建', 'success');
                ModalManager.close('device-group-modal');
                // 同步刷新设备列表的 groups 字段，避免设备在组段与未分组段重复显示
                await loadDevicesManagement();
            } catch (e) {
                showToast(`保存失败: ${e.message}`, 'error');
            }
        }
        window.submitDeviceGroup = submitDeviceGroup;

        // act-bridge 委托目标（替代历史 inline 多语句 handler）。
        function autoGroupAndReset(value) {
            autoGroupDevices(value);
            // 重置回占位项
            this.value = '';
        }
        function refreshUsbipSourceLists() {
            loadUsbipSourceDevices();
            loadUsbipAssignments();
        }
        function copyTailscaleInstallCmd() {
            copyText('curl -fsSL https://tailscale.com/install.sh | sh', {successMsg: '✓ 安装命令已复制'});
        }
        async function autoGroupDevices(by) {
            if (!by) return;
            try {
                showToast(by === 'worker' ? '正在按主机分组...' : `正在按 ${by} 分组，需读取设备属性，请稍候...`, 'info');
                await apiCall('/api/device-groups/auto', 'POST', { by });
                // 刷新设备和分组数据，避免设备重复显示。
                await loadDevicesManagement();
                setupMgmtGroupDelegation();
                showToast('自动分组完成', 'success');
            } catch (e) {
                showToast(`自动分组失败: ${e.message}`, 'error');
            }
        }
        window.autoGroupDevices = autoGroupDevices;

        async function forceReleaseDeviceLock(serialNo) {
            if (!serialNo || serialNo === '-') {
                showToast('设备序列号无效，无法释放', 'error');
                return;
            }
            const confirmed = await showConfirmDialog(
                '释放设备',
                `确定要强制释放设备 ${serialNo} 的占用锁吗？`
            );
            if (!confirmed) return;
            const granted = await requestElevatedAccess(
                `强制释放设备 ${serialNo}`
            );
            if (!granted) return;

            try {
                await apiCall(
                    '/api/devices/force-release',
                    'POST',
                    { device_id: serialNo }
                );
                showToast('设备占用锁已释放', 'success');
                await loadDevicesManagement();
                if (typeof refreshDevices === 'function') {
                    refreshDevices();
                }
            } catch (error) {
                console.error('[Devices] Force release failed:', error);
                showToast(`释放设备失败: ${error.message}`, 'error');
            }
        }

        // 打开设备ADB Shell
        let pendingAdbDevice = null; // 保存待连接的设备序列号

        async function openDeviceShell(serialNo, requestedWorkerId = '') {
            try {
                if (!await ensureTerminalElevation()) return;
                if (!serialNo || serialNo === '-') {
                    showToast('设备序列号无效，无法打开 Shell', 'error');
                    return;
                }

                const device = allDevices.find(item =>
                    item.device_id === serialNo || item.serial_no === serialNo);
                const compositeWorkerId = serialNo.includes(':') ? serialNo.split(':', 1)[0] : '';
                const workerId = requestedWorkerId || device?.worker_id
                    || (desktopHosts.some(host => host.worker_id === compositeWorkerId)
                        ? compositeWorkerId : workspaceLocalWorkerId());
                const rawSerial = serialNo.startsWith(`${workerId}:`) ? serialNo.slice(workerId.length + 1) : serialNo;

                // Build the host list once, reusing the device page's cached
                // cluster directory. The visible terminal pane connects in ADB
                // mode directly; no timer or shell-command injection is needed.
                if (!desktopHosts.some(host => host.id === 'default')) {
                    // Do not let a local ADB pane race the asynchronous host
                    // directory merge. That merge can redraw the terminal
                    // workspace after its WebSocket opens; the disposed pane
                    // then never sends terminal_connect. The local shortcut
                    // used to be safe only before the workspace renderer
                    // began preserving/replacing panes asynchronously.
                    await initDesktopHosts();
                    window.desktopHostsInitialized = true;
                } else if (typeof mergeClusterDesktopHosts === 'function') {
                    await mergeClusterDesktopHosts();
                }
                const targetHost = isLocalWorkspaceWorker(workerId)
                    ? desktopHosts.find(h => h.id === 'default')
                    : desktopHosts.find(h => h.worker_id === workerId);
                if (!targetHost || targetHost.offline) {
                    throw new Error(`Worker ${workerId} 缺少可用的 SSH 主机信息`);
                }
                window.GmsWorkspace?.update({
                    worker_id: workerId,
                    origin_page: 'terminal'
                }, {source: 'device-shell'});
                showToast(`正在打开设备 ${rawSerial} 的 Shell...`, 'info');
                // ADB Shell 跳转仅打开目标设备的单窗格。
                initializeTerminalWorkspaceState();
                terminalWorkspace.layout = 'single';
                terminalWorkspace.maximized = null;
                terminalWorkspace.panes = [{
                    hostId: targetHost.id,
                    mode: 'adb',
                    serialNo: rawSerial,
                    workerId
                }];
                terminalWorkspace.pendingAdbTarget = {
                    serialNo: rawSerial,
                    workerId,
                };
                terminalWorkspace.renderedSignature = null;
                // 先隐藏旧终端画布再切换页面，避免 renderTerminalWorkspace
                // 异步执行期间暴露上一次终端的残留内容。
                setHostWorkspaceSurfaceReady('terminal', false);
                switchPage('terminal');
            } catch (error) {
                console.error('打开 Shell 失败:', error);
                showToast(`打开 Shell 失败: ${error.message}`, 'error');
            }
        }

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

        // ==================== 设备 config 资源查看（弹框）====================
        let dcfgDeviceSerial = null;
        let dcfgWorkerId = 'ats-worker-controller';
        let dcfgRequestGeneration = 0;
        let dcfgQueryRequestId = 0;
        let dcfgOverrideLoadId = 0;

        function dcfgCaptureContext() {
            const workerId = dcfgWorkerId || workspaceLocalWorkerId();
            const serial = dcfgDeviceSerial || '';
            const remote = Boolean(workerId && !isLocalWorkspaceWorker(workerId));
            return Object.freeze({
                generation: dcfgRequestGeneration,
                workerId,
                serial,
                remote,
                storageId: remote ? `${workerId}:${serial}` : serial
            });
        }

        function dcfgContextIsCurrent(context) {
            return Boolean(
                context
                && context.generation === dcfgRequestGeneration
                && context.workerId === dcfgWorkerId
                && context.serial === dcfgDeviceSerial
            );
        }

        function dcfgContextLabel(context = dcfgCaptureContext()) {
            return context.remote ? `${context.workerId} · ${context.serial}` : context.serial;
        }

        function dcfgSetButtonBusy(buttonId, busy, idleLabel, busyLabel = '刷新中…') {
            const button = document.getElementById(buttonId);
            if (!button) return;
            button.disabled = busy;
            button.textContent = busy ? busyLabel : idleLabel;
            if (busy) button.setAttribute('aria-busy', 'true');
            else button.removeAttribute('aria-busy');
        }

        async function openDeviceConfigExplorer(serialNo, workerId = '') {
            if (!serialNo || serialNo === '-') {
                showToast('设备序列号无效', 'error');
                return;
            }
            dcfgRequestGeneration += 1;
            if (dcfgOvrPollTimer) {
                clearTimeout(dcfgOvrPollTimer);
                dcfgOvrPollTimer = null;
            }
            dcfgDeviceSerial = serialNo;
            const managed = allDevices.find(item =>
                item.device_id === serialNo || item.serial_no === serialNo);
            dcfgWorkerId = workerId || window.event?.currentTarget?.dataset?.worker ||
                managed?.worker_id || workspaceLocalWorkerId();
            if (!isLocalWorkspaceWorker(dcfgWorkerId) && serialNo.startsWith(`${dcfgWorkerId}:`)) {
                dcfgDeviceSerial = serialNo.slice(dcfgWorkerId.length + 1);
            }
            const context = dcfgCaptureContext();
            document.getElementById('device-config-serial').textContent = dcfgContextLabel(context);
            // 重置为安全默认值
            document.getElementById('dcfg-package').value = 'android';
            document.getElementById('dcfg-package-list').replaceChildren();
            document.getElementById('dcfg-name').value = '';
            document.getElementById('dcfg-type').value = '';
            document.getElementById('dcfg-effective').checked = false;
            document.getElementById('dcfg-pkg-filter').value = '';
            document.getElementById('dcfg-feat-filter').value = '';
            document.getElementById('dcfg-prop-filter').value = '';
            document.getElementById('dcfg-ovr-name').value = '';
            document.getElementById('dcfg-ovr-type').value = 'string';
            document.getElementById('dcfg-ovr-value').value = '';
            document.getElementById('dcfg-ovr-value-arr').value = '';
            dcfgOvrTypeChanged();
            // 重置 tab 状态，默认进 configs，packages/features/props/overrides 懒加载
            dcfgPkgLoaded = false; dcfgFeatLoaded = false; dcfgPropLoaded = false; dcfgOvrLoaded = false;
            dcfgPkgRows = []; dcfgFeatRows = []; dcfgPropRows = [];
            dcfgPkgLoading = 0; dcfgFeatLoading = 0; dcfgPropLoading = 0;
            dcfgOvrStatus = null; dcfgOvrEntries = []; dcfgOvrBusyUntil = 0;
            dcfgQueryRequestId += 1;
            dcfgOverrideLoadId += 1;
            dcfgSetButtonBusy('dcfg-query', false, '查询');
            for (const refreshId of [
                'dcfg-pkg-refresh', 'dcfg-feat-refresh',
                'dcfg-prop-refresh', 'dcfg-ovr-refresh'
            ]) dcfgSetButtonBusy(refreshId, false, '↻ 刷新');
            for (const [resultId, emptyText] of [
                ['dcfg-results', '正在查询…'],
                ['dcfg-pkg-results', '点击「刷新」加载'],
                ['dcfg-feat-results', '点击「刷新」加载'],
                ['dcfg-prop-results', '点击「刷新」加载'],
                ['dcfg-ovr-results', '点击「刷新」加载当前覆盖项']
            ]) {
                const result = document.getElementById(resultId);
                if (result) {
                    result.dataset.renderGeneration = String(
                        Number(result.dataset.renderGeneration || 0) + 1
                    );
                    delete result.dataset.loaded;
                    result.setAttribute('aria-busy', 'false');
                    result.classList.add('dcfg-empty');
                    result.textContent = emptyText;
                }
            }
            for (const statId of ['dcfg-pkg-stat', 'dcfg-feat-stat', 'dcfg-prop-stat']) {
                const stat = document.getElementById(statId);
                if (stat) stat.textContent = '';
            }
            const overrideStatus = document.getElementById('dcfg-ovr-status');
            if (overrideStatus) overrideStatus.textContent = '';
            const overrideSummary = document.getElementById('dcfg-ovr-status-summary');
            if (overrideSummary) overrideSummary.textContent = '';
            const overridePreview = document.getElementById('dcfg-ovr-preview-xml');
            if (overridePreview) overridePreview.textContent = '（未加载）';
            dcfgSwitchTab('configs');
            const minimized = document.getElementById('device-config-minimized');
            if (minimized) minimized.style.display = 'none';
            ModalManager.open('device-config-modal');
            // 加载该设备可用的包列表（configs tab 的 datalist）
            loadDeviceConfigPackages(context);
            // 默认只查询 APK 默认值；overlay 生效值由用户按需开启。
            runDeviceConfigQuery(context);
        }

        function dcfgIsRemote(context = null) {
            const selectedWorker = context?.workerId || dcfgWorkerId;
            return Boolean(selectedWorker && !isLocalWorkspaceWorker(selectedWorker));
        }

        function dcfgStorageDeviceId(context = null) {
            if (context) return context.storageId;
            return dcfgIsRemote() ? `${dcfgWorkerId}:${dcfgDeviceSerial}` : (dcfgDeviceSerial || '');
        }

        async function dcfgInspect(action, values = {}, context = dcfgCaptureContext()) {
            if (!context.remote) throw new Error('dcfgInspect 仅用于远端 Worker');
            if (!dcfgContextIsCurrent(context)) throw new Error('Device Info 已切换到其他设备');
            return executeClusterCommandAndWait({
                worker_id: context.workerId,
                devices: [context.serial],
                action,
                ...values,
            }, () => dcfgContextIsCurrent(context));
        }

        function closeDeviceConfigExplorer() {
            ModalManager.close('device-config-modal');
            const minimized = document.getElementById('device-config-minimized');
            if (minimized) minimized.style.display = 'none';
            dcfgRequestGeneration += 1;
            dcfgDeviceSerial = null;
            if (dcfgOvrPollTimer) {
                clearTimeout(dcfgOvrPollTimer);
                dcfgOvrPollTimer = null;
            }
        }

        function minimizeDeviceConfigExplorer() {
            if (!dcfgDeviceSerial) return;
            ModalManager.close('device-config-modal');
            const minimized = document.getElementById('device-config-minimized');
            const title = document.getElementById('device-config-minimized-title');
            if (title) title.textContent = `${dcfgContextLabel()} · ${dcfgCurrentTab}`;
            if (minimized) minimized.style.display = 'flex';
        }

        function restoreDeviceConfigExplorer() {
            const minimized = document.getElementById('device-config-minimized');
            if (minimized) minimized.style.display = 'none';
            if (dcfgDeviceSerial) ModalManager.open('device-config-modal');
        }

        // ===== Tab 切换 =====
        let dcfgCurrentTab = 'configs';
        let dcfgPkgLoaded = false, dcfgFeatLoaded = false, dcfgPropLoaded = false;
        let dcfgOvrLoaded = false;

        function dcfgSwitchTab(tab) {
            dcfgCurrentTab = tab;
            document.querySelectorAll('#device-config-modal .dcfg-tab').forEach(el => {
                el.classList.toggle('active', el.id === 'dcfg-tab-' + tab);
            });
            document.querySelectorAll('#dcfg-tabs .dcfg-tabbtn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.tab === tab);
            });
            const title = document.getElementById('device-config-minimized-title');
            if (title && dcfgDeviceSerial) title.textContent = `${dcfgContextLabel()} · ${tab}`;
            // 懒加载：先完成 tab 切换，再在下一帧拉取/渲染大表，避免切换点击卡顿
            const context = dcfgCaptureContext();
            if (tab === 'packages' && !dcfgPkgLoaded) { dcfgPkgLoaded = true; setTimeout(() => loadDevicePackagesF(context), 0); }
            if (tab === 'features' && !dcfgFeatLoaded) { dcfgFeatLoaded = true; setTimeout(() => loadDeviceFeatures(context), 0); }
            if (tab === 'props' && !dcfgPropLoaded) { dcfgPropLoaded = true; setTimeout(() => loadDeviceProps(context), 0); }
            if (tab === 'overrides' && !dcfgOvrLoaded) { dcfgOvrLoaded = true; setTimeout(() => loadDeviceOverrides(context), 0); }
        }

        // ==================== override（RRO config 覆盖）====================
        let dcfgOvrStatus = null;     // 最近一次 status 快照
        let dcfgOvrBusyUntil = 0;     // apply/revert 后禁用按钮的时间戳(ms)
        let dcfgOvrEntries = [];

        async function dcfgOvrApi(path, init, context = dcfgCaptureContext()) {
            if (!dcfgContextIsCurrent(context)) throw new Error('Device Info 已切换到其他设备');
            if (context.remote && [
                '/api/config-override/status', '/api/config-override/apply',
                '/api/config-override/revert', '/api/config-override/disable-verity',
                '/api/config-override/enable-verity', '/api/config-override/reboot'
            ].includes(path)) {
                const actions = {
                    '/api/config-override/status': 'override_status',
                    '/api/config-override/apply': 'override_apply',
                    '/api/config-override/revert': 'override_revert',
                    '/api/config-override/disable-verity': 'override_disable_verity',
                    '/api/config-override/enable-verity': 'override_enable_verity',
                    '/api/config-override/reboot': 'override_reboot'
                };
                try {
                    const result = await dcfgInspect(actions[path], {
                        entries: dcfgOvrEntries,
                        target_package: 'android'
                    }, context);
                    return {success: true, data: result};
                } catch (error) {
                    return {success: false, error: error.message || String(error)};
                }
            }
            const qs = `device_id=${encodeURIComponent(context.storageId)}`;
            const sep = path.includes('?') ? '&' : '?';
            return apiCall(path + sep + qs, init?.method || 'GET');
        }

        async function dcfgRequireElevation(actionLabel) {
            return requestElevatedAccess(actionLabel, {allowAnonymousDev: true});
        }

        function dcfgDebounce(fn, delay = 120) {
            let timer = null;
            return function(...args) {
                clearTimeout(timer);
                timer = setTimeout(() => fn.apply(this, args), delay);
            };
        }

        function dcfgRenderRows(tbody, rows, renderRow, chunkSize = 250, onComplete = null) {
            let index = 0;
            const append = () => {
                const end = Math.min(index + chunkSize, rows.length);
                let html = '';
                for (; index < end; index++) html += renderRow(rows[index]);
                tbody.insertAdjacentHTML('beforeend', html);
                if (index < rows.length) {
                    requestAnimationFrame(append);
                } else if (onComplete) {
                    onComplete();
                }
            };
            append();
        }

        function dcfgSetTable(box, colgroup, headerHtml, rows, renderRow, chunkSize = 250) {
            const renderGeneration = Number(box.dataset.renderGeneration || 0) + 1;
            box.dataset.renderGeneration = String(renderGeneration);
            const table = document.createElement('table');
            table.style.cssText = 'width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed;';
            table.innerHTML = `
                ${colgroup}
                <thead><tr>${headerHtml}</tr></thead>
                <tbody></tbody>
            `;
            const tbody = table.querySelector('tbody');
            if (!tbody) return;
            // 在脱离 DOM 的 table 中分块构建，完成后一次替换。这既保留
            // 大数据量分帧渲染，又不会在用户眼前先清空旧表格。
            dcfgRenderRows(tbody, rows, renderRow, chunkSize, () => {
                if (Number(box.dataset.renderGeneration || 0) !== renderGeneration) return;
                box.classList.remove('dcfg-empty');
                box.replaceChildren(table);
                box.dataset.loaded = 'true';
            });
        }

        // 根据类型切换 值输入控件：数组类型用 textarea，其余用 input
        function dcfgOvrTypeChanged() {
            const t = document.getElementById('dcfg-ovr-type').value;
            const isArr = t === 'integer-array' || t === 'string-array' || t === 'array';
            document.getElementById('dcfg-ovr-value').style.display = isArr ? 'none' : '';
            document.getElementById('dcfg-ovr-value-arr').style.display = isArr ? '' : 'none';
        }

        function dcfgOvrValueInput() {
            const t = document.getElementById('dcfg-ovr-type').value;
            const isArr = t === 'integer-array' || t === 'string-array' || t === 'array';
            const el = document.getElementById(isArr ? 'dcfg-ovr-value-arr' : 'dcfg-ovr-value');
            return el.value;
        }

        function dcfgOvrChip(ok, label) {
            const cls = ok ? 'dcfg-ovr-ok' : 'dcfg-ovr-bad';
            const icon = ok ? '✓' : '✗';
            return `<span class="dcfg-ovr-chip ${cls}">${icon} ${dcfgEscapeHtml(label)}</span>`;
        }

        function dcfgRenderOvrStatus(s) {
            dcfgOvrStatus = s;
            const box = document.getElementById('dcfg-ovr-status');
            if (!s || !s.reachable) {
                box.innerHTML = '<span class="dcfg-ovr-chip dcfg-ovr-bad">✗ 设备不可达</span>';
                document.getElementById('dcfg-ovr-status-summary').textContent = '';
                return;
            }
            const entryCount = s.configured_entry_count ?? s.applied_entry_count;
            const chips = [
                dcfgOvrChip(s.is_userdebug, `build=${s.build_type || '?'}`),
                dcfgOvrChip(s.verity_disabled, `verity=${s.verity_disabled ? 'disabled' : 'enforcing'}`),
                dcfgOvrChip(s.rooted, `root=${s.rooted ? 'yes' : 'no'}`),
                dcfgOvrChip(s.product_remountable, `/product ${s.product_remountable ? 'rw' : 'ro'}`),
                dcfgOvrChip(s.overlay_installed, `overlay=${s.overlay_installed ? '已装' : '未装'}`),
            ].join(' ');
            let warn = '';
            if (!s.is_userdebug) {
                warn = '<div class="dcfg-ovr-warn">⚠ 需要 userdebug/eng 构建（user 版无法 adb root）</div>';
            } else if (!s.verity_disabled) {
                warn = '<div class="dcfg-ovr-warn">⚠ dm-verity 未关闭。点上方「🔓 关闭verity」按钮执行（apply 前的一次性步骤，会重启设备）。</div>';
            } else if (!s.product_remountable) {
                warn = '<div class="dcfg-ovr-warn">⚠ /product 不可写（remount 失败）</div>';
            }
            box.innerHTML = chips + warn;
            document.getElementById('dcfg-ovr-status-summary').textContent =
                entryCount != null
                    ? `已配置 ${entryCount} 项；Overlay ${s.overlay_installed ? '已安装' : '未安装'}`
                    : `Overlay ${s.overlay_installed ? '已安装' : '未安装'}`;
        }

        async function loadDeviceOverrides(context = dcfgCaptureContext()) {
            if (!dcfgContextIsCurrent(context) || !context.serial) return;
            const loadId = ++dcfgOverrideLoadId;
            dcfgSetButtonBusy('dcfg-ovr-refresh', true, '↻ 刷新');
            const box = document.getElementById('dcfg-ovr-results');
            const hadRenderedEntries = box.dataset.loaded === 'true';
            box.setAttribute('aria-busy', 'true');
            if (!hadRenderedEntries) {
                box.classList.add('dcfg-empty');
                box.innerHTML = '加载中…';
            }
            try {
                // 并发拉 status + entries + preview；任一接口失败不阻断其它结果显示
                const [stR, enR, pvR] = await Promise.all([
                    dcfgOvrApi('/api/config-override/status', undefined, context).catch(e => ({success:false, error:e.message})),
                    dcfgOvrApi('/api/config-override/entries', undefined, context).catch(e => ({success:false, error:e.message})),
                    dcfgOvrApi('/api/config-override/preview-xml', undefined, context).catch(e => ({success:false, error:e.message})),
                ]);
                if (!dcfgContextIsCurrent(context) || loadId !== dcfgOverrideLoadId) return;
                dcfgRenderOvrStatus(stR.success ? stR.data : null);
                if (pvR.success) {
                    document.getElementById('dcfg-ovr-preview-xml').textContent =
                        (pvR.data.manifest || '') + '\n\n' + (pvR.data.config_xml || '（无覆盖项）');
                }
                if (!enR.success) {
                    if (hadRenderedEntries) {
                        showToast('覆盖项刷新失败: ' + (enR.error || '加载失败'), 'error');
                    } else {
                        box.innerHTML = '❌ ' + dcfgEscapeHtml(enR.error || '加载失败');
                    }
                    return;
                }
                dcfgRenderOvrEntries(enR.data.entries || []);
            } finally {
                if (dcfgContextIsCurrent(context) && loadId === dcfgOverrideLoadId) {
                    dcfgSetButtonBusy('dcfg-ovr-refresh', false, '↻ 刷新');
                    box.setAttribute('aria-busy', 'false');
                }
            }
        }

        function dcfgRenderOvrEntries(entries) {
            dcfgOvrEntries = Array.isArray(entries) ? entries : [];
            const box = document.getElementById('dcfg-ovr-results');
            if (!dcfgOvrEntries.length) {
                box.classList.add('dcfg-empty');
                box.innerHTML = '暂无覆盖项。在上方添加（资源名+类型+值），再点「应用」';
                box.dataset.loaded = 'true';
                return;
            }
            box.classList.remove('dcfg-empty');
            const rows = dcfgOvrEntries.map(e => {
                const val = dcfgEscapeHtml(e.value || '');
                return `<tr>
                    <td><div class="dcell" title="${dcfgEscapeHtml(e.resource_name)}">${dcfgEscapeHtml(e.resource_name)}</div></td>
                    <td><span class="dcfg-type-badge">${dcfgEscapeHtml(e.resource_type)}</span></td>
                    <td><div class="dcell" title="${val}">${val.replace(/\n/g, ' ⏎ ')}</div></td>
                    <td>
                        <button class="btn-xxs" data-click="dcfgRemoveOverride" data-a0="${dcfgEscapeHtml(e.resource_name)}">删除</button>
                    </td>
                </tr>`;
            }).join('');
            box.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed;">
                <colgroup><col style="width:32%"><col style="width:12%"><col style="width:42%"><col style="width:14%"></colgroup>
                <thead><tr>
                    <th class="dcfg-th">资源名</th><th class="dcfg-th">类型</th>
                    <th class="dcfg-th">覆盖值</th><th class="dcfg-th">操作</th>
                </tr></thead>
                <tbody>${rows}</tbody></table>`;
            box.dataset.loaded = 'true';
        }

        async function dcfgAddOverride() {
            const context = dcfgCaptureContext();
            if (!dcfgContextIsCurrent(context) || !context.serial) return;
            const name = document.getElementById('dcfg-ovr-name').value.trim();
            const type = document.getElementById('dcfg-ovr-type').value;
            const value = dcfgOvrValueInput();
            if (!name) { showToast('请填写资源名', 'warning'); return; }
            const body = {
                device_id: context.storageId,
                target_package: 'android',
                resource_name: name,
                resource_type: type,
                value,
            };
            const r = await apiCall('/api/config-override/entries', 'POST', body);
            if (!dcfgContextIsCurrent(context)) return;
            if (!r.success) {
                showToast('添加失败：' + (r.error || ''), 'error');
                return;
            }
            showToast('已添加覆盖项（尚未应用，点「应用」生效）', 'success');
            document.getElementById('dcfg-ovr-name').value = '';
            document.getElementById('dcfg-ovr-value').value = '';
            document.getElementById('dcfg-ovr-value-arr').value = '';
            await loadDeviceOverrides(context);
        }

        async function dcfgRemoveOverride(name) {
            const context = dcfgCaptureContext();
            if (!dcfgContextIsCurrent(context) || !context.serial) return;
            const ok = await showConfirmDialog('删除覆盖项', `确定从列表删除 ${name}？删除后仍需点「应用」才在设备上生效。`);
            if (!ok || !dcfgContextIsCurrent(context)) return;
            const r = await dcfgOvrApi(`/api/config-override/entries?resource_name=${encodeURIComponent(name)}`, { method: 'DELETE' }, context);
            if (!dcfgContextIsCurrent(context)) return;
            if (!r.success) { showToast('删除失败：' + (r.error || ''), 'error'); return; }
            showToast(r.data.removed ? '已删除' : '该项不存在', r.data.removed ? 'success' : 'info');
            await loadDeviceOverrides(context);
        }

        function dcfgOvrGate() {
            if (!dcfgOvrStatus || !dcfgOvrStatus.reachable) { showToast('设备不可达', 'error'); return false; }
            if (!dcfgOvrStatus.is_userdebug) { showToast('需要 userdebug/eng 构建', 'error'); return false; }
            if (!dcfgOvrStatus.verity_disabled) { showToast('请先按状态提示关闭 dm-verity', 'warning'); return false; }
            if (Date.now() < dcfgOvrBusyUntil) { showToast('设备正在重启中，请稍候', 'warning'); return false; }
            return true;
        }

        function dcfgOvrSetBusy(seconds) {
            dcfgOvrBusyUntil = Date.now() + seconds * 1000;
        }

        // apply/revert 后设备会重启。等待 busy 窗口结束，轮询 status 直到设备可达，
        // 操作完成后刷新 overlay 状态。
        let dcfgOvrPollTimer = null;
        function dcfgOvrPollUntilReachable(context = dcfgCaptureContext(), timeoutMs = 180000) {
            if (dcfgOvrPollTimer) { clearTimeout(dcfgOvrPollTimer); dcfgOvrPollTimer = null; }
            const startedAt = Date.now();
            const tick = async () => {
                if (!dcfgContextIsCurrent(context)) {
                    dcfgOvrPollTimer = null;
                    return;
                }
                // 仍在 busy 窗口（重启中）→ 直接排下一轮
                if (Date.now() < dcfgOvrBusyUntil) {
                    dcfgOvrPollTimer = setTimeout(tick, 3000);
                    return;
                }
                const r = await dcfgOvrApi('/api/config-override/status', undefined, context).catch(e => ({success:false, error:e.message}));
                if (!dcfgContextIsCurrent(context)) return;
                if (r.success && r.data && r.data.reachable) {
                    dcfgOvrPollTimer = null;
                    await loadDeviceOverrides(context);      // 设备已恢复，刷新 status + entries + preview
                    showToast('设备已重启完成，状态已更新', 'success');
                    return;
                }
                if (Date.now() - startedAt > timeoutMs) {
                    dcfgOvrPollTimer = null;
                    showToast('设备重启超时，请手动点「刷新」', 'warning');
                    return;
                }
                dcfgOvrPollTimer = setTimeout(tick, 4000);  // 还未恢复连接，继续等
            };
            dcfgOvrPollTimer = setTimeout(tick, 3000);
        }

        async function dcfgApplyOverrides() {
            const context = dcfgCaptureContext();
            if (!dcfgContextIsCurrent(context) || !context.serial) return;
            if (!dcfgOvrGate()) return;
            const ok = await showConfirmDialog(
                '应用并重启设备',
                '将编译 RRO 覆盖包，推送到 /product/overlay 并重启设备（约 40 秒后恢复连接）。期间设备的其他操作会中断。继续？'
            );
            if (!ok || !dcfgContextIsCurrent(context)) return;
            if (!await dcfgRequireElevation('应用设备 RRO 配置覆盖')) return;
            if (!dcfgContextIsCurrent(context)) return;
            showToast('正在编译并推送 overlay…', 'info');
            const r = await dcfgOvrApi('/api/config-override/apply', { method: 'POST' }, context);
            if (!dcfgContextIsCurrent(context)) return;
            if (!r.success) { showToast('应用失败：' + (r.error || ''), 'error'); return; }
            dcfgOvrSetBusy(45);
            showToast('已推送并重启，设备恢复后状态将自动更新', 'success');
            dcfgOvrPollUntilReachable(context);
        }

        async function dcfgRevertAll() {
            const context = dcfgCaptureContext();
            if (!dcfgContextIsCurrent(context) || !context.serial) return;
            if (!dcfgOvrGate()) return;
            const ok = await showConfirmDialog(
                '撤销全部覆盖',
                '将删除设备上的 overlay 包并重启（host 覆盖列表保留，可重新应用）。继续？'
            );
            if (!ok || !dcfgContextIsCurrent(context)) return;
            if (!await dcfgRequireElevation('撤销设备 RRO 配置覆盖')) return;
            if (!dcfgContextIsCurrent(context)) return;
            const r = await dcfgOvrApi('/api/config-override/revert', { method: 'POST' }, context);
            if (!dcfgContextIsCurrent(context)) return;
            if (!r.success) { showToast('撤销失败：' + (r.error || ''), 'error'); return; }
            dcfgOvrSetBusy(45);
            showToast('已删除 overlay 并重启，设备恢复后状态将自动更新', 'success');
            dcfgOvrPollUntilReachable(context);
        }

        // 关闭/恢复 dm-verity。disable 是 apply 前的一次性步骤；两者都可能需要重启。
        async function dcfgToggleVerity(action) {
            const context = dcfgCaptureContext();
            if (!dcfgContextIsCurrent(context) || !context.serial) return;
            if (!dcfgOvrStatus || !dcfgOvrStatus.reachable) { showToast('设备不可达', 'error'); return; }
            if (!dcfgOvrStatus.is_userdebug) { showToast('需要 userdebug/eng 构建', 'error'); return; }
            if (Date.now() < dcfgOvrBusyUntil) { showToast('设备正在重启中，请稍候', 'warning'); return; }
            const isDisable = action === 'disable';
            // 已是目标状态则提示无需操作
            if (isDisable && dcfgOvrStatus.verity_disabled) { showToast('dm-verity 已是关闭状态', 'info'); return; }
            if (!isDisable && dcfgOvrStatus.verity_disabled === false) { showToast('dm-verity 已是启用状态', 'info'); return; }
            const title = isDisable ? '关闭 dm-verity' : '恢复 dm-verity';
            const desc = isDisable
                ? '关闭验证启动，使 /product 可写（apply 覆盖前的一次性步骤）。需要重启设备生效。继续？'
                : '恢复验证启动（dm-verity）。需要重启设备生效。继续？';
            const ok = await showConfirmDialog(title, desc);
            if (!ok || !dcfgContextIsCurrent(context)) return;
            if (!await dcfgRequireElevation(`${title}并重启设备`)) return;
            if (!dcfgContextIsCurrent(context)) return;
            const path = isDisable ? '/api/config-override/disable-verity' : '/api/config-override/enable-verity';
            const r = await dcfgOvrApi(path, { method: 'POST' }, context);
            if (!dcfgContextIsCurrent(context)) return;
            if (!r.success) { showToast((isDisable ? '关闭' : '恢复') + '失败：' + (r.error || ''), 'error'); return; }
            if (r.data && r.data.needs_reboot) {
                // 链式调用专用 reboot（同 adb 路径，device_id 一致）
                showToast(r.data.message + ' 正在重启…', 'info');
                await dcfgOvrApi('/api/config-override/reboot', { method: 'POST' }, context);
                if (!dcfgContextIsCurrent(context)) return;
                dcfgOvrSetBusy(45);
                showToast('设备重启中，设备恢复后状态将自动更新', 'success');
                dcfgOvrPollUntilReachable(context);
            } else {
                showToast(r.data.message, 'success');
                await loadDeviceOverrides(context);
            }
        }

        // ===== packages (pm list packages -f) =====
        let dcfgPkgRows = [];
        let dcfgPkgLoading = 0;
        const dcfgScheduleFilterPkgImpl = dcfgDebounce(() => dcfgFilterPkg());
        function dcfgScheduleFilterPkg() { dcfgScheduleFilterPkgImpl(); }
        async function loadDevicePackagesF(context = dcfgCaptureContext()) {
            if (!dcfgContextIsCurrent(context)) return;
            if (dcfgPkgLoading === context.generation) return;
            dcfgPkgLoading = context.generation;
            dcfgSetButtonBusy('dcfg-pkg-refresh', true, '↻ 刷新');
            const box = document.getElementById('dcfg-pkg-results');
            const hadRenderedRows = box.dataset.loaded === 'true';
            box.setAttribute('aria-busy', 'true');
            if (!hadRenderedRows) {
                box.classList.add('dcfg-empty');
                box.innerHTML = '加载中…';
            }
            try {
                const j = context.remote
                    ? await dcfgInspect('packages_with_path', {}, context)
                    : await fetch('/api/config-explorer/packages-with-path?device_id=' + encodeURIComponent(context.serial)).then(r => r.json());
                if (!dcfgContextIsCurrent(context)) return;
                if (!j.success) {
                    if (hadRenderedRows) showToast('应用列表刷新失败: ' + (j.error || '加载失败'), 'error');
                    else { box.classList.add('dcfg-empty'); box.innerHTML = '❌ ' + dcfgEscapeHtml(j.error || '加载失败'); }
                    return;
                }
                dcfgPkgRows = (context.remote ? j.rows : j.data?.rows) || [];
                document.getElementById('dcfg-pkg-stat').textContent = `共 ${dcfgPkgRows.length} 个`;
                dcfgFilterPkg();
            } catch (e) {
                if (dcfgContextIsCurrent(context)) {
                    if (hadRenderedRows) showToast('应用列表刷新失败: ' + e.message, 'error');
                    else { box.classList.add('dcfg-empty'); box.innerHTML = '❌ ' + dcfgEscapeHtml(e.message); }
                }
            } finally {
                if (dcfgPkgLoading === context.generation) {
                    dcfgPkgLoading = 0;
                    if (dcfgContextIsCurrent(context)) {
                        dcfgSetButtonBusy('dcfg-pkg-refresh', false, '↻ 刷新');
                        box.setAttribute('aria-busy', 'false');
                    }
                }
            }
        }
        function dcfgFilterPkg() {
            const q = document.getElementById('dcfg-pkg-filter').value.trim().toLowerCase();
            const box = document.getElementById('dcfg-pkg-results');
            const rows = q ? dcfgPkgRows.filter(r => (r.package + ' ' + r.path).toLowerCase().includes(q)) : dcfgPkgRows;
            if (!rows.length) { box.classList.add('dcfg-empty'); box.innerHTML = '无匹配'; box.dataset.loaded = 'true'; return; }
            dcfgSetTable(
                box,
                '<colgroup><col style="width:30%"><col style="width:58%"><col style="width:12%"></colgroup>',
                '<th class="dcfg-th">包名</th><th class="dcfg-th">APK 路径</th><th class="dcfg-th">操作</th>',
                rows,
                r => {
                    const isApk = /\.(apk|jar)$/i.test(r.path);
                    const actBtn = isApk
                        ? `<button class="btn-xxs btn-primary" data-click="dcfgDecompileApk" data-a0="${dcfgEscapeHtml(r.path)}" data-a1="${dcfgEscapeHtml(r.package)}">🔍 反编译</button>`
                        : '<span class="dcfg-muted dcfg-hint">非 APK</span>';
                    return `<tr>
                    <td><div class="dcell" title="${dcfgEscapeHtml(r.package)}">${dcfgEscapeHtml(r.package)}</div></td>
                    <td><div class="dcell" title="${dcfgEscapeHtml(r.path)}">${dcfgEscapeHtml(r.path)}</div></td>
                    <td>${actBtn}</td>
                </tr>`;
                },
            );
        }

        // 拉取设备 APK 并送入 APK 分析（反编译）流程
        async function dcfgDecompileApk(path, pkgName) {
            const context = dcfgCaptureContext();
            if (!dcfgContextIsCurrent(context) || !context.serial || !path) return;
            const btn = event && event.target ? event.target : null;
            if (btn) { btn.disabled = true; btn.textContent = '准备中…'; }
            showToast(`正在拉取 ${pkgName || path} 进行反编译…`, 'info');
            try {
                let j;
                if (context.remote) {
                    const params = new URLSearchParams({
                        worker_id: context.workerId,
                        device_id: context.serial,
                        path
                    });
                    const started = await apiCall(`/api/cluster/devices/export?${params}`, 'POST');
                    if (!dcfgContextIsCurrent(context)) return;
                    const transferId = started.transfer?.id;
                    if (!transferId || !started.command_id) throw new Error('Worker 未返回文件传输任务');
                    window.GmsWorkspace?.update({artifact_id: transferId}, {source: 'device-apk-export'});
                    const deadline = Date.now() + 6 * 60 * 1000;
                    while (Date.now() < deadline) {
                        await new Promise(resolve => setTimeout(resolve, 1000));
                        if (!dcfgContextIsCurrent(context)) return;
                        const status = await apiCall(`/api/cluster/commands/${encodeURIComponent(started.command_id)}`);
                        const command = status.command || {};
                        if (command.status === 'completed') break;
                        if (['failed', 'cancelled'].includes(command.status)) {
                            throw new Error(command.error || 'Worker 拉取 APK 失败');
                        }
                        if (Date.now() >= deadline) throw new Error('Worker 拉取 APK 超时');
                    }
                    j = await apiCall(
                        `/api/cluster/transfers/${encodeURIComponent(transferId)}/apk-analysis`,
                        'POST'
                    );
                } else {
                    j = await fetch('/api/config-explorer/decompile', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ device_id: context.serial, path: path })
                    }).then(r => r.json());
                }
                if (!dcfgContextIsCurrent(context)) return;
                if (!j.success) { showToast('反编译失败: ' + (j.error || j.message || '未知错误'), 'error'); if (btn) { btn.disabled = false; btn.textContent = '🔍 反编译'; } return; }
                const task = j.data || {};
                if (!task.task_id) { showToast('创建反编译任务失败', 'error'); if (btn) { btn.disabled = false; btn.textContent = '🔍 反编译'; } return; }
                // 跳转 APK 分析页并启动反编译。
                closeDeviceConfigExplorer();
                switchPage('apk-analysis', null);
                if (typeof initApkAnalysisPage === 'function') initApkAnalysisPage();
                if (typeof stopApkPolling === 'function') stopApkPolling();
                window.apkNotifiedTaskId = null;
                window.apkCurrentTaskId = task.task_id;
                window.GmsWorkspace?.update({
                    worker_id: context.workerId,
                    device_ids: [context.storageId],
                    artifact_id: task.transfer_id || '',
                    origin_page: 'apk-analysis'
                }, {source: 'device-apk-analysis'});
                if (typeof setApkUploadEmpty === 'function') setApkUploadEmpty(false);
                const fileSizeMB = task.size ? (task.size / (1024 * 1024)).toFixed(1) : '-';
                const el = (id) => document.getElementById(id);
                if (el('apk-analysis-status')) el('apk-analysis-status').style.display = 'block';
                if (el('apk-file-name')) el('apk-file-name').textContent = `${task.filename || path} (${fileSizeMB}MB)`;
                if (el('apk-analysis-state')) el('apk-analysis-state').textContent = '已从设备导入，正在启动反编译';
                if (el('apk-btn-download')) el('apk-btn-download').style.display = 'none';
                if (el('apk-analysis-result')) el('apk-analysis-result').style.display = 'none';
                if (el('apk-analysis-progress-container')) el('apk-analysis-progress-container').style.display = 'none';
                if (el('apk-analysis-progress-bar')) el('apk-analysis-progress-bar').style.width = '0%';
                const tree = el('apk-source-tree');
                if (tree) { tree.dataset.loaded = ''; tree.innerHTML = ''; }
                const permList = el('apk-permissions-list');
                if (permList) { permList.dataset.loaded = ''; permList.innerHTML = ''; }
                const manifestInfo = el('apk-manifest-info');
                if (manifestInfo) manifestInfo.innerHTML = '';
                const rawXml = el('apk-raw-xml');
                if (rawXml) rawXml.textContent = '';
                if (typeof closeApkFileViewer === 'function') closeApkFileViewer();
                if (typeof switchApkTab === 'function') switchApkTab('manifest');
                if (typeof startApkAnalysis === 'function') await startApkAnalysis();
            } catch (e) {
                if (!dcfgContextIsCurrent(context)) return;
                showToast('反编译请求失败: ' + (e && e.message ? e.message : e), 'error');
                if (btn) { btn.disabled = false; btn.textContent = '🔍 反编译'; }
            }
        }

        // ===== features (pm list features) =====
        let dcfgFeatRows = [];
        let dcfgFeatLoading = 0;
        const dcfgScheduleFilterFeatImpl = dcfgDebounce(() => dcfgFilterFeat());
        function dcfgScheduleFilterFeat() { dcfgScheduleFilterFeatImpl(); }
        async function loadDeviceFeatures(context = dcfgCaptureContext()) {
            if (!dcfgContextIsCurrent(context)) return;
            if (dcfgFeatLoading === context.generation) return;
            dcfgFeatLoading = context.generation;
            dcfgSetButtonBusy('dcfg-feat-refresh', true, '↻ 刷新');
            const box = document.getElementById('dcfg-feat-results');
            const hadRenderedRows = box.dataset.loaded === 'true';
            box.setAttribute('aria-busy', 'true');
            if (!hadRenderedRows) {
                box.classList.add('dcfg-empty');
                box.innerHTML = '加载中…';
            }
            try {
                const j = context.remote
                    ? await dcfgInspect('features', {}, context)
                    : await fetch('/api/config-explorer/features?device_id=' + encodeURIComponent(context.serial)).then(r => r.json());
                if (!dcfgContextIsCurrent(context)) return;
                if (!j.success) {
                    if (hadRenderedRows) showToast('特性列表刷新失败: ' + (j.error || '加载失败'), 'error');
                    else { box.classList.add('dcfg-empty'); box.innerHTML = '❌ ' + dcfgEscapeHtml(j.error || '加载失败'); }
                    return;
                }
                dcfgFeatRows = (context.remote ? j.rows : j.data?.rows) || [];
                document.getElementById('dcfg-feat-stat').textContent = `共 ${dcfgFeatRows.length} 个`;
                dcfgFilterFeat();
            } catch (e) {
                if (dcfgContextIsCurrent(context)) {
                    if (hadRenderedRows) showToast('特性列表刷新失败: ' + e.message, 'error');
                    else { box.classList.add('dcfg-empty'); box.innerHTML = '❌ ' + dcfgEscapeHtml(e.message); }
                }
            } finally {
                if (dcfgFeatLoading === context.generation) {
                    dcfgFeatLoading = 0;
                    if (dcfgContextIsCurrent(context)) {
                        dcfgSetButtonBusy('dcfg-feat-refresh', false, '↻ 刷新');
                        box.setAttribute('aria-busy', 'false');
                    }
                }
            }
        }
        function dcfgFilterFeat() {
            const q = document.getElementById('dcfg-feat-filter').value.trim().toLowerCase();
            const box = document.getElementById('dcfg-feat-results');
            const rows = q ? dcfgFeatRows.filter(r => r.name.toLowerCase().includes(q)) : dcfgFeatRows;
            if (!rows.length) { box.classList.add('dcfg-empty'); box.innerHTML = '无匹配'; box.dataset.loaded = 'true'; return; }
            dcfgSetTable(
                box,
                '<colgroup><col style="width:70%"><col style="width:30%"></colgroup>',
                '<th class="dcfg-th">特性名</th><th class="dcfg-th">版本</th>',
                rows,
                r => `<tr>
                    <td><div class="dcell" title="${dcfgEscapeHtml(r.name)}">${dcfgEscapeHtml(r.name)}</div></td>
                    <td><div class="dcell" title="${dcfgEscapeHtml(r.version)}">${r.version ? dcfgEscapeHtml(r.version) : '<span class="dcfg-muted">—</span>'}</div></td>
                </tr>`,
            );
        }

        // ===== props (getprop) =====
        let dcfgPropRows = [];
        let dcfgPropLoading = 0;
        const dcfgScheduleFilterPropImpl = dcfgDebounce(() => dcfgFilterProp());
        function dcfgScheduleFilterProp() { dcfgScheduleFilterPropImpl(); }
        async function loadDeviceProps(context = dcfgCaptureContext()) {
            if (!dcfgContextIsCurrent(context)) return;
            if (dcfgPropLoading === context.generation) return;
            dcfgPropLoading = context.generation;
            dcfgSetButtonBusy('dcfg-prop-refresh', true, '↻ 刷新');
            const box = document.getElementById('dcfg-prop-results');
            const hadRenderedRows = box.dataset.loaded === 'true';
            box.setAttribute('aria-busy', 'true');
            if (!hadRenderedRows) {
                box.classList.add('dcfg-empty');
                box.innerHTML = '加载中…';
            }
            try {
                const j = context.remote
                    ? await dcfgInspect('props', {}, context)
                    : await fetch('/api/config-explorer/props?device_id=' + encodeURIComponent(context.serial)).then(r => r.json());
                if (!dcfgContextIsCurrent(context)) return;
                if (!j.success) {
                    if (hadRenderedRows) showToast('属性列表刷新失败: ' + (j.error || '加载失败'), 'error');
                    else { box.classList.add('dcfg-empty'); box.innerHTML = '❌ ' + dcfgEscapeHtml(j.error || '加载失败'); }
                    return;
                }
                dcfgPropRows = (context.remote ? j.rows : j.data?.rows) || [];
                document.getElementById('dcfg-prop-stat').textContent = `共 ${dcfgPropRows.length} 项`;
                dcfgFilterProp();
            } catch (e) {
                if (dcfgContextIsCurrent(context)) {
                    if (hadRenderedRows) showToast('属性列表刷新失败: ' + e.message, 'error');
                    else { box.classList.add('dcfg-empty'); box.innerHTML = '❌ ' + dcfgEscapeHtml(e.message); }
                }
            } finally {
                if (dcfgPropLoading === context.generation) {
                    dcfgPropLoading = 0;
                    if (dcfgContextIsCurrent(context)) {
                        dcfgSetButtonBusy('dcfg-prop-refresh', false, '↻ 刷新');
                        box.setAttribute('aria-busy', 'false');
                    }
                }
            }
        }
        function dcfgFilterProp() {
            const q = document.getElementById('dcfg-prop-filter').value.trim().toLowerCase();
            const box = document.getElementById('dcfg-prop-results');
            const rows = q ? dcfgPropRows.filter(r => (r.name + ' ' + r.value).toLowerCase().includes(q)) : dcfgPropRows;
            if (!rows.length) { box.classList.add('dcfg-empty'); box.innerHTML = '无匹配'; box.dataset.loaded = 'true'; return; }
            dcfgSetTable(
                box,
                '<colgroup><col style="width:40%"><col style="width:60%"></colgroup>',
                '<th class="dcfg-th">属性名</th><th class="dcfg-th">值</th>',
                rows,
                r => `<tr>
                    <td><div class="dcell" title="${dcfgEscapeHtml(r.name)}">${dcfgEscapeHtml(r.name)}</div></td>
                    <td><div class="dcell" title="${dcfgEscapeHtml(r.value)}">${r.value ? dcfgEscapeHtml(r.value) : '<span class="dcfg-muted">—</span>'}</div></td>
                </tr>`,
            );
        }

        async function loadDeviceConfigPackages(context = dcfgCaptureContext()) {
            if (!dcfgContextIsCurrent(context)) return;
            const dl = document.getElementById('dcfg-package-list');
            try {
                const j = context.remote
                    ? await dcfgInspect('packages_all', {}, context)
                    : await fetch('/api/config-explorer/packages/all?device_id=' + encodeURIComponent(context.serial)).then(r => r.json());
                if (!dcfgContextIsCurrent(context)) return;
                const pkgs = (context.remote ? j.packages : j.data?.packages) || [];
                // 常带 config 的包置顶，方便快速选择
                const pinned = ['android', 'com.android.systemui', 'com.android.providers.settings', 'com.android.settings', 'com.android.phone', 'com.android.wifi'];
                const rest = pkgs.filter(p => !pinned.includes(p));
                const ordered = [...pinned.filter(p => pkgs.includes(p)), ...rest];
                dl.innerHTML = ordered.map(p => `<option value="${dcfgEscapeHtml(p)}">`).join('');
            } catch (e) {
                if (!dcfgContextIsCurrent(context)) return;
                console.error('加载包列表失败', e);
                showToast(`加载设备包列表失败: ${e.message}`, 'error');
            }
        }

        function dcfgEscapeHtml(s) {
            if (s === null || s === undefined) return '';
            return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
        }

        async function runDeviceConfigQuery(context = dcfgCaptureContext()) {
            if (!dcfgContextIsCurrent(context) || !context.serial) return;
            const queryId = ++dcfgQueryRequestId;
            const pkg = document.getElementById('dcfg-package').value || 'android';
            const name = document.getElementById('dcfg-name').value.trim();
            const type = document.getElementById('dcfg-type').value;
            const withEffective = document.getElementById('dcfg-effective').checked;

            const params = new URLSearchParams({
                package: pkg,
                device_id: context.serial,
                config_only: 'true',
                with_effective: withEffective ? 'true' : 'false'
            });
            if (name) params.set('name', name);
            if (type) params.set('type', type);

            const stat = document.getElementById('dcfg-stat');
            const results = document.getElementById('dcfg-results');
            const hadRenderedResults = results.dataset.loaded === 'true';
            results.setAttribute('aria-busy', 'true');
            if (!hadRenderedResults) {
                stat.classList.remove('active');
                results.classList.add('dcfg-empty');
                results.innerHTML = withEffective ? '正在查询 overlay 生效值（逐个 adb 调用，请稍候）…' : '查询中…';
            }
            dcfgSetButtonBusy('dcfg-query', true, '查询', '查询中…');

            try {
                const j = context.remote
                    ? await dcfgInspect('config_explore', {
                        package: pkg,
                        name_filter: name,
                        type_filter: type,
                        with_effective: withEffective,
                        effective_limit: 0
                    }, context)
                    : await fetch('/api/config-explorer?' + params).then(r => r.json());
                if (!dcfgContextIsCurrent(context) || queryId !== dcfgQueryRequestId) return;
                if (!j.success) {
                    if (hadRenderedResults) {
                        showToast('配置查询失败: ' + (j.error || j.message || '查询失败'), 'error');
                    } else {
                        results.classList.add('dcfg-empty');
                        results.innerHTML = '❌ ' + dcfgEscapeHtml(j.error || j.message || '查询失败');
                    }
                    return;
                }
                renderDeviceConfigResults(context.remote ? j : j.data, withEffective);
            } catch (e) {
                if (dcfgContextIsCurrent(context) && queryId === dcfgQueryRequestId) {
                    if (hadRenderedResults) showToast('配置查询失败: ' + e.message, 'error');
                    else { results.classList.add('dcfg-empty'); results.innerHTML = '❌ 请求失败: ' + dcfgEscapeHtml(e.message); }
                }
            } finally {
                if (dcfgContextIsCurrent(context) && queryId === dcfgQueryRequestId) {
                    dcfgSetButtonBusy('dcfg-query', false, '查询', '查询中…');
                    results.setAttribute('aria-busy', 'false');
                }
            }
        }

        function renderDeviceConfigResults(data, withEffective) {
            const res = data.resources || [];
            const stat = document.getElementById('dcfg-stat');
            const results = document.getElementById('dcfg-results');
            if (!res.length) {
                results.classList.add('dcfg-empty');
                results.innerHTML = '没有匹配的资源';
                results.dataset.loaded = 'true';
                stat.classList.remove('active');
                return;
            }
            let txt = `共 ${data.total} 个资源`;
            if (withEffective) {
                txt += ` · 模式: <b>默认值+overlay生效值</b> · 被 overlay 修改: <b class="dcfg-changed">${data.overlayed_count}</b>`;
            } else {
                txt += ` · 模式: <b>仅默认值</b>（勾选「对比 overlay 生效值」可显示生效值/状态列）`;
            }
            stat.innerHTML = txt;
            stat.classList.add('active');

            // table-layout:fixed + 固定列宽，确保生效值/状态列始终可见，长内容自动截断
            // .dcell 容器实现单行截断，title 属性让鼠标悬停显示完整内容
            const colWidths = withEffective
                ? '<colgroup><col style="width:24%"><col style="width:7%"><col style="width:18%"><col style="width:20%"><col style="width:9%"><col style="width:14%"><col style="width:8%"></colgroup>'
                : '<colgroup><col style="width:42%"><col style="width:10%"><col style="width:40%"><col style="width:8%"></colgroup>';
            const effCol = withEffective ? '<th class="dcfg-th">生效值(overlay)</th><th class="dcfg-th">状态</th><th class="dcfg-th">overlay来源</th>' : '';
            const renderRow = e => {
                const changed = e.overlay_changed === true;
                // 未被 overlay 覆盖时，生效值列留空（lookup 返回值与默认值相同，无意义）
                const effDisplay = changed
                    ? dcfgEscapeHtml(e.effective_value)
                    : '<span class="dcfg-muted">—</span>';
                const effTitle = changed ? dcfgEscapeHtml(e.effective_value) : '';
                // overlay 来源：只有被修改时才显示来源包名，否则留空
                const srcDisplay = changed && e.overlay_source
                    ? dcfgEscapeHtml(e.overlay_source)
                    : '<span class="dcfg-muted">—</span>';
                const status = e.lookup_error
                    ? `<span title="${dcfgEscapeHtml(e.lookup_error)}" class="dcfg-danger">错误</span>`
                    : (changed ? '<span class="dcfg-changed dcfg-status">已修改</span>'
                               : (e.overlay_changed === false ? '<span class="dcfg-muted">默认</span>' : ''));
                const effCell = withEffective
                    ? `<td class="${changed ? 'dcfg-changed' : ''}"><div class="dcell" title="${effTitle}">${effDisplay}</div></td><td>${status}</td><td><div class="dcell" title="${changed && e.overlay_source ? dcfgEscapeHtml(e.overlay_source) : ''}">${srcDisplay}</div></td>`
                    : '';
                const bareName = e.name.replace(/^[a-z-]+\//, '');
                const isAndroidPkg = (data.package || 'android') === 'android';
                const actCell = isAndroidPkg
                    ? `<td><button class="btn-xxs" title="用此 framework 资源预填覆盖表单" data-click="dcfgPrefillOverride" data-a0="${dcfgEscapeHtml(bareName)}" data-a1="${dcfgEscapeHtml(e.type)}">⚡覆盖</button></td>`
                    : '<td><span class="dcfg-muted dcfg-hint" title="当前 override 功能仅支持 android/framework-res">—</span></td>';
                return `<tr>
                    <td><div class="dcell" title="${dcfgEscapeHtml(e.name)}">${dcfgEscapeHtml(e.name)}</div></td>
                    <td><span class="dcfg-type-badge">${dcfgEscapeHtml(e.type)}</span></td>
                    <td><div class="dcell" title="${dcfgEscapeHtml(e.default_value)}">${dcfgEscapeHtml(e.default_value)}</div></td>
                    ${effCell}
                    ${actCell}
                </tr>`;
            };

            dcfgSetTable(
                results,
                colWidths,
                `<th class="dcfg-th">资源名</th>
                    <th class="dcfg-th">类型</th>
                    <th class="dcfg-th">默认值(APK)</th>
                    ${effCol}
                    <th class="dcfg-th">操作</th>`,
                res,
                renderRow,
            );
            // 把查询到的资源名灌进 override 表单的下拉，方便快速选择
            const dl = document.getElementById('dcfg-ovr-name-list');
            if (dl) {
                dl.innerHTML = res.slice(0, 500).map(e => {
                    const bare = e.name.replace(/^[a-z-]+\//, '');
                    return `<option value="${dcfgEscapeHtml(bare)}">${dcfgEscapeHtml(e.type)}</option>`;
                }).join('');
            }
        }

        // 从 configs 查询行预填 override 表单并切到 override tab
        function dcfgPrefillOverride(name, type) {
            document.getElementById('dcfg-ovr-name').value = name;
            const sel = document.getElementById('dcfg-ovr-type');
            for (const opt of sel.options) { if (opt.value === type) { sel.value = type; break; } }
            dcfgOvrTypeChanged();
            dcfgSwitchTab('overrides');
            showToast(`已预填 ${name}，填写覆盖值后点「添加覆盖」`, 'info');
        }

        // ==================== 页面切换功能 ====================
        let currentPage = window.__targetPage || 'test';
        let terminalInitialized = false;
        let terminal = null;
        let terminalSocket = null;
        let isTerminalConnected = false;
        let terminalDomEventController = null;
        // 页面标题映射
        const PAGE_TITLES = {
            'test': '测试界面 - GMS远程测试',
            'desktop': '主机桌面 - GMS远程测试',
            'terminal': '主机终端 - GMS远程测试',
            'users': '用户管理 - GMS远程测试',
            'devices': '设备管理 - GMS远程测试',
            'devices-console': '设备串口 - GMS 远程测试',
            'reports': '报告管理 - GMS远程测试',
            'report-analysis': '报告分析 - GMS远程测试',
            'apk-analysis': 'APK分析 - GMS远程测试',
            'test-suites': '测试套件 - GMS远程测试',
            'api-docs': '系统接口 - GMS远程测试',
            'architecture': '系统架构 - GMS远程测试',
            'websites': '常用网址 - GMS远程测试',
            'tools': '常用工具 - GMS远程测试',
            'security-audit': '安全审计 - GMS 远程测试',
            'gms-assistant': 'GMS助手 - GMS 远程测试',
            'automation': 'GMS ATS - GMS 远程测试',
            'cluster': '主机集群 - GMS 远程测试',
            'redmine-agent': 'Redmine - GMS 远程测试',
            'gerrit-dashboard': 'Gerrit看板 - GMS 远程测试',
            'notes': '个人知识库 - GMS 远程测试',
            'agent': '对话Agent - GMS 远程测试'
        };

        const SIDEBAR_VISIBLE_STORAGE_KEY = 'gms_sidebar_visible_pages';
        const SIDEBAR_PAGE_DESCRIPTIONS = {
            test: '选择 CTS/GTS/VTS/STS 等套件，指定模块/用例和设备，启动/停止测试，查看实时日志和执行状态。',
            desktop: '启动或查看 noVNC/x11vnc 桌面，用于远程 GUI 操作、调试工具和桌面环境确认。',
            terminal: '打开服务器 SSH 终端，执行命令、上传文件、辅助定位环境问题。',
            users: '查看在线用户、客户端 IP、用户名、测试运行状态和设备占用情况。',
            devices: '查看 ADB 设备、型号、Android 版本、电量、来源、锁定状态；支持重启、remount、WiFi、投屏、bootloader 操作。',
            'devices-console': '枚举 Controller 本机 USB 转串口，绑定设备备注，查看实时控制台并管理开机早期日志。',
            reports: '列出历史测试报告，按用户/时间查看，下载、删除、进入分析。',
            'report-analysis': '上传报告或打开已有报告，解析失败项，做失败诊断、AI 分析和根因线索整理。',
            'apk-analysis': '上传 APK/JAR，反编译源码，查看 Manifest、权限、源码树和文件内容。',
            'test-suites': '浏览本地/远端套件目录，查看 tradefed 结果，下载/解压套件，触发套件内 APK/JAR 分析。',
            'api-docs': '查看 API 清单、参数、curl 示例和响应格式，适合调试接口或脚本调用。',
            architecture: '查看平台模块、数据流和核心组件关系。',
            websites: '按分类维护常用站点、图标和链接。',
            tools: '维护可下载工具条目，从服务器白名单工具目录下载脚本或二进制工具。',
            'security-audit': '查看页面访问、API 调用、请求摘要、响应摘要和耗时，辅助追踪操作记录。',
            'gms-assistant': '打开外部/内置 GMS 知识助手入口。',
            automation: '把 Gerrit 触发、构建产物、设备选择、烧写、测试执行和结果回写串成自动化测试站流程。',
            cluster: '查看 Controller/Worker、设备、套件、运行任务和部署状态，管理持久化 Cluster Job。',
            'redmine-agent': '查看个人/部门/项目 Redmine 统计、待回复、超阈值未回复、解决趋势和问题明细。',
            'gerrit-dashboard': '查看个人/部门 Gerrit 提交统计、查询变更、趋势明细和成员配置。',
            agent: '用自然语言查询设备/报告/套件，生成测试计划，打开项目页面，执行确认类操作并跟踪分析流程。',
            notes: '按知识空间和目录维护 Wiki，上传附件、全文检索、关联报告/Redmine/Gerrit 并进行知识问答。'
        };

        function getSidebarNav() {
            return document.getElementById('sidebar-nav');
        }

        function getSidebarItems() {
            const nav = getSidebarNav();
            return nav ? Array.from(nav.querySelectorAll('.sidebar-item')) : [];
        }

        function getAllSidebarPages() {
            return getSidebarItems().map(item => item.dataset.page).filter(Boolean);
        }

        function normalizeSidebarVisiblePages(pages) {
            const allPages = getAllSidebarPages();
            if (!Array.isArray(pages) || pages.length === 0) {
                return allPages;
            }
            const valid = [];
            const seen = new Set();
            pages.forEach(page => {
                if (allPages.includes(page) && !seen.has(page)) {
                    valid.push(page);
                    seen.add(page);
                }
            });
            for (const requiredPage of ['notes', 'cluster']) {
                if (allPages.includes(requiredPage) && !seen.has(requiredPage)) valid.push(requiredPage);
            }
            return valid.length > 0 ? valid : allPages;
        }

        function getSavedSidebarVisiblePages() {
            if (Array.isArray(window.__savedSidebarVisiblePages)) {
                return normalizeSidebarVisiblePages(window.__savedSidebarVisiblePages);
            }
            try {
                return normalizeSidebarVisiblePages(JSON.parse(localStorage.getItem(SIDEBAR_VISIBLE_STORAGE_KEY) || 'null'));
            } catch (e) {
                console.error('[Sidebar] Failed to parse visible pages:', e);
                return normalizeSidebarVisiblePages(null);
            }
        }

        function getCurrentVisibleSidebarPages() {
            return getSidebarItems()
                .filter(item => !item.hidden && item.style.display !== 'none')
                .map(item => item.dataset.page)
                .filter(Boolean);
        }

        function resolveVisiblePage(pageName) {
            const visiblePages = getCurrentVisibleSidebarPages();
            if (visiblePages.includes(pageName)) {
                return pageName;
            }
            if (visiblePages.includes('test')) {
                return 'test';
            }
            return visiblePages[0] || getAllSidebarPages()[0] || 'test';
        }

        function readCurrentPageCookie() {
            const match = document.cookie.match(/(?:^|;\s*)gms_current_page=([^;]+)/);
            return match ? decodeURIComponent(match[1]) : '';
        }

        function saveCurrentPageState(pageName, { updateHash = true } = {}) {
            localStorage.setItem('gms_current_page', pageName);
            document.cookie = 'gms_current_page=' + encodeURIComponent(pageName) + '; path=/; max-age=31536000; SameSite=Lax';
            if (!updateHash || !window.history || !window.location) {
                return;
            }
            const currentHash = window.location.hash.substring(1);
            if (currentHash.split('?')[0] === pageName) {
                return;
            }
            window.history.replaceState(null, '', '#' + encodeURIComponent(pageName));
        }

        function applySidebarVisibility(pages) {
            const visiblePages = normalizeSidebarVisiblePages(pages);
            const visibleSet = new Set(visiblePages);
            getSidebarItems().forEach(item => {
                item.style.display = visibleSet.has(item.dataset.page) ? '' : 'none';
            });
            return visiblePages;
        }

        function getSidebarItemLabel(item) {
            const icon = item.querySelector('.sidebar-icon')?.innerHTML || '';
            const text = item.querySelector('.sidebar-text')?.textContent?.trim() || item.dataset.page;
            return { icon, text };
        }

        function handleSidebarBrandKeydown(event) {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                openSidebarVisibilityModal();
            }
        }

        function switchSidebarSettingsTab(tabName) {
            const target = tabName === 'guide' ? 'guide' : 'visibility';
            document.querySelectorAll('[data-sidebar-settings-tab]').forEach(button => {
                const active = button.dataset.sidebarSettingsTab === target;
                button.classList.toggle('active', active);
                button.setAttribute('aria-selected', active ? 'true' : 'false');
            });
            document.querySelectorAll('[data-sidebar-settings-panel]').forEach(panel => {
                panel.hidden = panel.dataset.sidebarSettingsPanel !== target;
            });
        }

        function setGuideImageZoom(actualSize) {
            const image = document.getElementById('guide-image-preview');
            const button = document.getElementById('guide-image-zoom-btn');
            const viewport = document.getElementById('guide-image-viewport');
            if (!image || !button || !viewport) return;
            image.classList.toggle('actual-size', actualSize);
            viewport.classList.toggle('actual-size', actualSize);
            button.textContent = actualSize ? '适合窗口' : '1:1 原图';
            viewport.scrollTop = 0;
            viewport.scrollLeft = 0;
        }

        function openGuideImageLightbox(trigger) {
            const sourceImage = trigger?.querySelector('img');
            if (!sourceImage) return;
            const preview = document.getElementById('guide-image-preview');
            const title = document.getElementById('guide-image-title');
            const caption = document.getElementById('guide-image-caption');
            if (!preview || !title || !caption) return;
            preview.src = sourceImage.currentSrc || sourceImage.src;
            preview.alt = sourceImage.alt || '';
            title.textContent = trigger.dataset.guideTitle || sourceImage.alt || '图片操作实例';
            caption.textContent = trigger.dataset.guideCaption || '';
            setGuideImageZoom(false);
            ModalManager.open('guide-image-modal');
        }

        function toggleGuideImageZoom() {
            const image = document.getElementById('guide-image-preview');
            if (!image) return;
            setGuideImageZoom(!image.classList.contains('actual-size'));
        }

        function closeGuideImageLightbox() {
            ModalManager.close('guide-image-modal');
        }

        function closeGuideImageLightboxFromBackdrop(event) {
            if (event.target?.id === 'guide-image-modal') closeGuideImageLightbox();
        }

        function openSidebarVisibilityModal() {
            const modal = document.getElementById('sidebar-visibility-modal');
            const list = document.getElementById('sidebar-visibility-list');
            if (!modal || !list) return;

            const guideUrl = document.getElementById('project-guide-url');
            if (guideUrl) {
                const currentOrigin = `${window.location.origin}/`;
                guideUrl.href = currentOrigin;
                guideUrl.textContent = `${currentOrigin} ↗`;
            }

            const visibleSet = new Set(getCurrentVisibleSidebarPages());
            list.innerHTML = getSidebarItems().map(item => {
                const { icon, text } = getSidebarItemLabel(item);
                const page = item.dataset.page;
                const checked = visibleSet.has(page) ? 'checked' : '';
                const description = SIDEBAR_PAGE_DESCRIPTIONS[page] || '显示或隐藏此功能页面。';
                return `<label class="sidebar-visibility-option">
                    <input type="checkbox" value="${page}" ${checked}>
                    <span class="sidebar-icon">${icon}</span>
                    <span class="sidebar-text">${text}</span>
                    <span class="sidebar-description" title="${description}">${description}</span>
                </label>`;
            }).join('');

            switchSidebarSettingsTab('visibility');

            ModalManager.open('sidebar-visibility-modal');
        }

        function closeSidebarVisibilityModal() {
            ModalManager.close('sidebar-visibility-modal');
        }

        function selectAllSidebarVisibility() {
            document.querySelectorAll('#sidebar-visibility-list input[type="checkbox"]').forEach(input => {
                input.checked = true;
            });
        }

        function saveSidebarVisibilityFromModal() {
            const checkedPages = Array.from(document.querySelectorAll('#sidebar-visibility-list input[type="checkbox"]:checked'))
                .map(input => input.value)
                .filter(Boolean);
            if (checkedPages.length === 0) {
                if (typeof showToast === 'function') {
                    showToast('至少保留一个导航页面', 'warning');
                } else {
                    console.warn('至少保留一个导航页面');
                }
                return;
            }

            const visiblePages = applySidebarVisibility(checkedPages);
            localStorage.setItem(SIDEBAR_VISIBLE_STORAGE_KEY, JSON.stringify(visiblePages));
            window.__savedSidebarVisiblePages = visiblePages;
            saveSidebarVisibilityToBackend(visiblePages);
            closeSidebarVisibilityModal();

            const nextPage = resolveVisiblePage(currentPage);
            if (nextPage !== currentPage) {
                switchPage(nextPage);
            }
        }

        async function saveSidebarVisibilityToBackend(visiblePages) {
            const nav = getSidebarNav();
            const order = nav ? getSidebarPages(nav) : getAllSidebarPages();
            try {
                const response = await fetch('/api/sidebar-order', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ order, visible_pages: visiblePages })
                });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const data = await response.json();
                if (data.success === false) {
                    throw new Error(data.error || 'Save failed');
                }
            } catch (e) {
                console.error('[Sidebar] Failed to save visibility:', e);
            }
        }

        async function loadSidebarConfigFromBackend() {
            try {
                const response = await fetch('/api/sidebar-order');
                if (!response.ok) return;
                const data = await response.json();
                if (data.success === false || !data.data) return;

                const nav = getSidebarNav();
                if (nav && Array.isArray(data.data.order) && data.data.order.length > 0) {
                    applySidebarOrder(nav, data.data.order);
                    localStorage.setItem('gms_sidebar_order', JSON.stringify(getSidebarPages(nav)));
                }
                if (Array.isArray(data.data.visible_pages) && data.data.visible_pages.length > 0) {
                    const visiblePages = applySidebarVisibility(data.data.visible_pages);
                    localStorage.setItem(SIDEBAR_VISIBLE_STORAGE_KEY, JSON.stringify(visiblePages));
                    window.__savedSidebarVisiblePages = visiblePages;
                    const nextPage = resolveVisiblePage(currentPage);
                    if (currentPage && nextPage !== currentPage) {
                        switchPage(nextPage);
                    }
                }
            } catch (e) {
                console.error('[Sidebar] Failed to load config:', e);
            }
        }

        const LAZY_FRAME_PROGRESS_DELAY_MS = 650;

        function prepareLazyFrameLoadingSurface(frame) {
            const shell = frame?.closest('.embedded-frame-shell');
            const status = shell?.querySelector('.embedded-frame-loading');
            if (!status) return null;
            if (!status.dataset.loadingText) {
                status.dataset.loadingText = status.textContent.trim() || '页面仍在准备中…';
            }
            if (status.dataset.hydrated === 'true') return status;

            const skeleton = document.createElement('div');
            skeleton.className = 'embedded-frame-skeleton';
            skeleton.setAttribute('aria-hidden', 'true');

            const bar = document.createElement('div');
            bar.className = 'embedded-frame-skeleton-bar';
            const cards = document.createElement('div');
            cards.className = 'embedded-frame-skeleton-cards';
            for (let index = 0; index < 4; index += 1) {
                const card = document.createElement('div');
                card.className = 'embedded-frame-skeleton-card';
                cards.appendChild(card);
            }
            const panels = document.createElement('div');
            panels.className = 'embedded-frame-skeleton-panels';
            for (let index = 0; index < 2; index += 1) {
                const panel = document.createElement('div');
                panel.className = 'embedded-frame-skeleton-panel';
                panels.appendChild(panel);
            }
            skeleton.append(bar, cards, panels);

            const chip = document.createElement('span');
            chip.className = 'embedded-frame-loading-chip';
            const spinner = document.createElement('span');
            spinner.className = 'embedded-frame-loading-spinner';
            spinner.setAttribute('aria-hidden', 'true');
            const text = document.createElement('span');
            text.className = 'embedded-frame-loading-text';
            chip.append(spinner, text);
            status.replaceChildren(skeleton, chip);
            status.dataset.hydrated = 'true';
            return status;
        }

        function setLazyFrameStatus(frame, message) {
            const status = prepareLazyFrameLoadingSurface(frame);
            const text = status?.querySelector('.embedded-frame-loading-text');
            if (text) text.textContent = message;
        }

        function revealLazyFrame(frame, deferUntilPaint = true) {
            const shell = frame?.closest('.embedded-frame-shell');
            if (!shell || shell.dataset.frameState !== 'loading') return;
            if (frame.__surfaceReadyFallbackTimer) {
                clearTimeout(frame.__surfaceReadyFallbackTimer);
                frame.__surfaceReadyFallbackTimer = null;
            }
            if (frame.__surfaceProgressTimer) {
                clearTimeout(frame.__surfaceProgressTimer);
                frame.__surfaceProgressTimer = null;
            }
            const reveal = () => {
                if (shell.dataset.frameState === 'loading') {
                    shell.dataset.frameHasContent = 'true';
                    shell.dataset.frameState = 'ready';
                    shell.dataset.frameProgress = 'hidden';
                }
            };
            if (deferUntilPaint) requestAnimationFrame(() => requestAnimationFrame(reveal));
            else reveal();
        }

        function markLazyFrameReady(frame) {
            if (!frame) return;
            frame.dataset.surfaceReadyReceived = 'true';
            // 子页主动发出的 ready 表示它的稳定首帧已经完成布局，比 iframe
            // load（可能被图表/CDN/慢接口拖延）更可靠；收到后即可揭示。
            revealLazyFrame(frame, false);
            flushEmbeddedActiveTabFocus(frame);
        }
        window.markLazyFrameReady = markLazyFrameReady;

        const EMBEDDED_TAB_FOCUS_FRAMES = Object.freeze({
            automation: 'automation-frame',
            cluster: 'cluster-frame',
            'devices-console': 'devices-console-frame',
            'redmine-agent': 'redmine-agent-frame',
            'gerrit-dashboard': 'gerrit-dashboard-frame'
        });

        function pageForEmbeddedTabFrame(frame) {
            return Object.keys(EMBEDDED_TAB_FOCUS_FRAMES).find(
                page => document.getElementById(EMBEDDED_TAB_FOCUS_FRAMES[page]) === frame
            ) || '';
        }

        function flushEmbeddedActiveTabFocus(frame) {
            if (frame?.dataset.pendingActiveTabFocus !== 'true') return;
            const pageName = pageForEmbeddedTabFrame(frame);
            if (!pageName || currentPage !== pageName || !frame.contentWindow) {
                delete frame.dataset.pendingActiveTabFocus;
                return;
            }
            delete frame.dataset.pendingActiveTabFocus;
            requestAnimationFrame(() => {
                if (currentPage === pageName && frame.contentWindow) {
                    frame.contentWindow.postMessage(
                        {type: 'embedded-focus-active-tab'}, window.location.origin
                    );
                }
            });
        }

        function focusEmbeddedActiveTab(pageName) {
            const frameId = EMBEDDED_TAB_FOCUS_FRAMES[pageName];
            const frame = frameId && document.getElementById(frameId);
            if (!frame) return;
            frame.dataset.pendingActiveTabFocus = 'true';
            if (frame.dataset.surfaceReadyReceived === 'true'
                    || frame.dataset.documentLoaded === 'true') {
                flushEmbeddedActiveTabFocus(frame);
            }
        }
        window.focusEmbeddedActiveTab = focusEmbeddedActiveTab;

        function focusSidebarNavigationTarget(pageName) {
            if (EMBEDDED_TAB_FOCUS_FRAMES[pageName]) {
                focusEmbeddedActiveTab(pageName);
                return;
            }
            // 非 Tab 页面不能保留上一页 iframe 的焦点；页面容器作为
            // 稳定的键盘导航锚点，不抢占页面内输入框或终端的用户焦点。
            const page = document.getElementById(`page-${pageName}`);
            if (!page) return;
            page.tabIndex = -1;
            page.focus({preventScroll: true});
        }
        window.focusSidebarNavigationTarget = focusSidebarNavigationTarget;

        function bindLazyFrameState(frame) {
            if (!frame || frame.dataset.frameStateBound === 'true') return;
            frame.dataset.frameStateBound = 'true';
            frame.addEventListener('load', () => {
                const shell = frame.closest('.embedded-frame-shell');
                if (!shell) return;
                frame.dataset.documentLoaded = 'true';
                if (frame.dataset.waitForReady === 'true'
                        && frame.dataset.surfaceReadyReceived !== 'true') {
                    // 同源微前端在首批数据已原子渲染后主动通知。
                    // 超时只是容错，避免子页异常时永久遮挡诊断信息。
                    frame.__surfaceReadyFallbackTimer = setTimeout(
                        () => markLazyFrameReady(frame),
                        2500
                    );
                    return;
                }
                // load 事件早于 iframe 内容的首次合成；延后两帧再揭示，
                // 避免用户看到 about:blank 或子页样式尚未绘制的中间帧。
                revealLazyFrame(frame);
            });
            frame.addEventListener('error', () => {
                const shell = frame.closest('.embedded-frame-shell');
                if (!shell) return;
                if (frame.__surfaceReadyFallbackTimer) clearTimeout(frame.__surfaceReadyFallbackTimer);
                if (frame.__surfaceProgressTimer) clearTimeout(frame.__surfaceProgressTimer);
                shell.dataset.frameState = 'error';
                shell.dataset.frameProgress = 'visible';
                setLazyFrameStatus(frame, '页面加载失败，请稍后重试');
            });
        }

        function setLazyFrameSource(frame, source) {
            if (!frame || !source) return;
            bindLazyFrameState(frame);
            const shell = frame.closest('.embedded-frame-shell');
            if (shell) {
                const status = prepareLazyFrameLoadingSurface(frame);
                setLazyFrameStatus(frame, status?.dataset.loadingText || '页面仍在准备中…');
                shell.dataset.frameState = 'loading';
                shell.dataset.frameProgress = 'hidden';
            }
            if (frame.__surfaceReadyFallbackTimer) clearTimeout(frame.__surfaceReadyFallbackTimer);
            if (frame.__surfaceProgressTimer) clearTimeout(frame.__surfaceProgressTimer);
            frame.__surfaceProgressTimer = setTimeout(() => {
                if (shell?.dataset.frameState === 'loading') {
                    shell.dataset.frameProgress = 'visible';
                }
                frame.__surfaceProgressTimer = null;
            }, LAZY_FRAME_PROGRESS_DELAY_MS);
            frame.dataset.documentLoaded = 'false';
            frame.dataset.surfaceReadyReceived = 'false';
            frame.setAttribute('src', source);
        }
        window.setLazyFrameSource = setLazyFrameSource;

        // 页面切换函数：iframe 微前端懒加载
        // 解决 F5 刷新后 iframe 内容丢失的问题
        function ensureLazyFrameLoaded(pageName, forceReload = false) {
            const frameMap = {
                'gms-assistant': 'gms-assistant-frame',
                'automation': 'automation-frame',
                'cluster': 'cluster-frame',
                'devices-console': 'devices-console-frame',
                'redmine-agent': 'redmine-agent-frame',
                'gerrit-dashboard': 'gerrit-dashboard-frame',
                'architecture': 'architecture-iframe'
            };
            const frameId = frameMap[pageName];
            if (!frameId) return;
            const frame = document.getElementById(frameId);
            if (!frame) return;

            const dataSrc = frame.getAttribute('data-src');
            if (!dataSrc) return;

            const hasSrc = frame.hasAttribute('src');

            // 首次加载：iframe 没有 src，从 data-src 加载
            if (!hasSrc) {
                setLazyFrameSource(frame, dataSrc);
                frame.dataset.lazyLoadedAt = String(Date.now());
                return;
            }

            // F5 刷新后：iframe 有 src 但内容可能空白。
            // 只有 forceReload=true 时才强制重新加载（避免页面切换时不必要的重载）。
            // 如果 iframe 在 10 秒内刚被加载过（例如 DOMContentLoaded 提前触发），
            // 跳过重复重载，避免用户看到二次结构骨架。
            if (forceReload) {
                const loadedAt = Number(frame.dataset.lazyLoadedAt || 0);
                if (loadedAt && Date.now() - loadedAt < 10000) return;
                const currentSrc = frame.getAttribute('src');
                if (currentSrc) setLazyFrameSource(frame, currentSrc);
            }
        }

        const pendingAuthPageInitializers = new Set();

        function initializePageSafely(pageName) {
            runPageInitializers(pageName).catch(error => {
                console.error(`[Navigation] Failed to initialize page ${pageName}:`, error);
                if (currentPage === pageName) {
                    showToast(`页面初始化失败：${error?.message || '未知错误'}`, 'error');
                }
            });
        }

        async function runPageInitializers(pageName) {
            if (!state.authReady) {
                if (pendingAuthPageInitializers.has(pageName)) return;
                pendingAuthPageInitializers.add(pageName);
                window.addEventListener(
                    'gms:auth-ready',
                    () => {
                        pendingAuthPageInitializers.delete(pageName);
                        // Do not initialize a page that the user already left;
                        // entering it later will invoke this initializer again.
                        if (currentPage === pageName) initializePageSafely(pageName);
                    },
                    { once: true }
                );
                return;
            }
            if ((pageName === 'desktop' || pageName === 'terminal')
                    && typeof initializeClusterMode === 'function') {
                // Resolve infrastructure + persisted workspace scope before
                // either host workspace is allowed to choose its layout.
                await initializeClusterMode();
            }
            // 页面切换和刷新恢复使用相同初始化入口。
            if (pageName === 'test') {
                if (typeof loadClusterWorkers === 'function') {
                    loadClusterWorkers().catch(error => debugLog('[Cluster] Worker list unavailable:', error));
                }
                // 等待 workspace 上下文就绪后再加载设备，避免先闪现本机设备。
                if (typeof loadDevices === 'function') {
                    (window.GmsWorkspace?.ready || Promise.resolve())
                        .then(() => loadDevices(false))
                        .catch(() => {});
                }
                if (typeof loadTestSuites === 'function') loadTestSuites(false).catch(() => {});
                if (typeof checkInitialTestStatus === 'function') checkInitialTestStatus().catch(() => {});
            }
            if (pageName === 'terminal') {
                if (!await ensureTerminalElevation()) return;
                // Load local xterm assets while fetching the host directory so
                // the first terminal pane can open immediately afterwards. A
                // local target does not depend on the cluster round trip, so
                // let that directory refresh finish behind the visible mount.
                const terminalContext = window.GmsWorkspace?.get?.() || {};
                const needsRemoteHost = terminalContext.scope_mode === 'cluster'
                    && !isLocalWorkspaceWorker(
                        terminalContext.worker_id || workspaceLocalWorkerId()
                    );
                const terminalHostsReady = loadTerminalClusterHosts().catch(error =>
                    debugLog('[Terminal] Worker host directory unavailable:', error));
                const terminalAssetsReady = loadXTermScripts().catch(error => {
                    debugLog('[Terminal] xterm assets unavailable:', error);
                    throw error;
                });
                if (needsRemoteHost) {
                    await Promise.all([terminalHostsReady, terminalAssetsReady]);
                } else {
                    await terminalAssetsReady;
                    terminalHostsReady.catch(() => {});
                }
                const pendingCommand = sessionStorage.getItem('pending_terminal_command');
                const commandSource = sessionStorage.getItem('command_source');
                pendingAdbDevice = pendingAdbDevice || sessionStorage.getItem('pending_adb_device');

                if (!pendingAdbDevice && !pendingCommand) {
                    await ensureTerminalWorkspaceInitialized();
                } else if (!terminalInitialized || pendingAdbDevice || pendingCommand) {
                    if ((pendingAdbDevice || pendingCommand) && terminalInitialized) {
                        debugLog('Forcing terminal re-initialization');
                        isReconnecting = true;
                        if (terminalSocket) {
                            terminalSocket.close();
                            terminalSocket = null;
                        }
                        if (terminal) {
                            terminal.dispose();
                            terminal = null;
                        }
                        terminalInitialized = false;
                        setTimeout(() => {
                            isReconnecting = false;
                        }, 100);
                    }

                    if (pendingCommand && commandSource === 'route_check') {
                        updateSilentMode(false, 'route', pendingCommand);
                        sessionStorage.removeItem('pending_terminal_command');
                        sessionStorage.removeItem('command_source');
                        debugLog('Route command mode enabled, command:', pendingCommand);
                    }

                    initTerminal();
                }
            }

            if (pageName === 'users') {
                if (window.agentAccessPanelIsOpen?.()) {
                    stopUsersAutoRefresh();
                    agentAccessReload();
                } else {
                    loadUsersList();
                    startUsersAutoRefresh();
                }
            } else {
                stopUsersAutoRefresh();
            }

            if (pageName === 'security-audit') {
                if (!securityAuditState.loaded) loadSecurityAudit(true);
            }
            if (pageName === 'devices') {
                loadDevicesManagement();
            } else {
                stopDevicesAutoRefresh();
            }
            if (pageName === 'reports') {
                loadTestReports(typeof currentUserFilter !== 'undefined' ? currentUserFilter : false);
            } else if (typeof cleanupReportsPolling === 'function') {
                cleanupReportsPolling();
            }
            if (pageName === 'api-docs') {
                loadApiDocs();
            }
            if (pageName === 'test-suites') {
                await initTestSuiteBrowserPage();
            }
            if (pageName === 'apk-analysis') {
                setTimeout(() => {
                    if (typeof window.initApkAnalysisPage === 'function') {
                        window.initApkAnalysisPage();
                    }
                }, 50);
            }
            if (pageName === 'terminal' && terminalInitialized && terminal && (pendingAdbDevice || sessionStorage.getItem('pending_terminal_command'))) {
                setTimeout(() => {
                    terminal.focus();
                }, 100);
            }
            if (pageName === 'desktop') {
                if (!await ensureTerminalElevation(false, '打开主机桌面', '主机桌面')) return;
                await ensureDesktopInitialized();
            }
            if (pageName === 'websites') {
                loadToolsList();
            }
            if (pageName === 'tools') {
                ut_loadToolsList();
            }
            if (pageName === 'agent' && typeof initAgentPage === 'function') {
                initAgentPage();
            }
            if (pageName === 'notes' && typeof initNotesPage === 'function') {
                initNotesPage();
            }
        }

        function switchPage(pageName, event, options = {}) {
            if (event) {
                event.preventDefault();
            }
            pageName = resolveVisiblePage(pageName);
            // 页面切换时保留 VNC iframe 和终端 WebSocket，避免重复连接。
            const pageEl = document.getElementById(`page-${pageName}`);
            if (!pageEl) return;
            const initStyle = document.getElementById('page-init-style');
            if (initStyle) {
                initStyle.remove();
            }

            // 更新导航栏高亮
            document.querySelectorAll('.sidebar-item').forEach(item => {
                item.classList.remove('active');
            });
            const activeItem = document.querySelector(`[data-page="${pageName}"]`);
            if (activeItem) {
                activeItem.classList.add('active');
            }

            // 隐藏所有页面
            document.querySelectorAll('.page-content.active').forEach(el => el.classList.remove('active'));

            // 显示目标页面
            pageEl.classList.add('active');
            ensureLazyFrameLoaded(pageName);
            window.GmsWorkspace?.setActivePage(pageName);
            window.GmsWorkspace?.update({origin_page: pageName}, {source: 'navigation'});
            window.GmsWorkspace?.postToFrame(pageName);
            if (document.readyState !== 'complete') {
                window.__shellPageSwitchedBeforeLoad = true;
            }

            // 清除DOM缓存，避免保留已隐藏页面的元素引用
            if (typeof clearDomCache === 'function') clearDomCache();

            currentPage = pageName;

            // 保存当前页面。hash 参与刷新恢复，localStorage/cookie 用于首次无 hash 的回退。
            saveCurrentPageState(pageName, { updateHash: options.updateHash !== false });

            // 动态更新页面标题
            document.title = PAGE_TITLES[pageName] || 'GMS远程测试';
            if (typeof recordSecurityPageView === 'function') {
                recordSecurityPageView(pageName);
            }

            initializePageSafely(pageName);
        }

        // 将 switchPage 导出到全局作用域，供外部脚本调用
        window.switchPage = switchPage;



        // ==================== 个人知识库 ====================
        const kbState = {
            initialized: false,
            spaces: [],
            nodes: [],
            currentSpace: 'gms',
            currentParent: '',
            currentDocId: '',
            currentNodeId: '',
            currentFavorite: false,
            dirty: false,
            docsLoaded: false,
            docsRequestGeneration: 0,
        };

        function kbApiData(result) {
            if (result && result.success === false) throw new Error(result.error || '请求失败');
            return result && result.data !== undefined ? result.data : result;
        }

        async function kbFetch(url, options) {
            const response = await fetch(url, {credentials: 'same-origin', ...(options || {})});
            const result = await response.json().catch(() => ({}));
            if (!response.ok || result.success === false) throw new Error(result.error || result.message || `HTTP ${response.status}`);
            return kbApiData(result);
        }

        function kbEsc(text) {
            return String(text == null ? '' : text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }

        function kbSnippet(text, size = 120) {
            const value = String(text || '').replace(/\s+/g, ' ').trim();
            return value.length > size ? value.slice(0, size) + '...' : value;
        }

        async function initNotesPage() {
            if (kbState.initialized) return;
            kbState.initialized = true;
            const content = document.getElementById('kb-doc-content');
            const title = document.getElementById('kb-doc-title');
            const tags = document.getElementById('kb-doc-tags');
            [content, title, tags].forEach(el => { if (el) el.addEventListener('input', () => kbState.dirty = true); });
            await kbRefreshAll();
        }

        async function kbRefreshAll() {
            // 三个加载互不依赖各自的返回值，并发跑省一次往返。
            await Promise.all([kbLoadSpaces(), kbLoadTree(), kbLoadDocs()]);
        }

        async function kbLoadSpaces() {
            const data = await kbFetch('/api/knowledge/spaces');
            kbState.spaces = data.spaces || [];
            if (!kbState.spaces.find(s => s.space_id === kbState.currentSpace) && kbState.spaces.length) kbState.currentSpace = kbState.spaces[0].space_id;
            const box = document.getElementById('kb-space-list');
            if (!box) return;
            box.innerHTML = kbState.spaces.map(sp => `
                <button class="btn-xs" style="width:100%;margin-bottom:6px;text-align:left;${sp.space_id === kbState.currentSpace ? 'border-color:var(--primary-color);' : ''}" data-click="kbSelectSpace" data-a0="${kbEsc(sp.space_id)}">
                    ${kbEsc(sp.name)} <span style="color:var(--text-secondary)">(${sp.doc_count || 0})</span>
                </button>
            `).join('');
        }

        async function kbLoadTree() {
            const data = await kbFetch('/api/knowledge/tree?space_id=' + encodeURIComponent(kbState.currentSpace));
            kbState.nodes = data.nodes || [];
            const tree = document.getElementById('kb-tree');
            if (!tree) return;
            const children = {};
            kbState.nodes.forEach(n => {
                const pid = n.parent_id || '';
                (children[pid] = children[pid] || []).push(n);
            });
            function render(pid, depth) {
                return (children[pid] || []).map(n => {
                    const isDoc = n.type === 'doc';
                    const active = (isDoc && n.doc_id === kbState.currentDocId) || (!isDoc && n.node_id === kbState.currentParent);
                    const click = isDoc ? 'kbOpenDoc' : 'kbSelectFolder';
                    const clickArg = isDoc ? kbEsc(n.doc_id) : kbEsc(n.node_id);
                    return `
                        <div>
                            <button class="btn-xs" style="width:100%;margin-bottom:4px;text-align:left;padding-left:${6 + depth * 14}px;${active ? 'border-color:var(--primary-color);' : ''}" data-click="${click}" data-a0="${clickArg}">
                                ${isDoc ? '📄' : '📁'} ${kbEsc(n.title)}
                            </button>
                            ${!isDoc ? render(n.node_id, depth + 1) : ''}
                        </div>
                    `;
                }).join('');
            }
            tree.innerHTML = `<button class="btn-xs" style="width:100%;margin-bottom:6px;text-align:left;${kbState.currentParent ? '' : 'border-color:var(--primary-color);'}" data-click="kbSelectFolder" data-a0="">全部文档</button>` + (render('', 0) || '<div class="suite-empty" style="font-size:12px;padding:8px;">暂无目录</div>');
        }

        async function kbLoadDocs() {
            const box = document.getElementById('kb-doc-list');
            const requestGeneration = ++kbState.docsRequestGeneration;
            const hadRenderedDocs = kbState.docsLoaded;
            if (box) {
                box.setAttribute('aria-busy', 'true');
                if (!hadRenderedDocs) {
                    box.innerHTML = '<div class="suite-empty" style="padding:20px;text-align:center;">加载中...</div>';
                }
            }
            const q = (document.getElementById('kb-search-input') || {}).value || '';
            const params = new URLSearchParams();
            params.set('space_id', kbState.currentSpace);
            if (q.trim()) params.set('q', q.trim());
            if (!q.trim() && kbState.currentParent) params.set('parent_id', kbState.currentParent);
            const url = q.trim() ? '/api/knowledge/search?' + params.toString() : '/api/knowledge/docs?' + params.toString();
            try {
                const data = await kbFetch(url);
                if (requestGeneration !== kbState.docsRequestGeneration) return;
                const docs = data.docs || data.items || [];
                if (!box) return;
                if (!docs.length) {
                    box.innerHTML = '<div class="suite-empty" style="padding:28px;text-align:center;">暂无文档</div>';
                    kbState.docsLoaded = true;
                    return;
                }
                box.innerHTML = docs.map(doc => `
                    <div class="report-failure-card" data-doc-id="${kbEsc(doc.doc_id)}" style="cursor:pointer;margin-bottom:8px;${doc.doc_id === kbState.currentDocId ? 'border-color:var(--primary-color);' : ''}" data-click="kbOpenDoc" data-a0="${kbEsc(doc.doc_id)}">
                        <div style="font-weight:600;margin-bottom:4px;">${doc.favorite ? '★ ' : ''}${kbEsc(doc.title || '无标题')}</div>
                        <div style="font-size:12px;color:var(--text-secondary);line-height:1.5;">${kbEsc(doc.summary || kbSnippet(doc.content_md || doc.raw_content, 110))}</div>
                        <div style="margin-top:6px;">${(doc.tags || []).map(t => `<span class="badge">${kbEsc(t)}</span>`).join(' ')}</div>
                    </div>
                `).join('');
                kbState.docsLoaded = true;
            } catch (error) {
                if (requestGeneration !== kbState.docsRequestGeneration) return;
                if (hadRenderedDocs) showToast('文档列表刷新失败: ' + error.message, 'error');
                else if (box) box.innerHTML = `<div class="suite-empty" style="padding:20px;text-align:center;">加载失败: ${kbEsc(error.message)}</div>`;
            } finally {
                if (requestGeneration === kbState.docsRequestGeneration && box) {
                    box.setAttribute('aria-busy', 'false');
                }
            }
        }

        function kbSelectSpace(spaceId) {
            kbState.currentSpace = spaceId || 'gms';
            kbState.currentParent = '';
            kbState.currentDocId = '';
            kbClearEditor();
            kbRefreshAll();
        }

        function kbSelectFolder(nodeId) {
            kbState.currentParent = nodeId || '';
            kbLoadTree();
            kbLoadDocs();
        }

        function kbClearEditor() {
            const title = document.getElementById('kb-doc-title');
            const tags = document.getElementById('kb-doc-tags');
            const content = document.getElementById('kb-doc-content');
            if (title) title.value = '';
            if (tags) tags.value = '';
            if (content) content.value = '';
            kbState.currentDocId = '';
            kbState.currentNodeId = '';
            kbState.currentFavorite = false;
            kbState.dirty = false;
            const info = document.getElementById('kb-side-info');
            if (info) info.innerHTML = '选择或新建一篇文档。';
        }

        async function kbOpenDoc(docId) {
            const doc = await kbFetch('/api/knowledge/docs/' + encodeURIComponent(docId));
            kbState.currentDocId = doc.doc_id;
            kbState.currentNodeId = doc.node_id;
            kbState.currentFavorite = !!doc.favorite;
            kbState.currentParent = doc.parent_id || '';
            document.getElementById('kb-doc-title').value = doc.title || '';
            document.getElementById('kb-doc-tags').value = (doc.tags || []).join(', ');
            document.getElementById('kb-doc-content').value = doc.content_md || '';
            kbState.dirty = false;
            kbRenderInfo(doc);
            kbLoadTree();
            kbLoadDocs();
        }

        function kbRenderInfo(doc) {
            const info = document.getElementById('kb-side-info');
            if (!info) return;
            const links = doc.links || [];
            const attachments = doc.attachments || [];
            info.innerHTML = `
                <div><b>更新：</b>${kbEsc(doc.updated_at || '')}</div>
                <div><b>来源：</b>${kbEsc(doc.source || 'manual')}</div>
                ${doc.summary ? `<div style="margin-top:6px;"><b>摘要：</b>${kbEsc(doc.summary)}</div>` : ''}
                ${links.length ? `<div style="margin-top:6px;"><b>关联：</b><div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;">${links.map(l => `<button type="button" class="btn-xs" data-link-type="${kbEsc(l.target_type)}" data-link-id="${kbEsc(l.target_id)}" data-click="kbOpenKnowledgeLink" data-r0="dataset.linkType" data-r1="dataset.linkId">${kbEsc(kbKnowledgeLinkLabel(l))}</button>`).join('')}</div></div>` : ''}
                ${attachments.length ? `<div style="margin-top:6px;"><b>附件：</b><div style="margin-top:4px;">${attachments.map(a => `<a class="btn-xs" style="display:inline-block;margin:0 4px 4px 0;text-decoration:none;" href="/api/knowledge/docs/${encodeURIComponent(doc.doc_id)}/attachments/${encodeURIComponent(a.attachment_id)}/download">${kbEsc(a.original_name)}</a>`).join('')}</div></div>` : ''}
            `;
        }

        function kbKnowledgeLinkLabel(link) {
            const labels = {test_report:'测试报告', redmine_issue:'Redmine', gerrit_change:'Gerrit', test_case:'测试用例'};
            return `${labels[link.target_type] || link.target_type}: ${link.title || link.target_id}`;
        }

        function kbOpenKnowledgeLink(type, id) {
            const value = String(id || '').trim();
            if (!value) return;
            if (type === 'test_report') {
                if (typeof analyzeReport === 'function') analyzeReport(value);
                else window.GmsWorkspace?.navigate('reports', {report_timestamp:value, origin_page:'notes'});
                return;
            }
            if (type === 'redmine_issue') {
                window.GmsWorkspace?.navigate('redmine-agent', {redmine_issue_id:value, origin_page:'notes'});
                return;
            }
            if (type === 'gerrit_change') {
                window.GmsWorkspace?.navigate('gerrit-dashboard', {gerrit_change_id:value, origin_page:'notes'});
                return;
            }
            if (type === 'test_case') {
                window.GmsWorkspace?.navigate('test', {origin_page:'notes'});
            }
        }

        async function kbNewSpace() {
            const name = prompt('知识库名称');
            if (!name || !name.trim()) return;
            const sp = await kbFetch('/api/knowledge/spaces', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name.trim()})});
            kbState.currentSpace = sp.space_id;
            await kbRefreshAll();
        }

        async function kbNewFolder() {
            const title = prompt('目录名称');
            if (!title || !title.trim()) return;
            await kbFetch('/api/knowledge/folders', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({space_id:kbState.currentSpace, parent_id:kbState.currentParent, title:title.trim()})});
            await kbLoadTree();
        }

        async function kbNewDoc() {
            const doc = await kbFetch('/api/knowledge/docs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({space_id:kbState.currentSpace, parent_id:kbState.currentParent, title:'新文档', content_md:'# 新文档\n\n'})});
            await kbRefreshAll();
            await kbOpenDoc(doc.doc_id);
        }

        async function kbSaveDoc() {
            const title = (document.getElementById('kb-doc-title') || {}).value || '';
            const tags = (document.getElementById('kb-doc-tags') || {}).value || '';
            const content = (document.getElementById('kb-doc-content') || {}).value || '';
            if (!content.trim()) { showToast('文档内容不能为空', 'warning'); return; }
            let doc;
            if (kbState.currentDocId) {
                doc = await kbFetch('/api/knowledge/docs/' + encodeURIComponent(kbState.currentDocId), {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title, tags, content_md:content, raw_content:content})});
            } else {
                doc = await kbFetch('/api/knowledge/docs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({space_id:kbState.currentSpace, parent_id:kbState.currentParent, title, tags, content_md:content})});
            }
            kbState.dirty = false;
            showToast('已保存', 'success');
            await kbRefreshAll();
            await kbOpenDoc(doc.doc_id);
        }

        async function kbToggleFavorite() {
            if (!kbState.currentDocId) return;
            const doc = await kbFetch('/api/knowledge/docs/' + encodeURIComponent(kbState.currentDocId), {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({favorite: kbState.currentFavorite ? 0 : 1})});
            await kbOpenDoc(doc.doc_id);
        }

        async function kbShowHistory() {
            if (!kbState.currentDocId) { showToast('请先选择文档', 'warning'); return; }
            const data = await kbFetch('/api/knowledge/docs/' + encodeURIComponent(kbState.currentDocId) + '/versions');
            const versions = data.versions || [];
            const info = document.getElementById('kb-side-info');
            if (!info) return;
            info.innerHTML = `<div style="font-weight:600;margin-bottom:8px;">版本历史</div>` + (versions.length
                ? versions.map(version => `<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;padding-bottom:6px;border-bottom:1px solid var(--border-color);">
                    <span style="flex:1;">v${version.version_no} · ${kbEsc(version.created_at)}<br>${kbEsc(version.title)}</span>
                    <button class="btn-xs" data-version-id="${kbEsc(version.version_id)}" data-click="kbRestoreVersion" data-r0="dataset.versionId">恢复</button>
                  </div>`).join('')
                : '<div>暂无历史版本</div>');
        }

        async function kbRestoreVersion(versionId) {
            if (!kbState.currentDocId || !versionId) return;
            if (!await showConfirmDialog(
                '恢复历史版本',
                '恢复这个历史版本？\n当前内容也会保留为新版本。'
            )) return;
            const doc = await kbFetch('/api/knowledge/docs/' + encodeURIComponent(kbState.currentDocId) + '/versions/' + encodeURIComponent(versionId) + '/restore', {method:'POST'});
            await kbOpenDoc(doc.doc_id);
            showToast('历史版本已恢复', 'success');
        }

        async function kbDeleteCurrent() {
            if (!kbState.currentNodeId) return;
            if (!await showConfirmDialog(
                '删除知识库文档',
                '确定删除当前文档？此操作不可恢复。'
            )) return;
            await kbFetch('/api/knowledge/nodes/' + encodeURIComponent(kbState.currentNodeId), {method:'DELETE'});
            kbClearEditor();
            await kbRefreshAll();
        }

        function kbSearch() { kbLoadDocs(); }

        function kbOpenUpload() {
            document.getElementById('kb-upload-input').value = '';
            document.getElementById('kb-upload-status').textContent = '';
            ModalManager.open('kb-upload-modal');
        }
        function kbCloseUpload() { ModalManager.close('kb-upload-modal'); }

        async function kbHandleFileSelect(input) {
            const files = Array.from(input.files || []);
            const status = document.getElementById('kb-upload-status');
            if (!files.length) return;
            try {
                for (let i = 0; i < files.length; i++) {
                    if (status) status.textContent = `上传解析中 ${i + 1}/${files.length}: ${files[i].name}`;
                    const form = new FormData();
                    form.append('file', files[i]);
                    form.append('space_id', kbState.currentSpace);
                    form.append('parent_id', kbState.currentParent);
                    const doc = await kbFetch('/api/knowledge/upload', {method:'POST', body:form});
                    kbState.currentDocId = doc.doc_id;
                }
                kbCloseUpload();
                await kbRefreshAll();
                if (kbState.currentDocId) await kbOpenDoc(kbState.currentDocId);
            } catch (e) {
                if (status) status.textContent = e.message;
            }
        }

        async function kbAsk() {
            const question = prompt('全库提问');
            if (!question || !question.trim()) return;
            const data = await kbFetch('/api/knowledge/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question:question.trim(), space_id:kbState.currentSpace})});
            const info = document.getElementById('kb-side-info');
            if (info) {
                const contexts = Array.isArray(data.contexts) ? data.contexts : [];
                const sources = contexts.length ? `
                    <div style="font-weight:600;margin-top:10px;margin-bottom:6px;">来源</div>
                    ${contexts.map((ctx, i) => `
                        <div style="border:1px solid var(--border-color);border-radius:6px;padding:6px;margin-bottom:6px;">
                            <button class="btn-xs" data-click="kbOpenDoc" data-a0="${kbEsc(ctx.doc_id)}" style="margin-bottom:4px;">[${i + 1}] ${kbEsc(ctx.title || '无标题')}</button>
                            <div style="color:var(--text-secondary);line-height:1.5;">${kbEsc(kbSnippet(ctx.snippet || ctx.summary || '', 180))}</div>
                        </div>
                    `).join('')}
                ` : '';
                info.innerHTML = `
                    <div style="font-weight:600;margin-bottom:6px;">问答结果 ${data.mode === 'ai' && data.provider ? `<span style="font-weight:normal;color:var(--text-secondary);">(${kbEsc(data.provider)})</span>` : ''}</div>
                    <div style="white-space:pre-wrap;color:var(--text-color);line-height:1.6;">${kbEsc(data.answer || '')}</div>
                    ${sources}
                `;
            }
        }

        Object.assign(window, {initNotesPage, kbNewSpace, kbSelectSpace, kbSelectFolder, kbNewFolder, kbNewDoc, kbOpenDoc, kbSaveDoc, kbToggleFavorite, kbShowHistory, kbRestoreVersion, kbDeleteCurrent, kbSearch, kbOpenUpload, kbCloseUpload, kbHandleFileSelect, kbAsk, kbOpenKnowledgeLink});

        async function saveToWiki(payload) {
            const notebookSpaces = {
                '测试问题库': 'issues', 'Redmine问题沉淀': 'issues',
                'Gerrit补丁说明': 'issues', '设备接入文档': 'devices',
                '固件烧录文档': 'devices', 'FAQ': 'gms'
            };
            const tags = payload.tags || (payload.notebook ? [payload.notebook] : '');
            const body = {
                space_id: payload.space_id || notebookSpaces[payload.notebook] || 'issues',
                title: payload.title || '',
                content_md: payload.content || payload.content_md || '',
                tags,
                source: payload.source || 'diagnosis',
                links: Array.isArray(payload.links) ? payload.links : [],
            };
            const resp = await fetch('/api/knowledge/docs', {method:'POST', headers:{'Content-Type':'application/json'}, credentials:'same-origin', body:JSON.stringify(body)});
            const result = await resp.json().catch(() => ({}));
            if (!resp.ok || result.success === false) throw new Error(result.error || result.message || '存入知识库失败');
            return result.data !== undefined ? result.data : result;
        }
        Object.assign(window, {saveToWiki});

        // ==================== Shell 引导（boot 保护 + 初始页面恢复）====================
        // 页面加载完成后，如果是终端页面则初始化终端
        window.addEventListener('load', () => {
            // 核心脚本（state.js/api.js/navigation.js）因网络错误未加载时，内联逻辑会
            // 因缺少全局 state 崩溃（控制台报 "state is not defined"）。
            // 这里给出明确提示而非 ReferenceError，便于定位到本机网络问题。
            if (
        typeof state === 'undefined'
        || typeof apiCall === 'undefined'
        || window.GmsNavigationReady !== true
            ) {
        console.error('[Boot] 核心脚本未加载完成，页面初始化中止');
        const bootBanner = document.createElement('div');
        bootBanner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;'
            + 'background:#b3261e;color:#fff;padding:10px 16px;font-size:14px;'
            + 'text-align:center;line-height:1.6;';
        bootBanner.textContent = '页面核心脚本加载失败（网络错误），功能暂不可用。'
            + '请刷新重试；若反复出现，请关闭本站多余标签页并检查本机网络代理/端口占用后重启浏览器。';
        document.body.appendChild(bootBanner);
        return;
            }
            // 获取页面加载前确定的目标页面
            const pageWasSwitchedBeforeLoad = Boolean(window.__shellPageSwitchedBeforeLoad);
            let targetPage = pageWasSwitchedBeforeLoad
        ? currentPage
        : (window.__targetPage || localStorage.getItem('gms_current_page') || readCurrentPageCookie() || 'test');
            applySidebarVisibility(getSavedSidebarVisiblePages());
            targetPage = resolveVisiblePage(targetPage);

            // 保存初始页面状态到 localStorage（确保首次访问后刷新能保持）
            saveCurrentPageState(targetPage);

            // 移除临时内联样式
            const initStyle = document.getElementById('page-init-style');
            if (initStyle) {
        initStyle.remove();
            }

            // 认证状态确认后再读取客户端信息，避免首次初始化页面产生 401。
            runAfterAuthReady(initClientInfo);

            // Sidebar 事件委托：替代逐项 inline onclick（去 unsafe-inline 的
            // 第一批迁移）。data-page 已标注目标页面，keydown 同样支持键盘导航。
            document.getElementById('sidebar-nav')?.addEventListener('click', event => {
                const item = event.target.closest('.sidebar-item[data-page]');
                if (item) {
                    switchPage(item.dataset.page, event);
                }
            });

            if (pageWasSwitchedBeforeLoad && targetPage === currentPage) {
        return;
            }

            // 先设置导航栏高亮（在显示页面前）
            document.querySelectorAll('.sidebar-item').forEach(item => {
        item.classList.remove('active');
            });
            const activeItem = document.querySelector(`[data-page="${targetPage}"]`);
            if (activeItem) {
        activeItem.classList.add('active');
            }

            // 使用正常的类系统显示目标页面
            document.querySelectorAll('.page-content').forEach(page => {
        page.classList.remove('active');
            });
            const targetPageEl = document.getElementById(`page-${targetPage}`);
            if (targetPageEl) {
        targetPageEl.classList.add('active');
            }

            // 刷新恢复页面时显式加载懒加载 iframe。
            ensureLazyFrameLoaded(targetPage, true);
            window.GmsWorkspace?.setActivePage(targetPage);

            currentPage = targetPage;

            // 更新页面标题
            document.title = PAGE_TITLES[targetPage] || 'GMS远程测试';
            runAfterAuthReady(function() {
        if (typeof recordSecurityPageView === 'function') {
            recordSecurityPageView(targetPage);
        }
            });
            runPageInitializers(targetPage);

        });
