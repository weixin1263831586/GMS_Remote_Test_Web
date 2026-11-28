        // ==================== 用户管理功能 ====================
        let usersRefreshInterval = null;
        let usersHasLoaded = false;
        let usersListCache = [];
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
            // 权限或加载失败时，不得通过筛选重新呈现旧缓存。
            usersHasLoaded = false;
            usersListCache = [];
            const counter = document.getElementById('users-list-visible-count');
            if (counter) counter.textContent = '无法读取用户列表';
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
                    && !window.agentAccessPanelIsOpen?.()
                    && !window.sdkConfigPanelIsOpen?.()) {
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

        // 顶部统计卡为纯展示（与设备/审计页一致），不做点击筛选。
        // 在本地已授权结果上筛选，10s 自动刷新不会重置筛选条件。
        function filterUsersList() {
            if (!usersHasLoaded || usersAccessDenied) return;
            const query = (document.getElementById('users-list-search')?.value || '').trim().toLowerCase();
            const status = document.getElementById('users-list-status-filter')?.value || '';
            const visible = usersListCache.filter(user => {
                if (status && getUserDisplayStatus(user) !== status) return false;
                if (!query) return true;
                const local = user.local_devices?.devices || [];
                return [
                    user.client_id, user.ip, user.source_label,
                    ...(Array.isArray(user.devices) ? user.devices : []),
                    ...(Array.isArray(local) ? local : []),
                ].filter(Boolean).join(' ').toLowerCase().includes(query);
            });
            const counter = document.getElementById('users-list-visible-count');
            if (counter) counter.textContent = `显示 ${visible.length} / ${usersListCache.length} 位用户`;
            renderUsersList(visible);
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
            usersHasLoaded = true;
            filterUsersList();

            // 有用户直连设备仍在后台 SSH 枚举（显示"枚举中…"）时，
            // 3 秒后补一次加载，不等 10 秒轮询。
            if (currentPage === 'users'
                && !window.agentAccessPanelIsOpen?.()
                && usersListCache.some(user => user.local_devices === null)) {
                clearTimeout(usersLocalDevicesReloadTimer);
                usersLocalDevicesReloadTimer = setTimeout(() => {
                    if (currentPage === 'users' && !document.hidden
                        && !window.agentAccessPanelIsOpen?.()
                        && !window.sdkConfigPanelIsOpen?.()) {
                        loadUsersList();
                    }
                }, 3000);
            }
        }

        function renderUsersList(users) {
            const tbody = document.getElementById('users-table-body');
            if (!tbody) return;
            if (users.length === 0) {
                const filtered = Boolean(document.getElementById('users-list-search')?.value
                    || document.getElementById('users-list-status-filter')?.value);
                const message = filtered && usersListCache.length
                    ? '没有匹配的用户，请调整筛选条件' : '暂无用户';
                tbody.innerHTML = `<tr><td colspan="9" class="users-empty-message">${message}</td></tr>`;
                usersHasLoaded = true;
                return;
            }

            // 渲染用户列表
            tbody.innerHTML = users.map(user => {
                const activeClusterJob = getActiveUserClusterJob(user);
                const normalizedStatus = getUserDisplayStatus(user);
                const statusLabels = { testing: '测试中', online: '在线', offline: '离线' };
                const statusText = activeClusterJob
                    ? `集群测试中 · ${activeClusterJob.worker_id || '-'}`
                    : (statusLabels[normalizedStatus] || '离线');
                const statusTone = normalizedStatus === 'testing' ? 'is-warning' : normalizedStatus === 'online' ? 'is-success' : '';

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
                const localDevicesTitle = `title="${escapeHtml(localInventory?.available === false && localInventory.error
                    ? localInventory.error : localDevices)}"`;
                const sourceLabel = user.source_label || '-';
                const sourceTone = user.source === 'internal' ? 'is-accent' : user.source === 'public' ? 'is-warning' : '';
                const removeCell = renderUserRemoveCell(user, normalizedStatus);
                const clusterCell = activeClusterJob
                    ? `<button class="btn-xxs" data-cluster-job="${escapeHtml(activeClusterJob.id)}" data-worker-id="${escapeHtml(activeClusterJob.worker_id || '')}" data-attempt-id="${escapeHtml(activeClusterJob.attempt_id || '')}">任务</button>`
                    : '<span class="user-action-placeholder" aria-hidden="true">-</span>';

                return `
                    <tr>
                        <td title="${escapeHtml(user.client_id || '')}" style="text-align: center;  ">
                            ${escapeHtml(user.client_id || '')}
                        </td>
                        <td title="${escapeHtml(user.ip || '')}" style="text-align: center;  ">
                            ${escapeHtml(user.ip || '')}
                        </td>
                        <td style="text-align: center;">
                            <span class="ui-status ${sourceTone}">${escapeHtml(sourceLabel)}</span>
                        </td>
                        <td style="text-align: center;  ${localDevicesStyle}" ${localDevicesTitle}>
                            ${escapeHtml(localDevices)}
                        </td>
                        <td title="${escapeHtml(devices)}" style="text-align: center;  ">
                            ${escapeHtml(devices)}
                        </td>
                        <td title="${escapeHtml(statusText)}" style="text-align: center;">
                            <span class="ui-status ${statusTone}">${escapeHtml(statusText)}</span>
                        </td>
                        <td style="text-align: center; ">
                            ${createdAt}
                        </td>
                        <td style="text-align: center; ">
                            ${lastSeen}
                        </td>
                        <td style="text-align: center;">
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

