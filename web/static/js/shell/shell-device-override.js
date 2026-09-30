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

