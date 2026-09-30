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

