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

