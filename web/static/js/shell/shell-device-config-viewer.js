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

