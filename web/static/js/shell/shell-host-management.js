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

