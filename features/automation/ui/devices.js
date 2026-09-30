// Automation page devices chunk; loaded by page.html in dependency order.

async function loadDevices(forceRefresh = false) {
    try {
        const workerId = selectedWorkerId();
        const endpoint = isLocalAutomationWorker(workerId)
            ? `/api/devices/list?force_refresh=${forceRefresh ? '1' : '0'}`
            : `/api/cluster/devices?worker_id=${encodeURIComponent(workerId)}`;
        const resp = await fetch(endpoint, {cache: 'no-store'});
        const payload = await resp.json();
        if (!resp.ok || payload.success === false) {
            throw new Error(payload.error || `测试主机 ${workerId} 的设备加载失败`);
        }
        connectedDevices = isLocalAutomationWorker(workerId) ? payload : (payload.devices || []);
        const items = Array.isArray(connectedDevices) ? connectedDevices : [];
        qs('automation-device-list').innerHTML = items.length
            ? items.map(d => {
                const id = d.id || d.device_id || d.serial || d.serial_no || '';
                const deviceState = isLocalAutomationWorker(workerId)
                    ? (d.locked ? 'allocated' : (d.status || 'available'))
                    : (d.state || 'unknown');
                const unavailable = isLocalAutomationWorker(workerId)
                    ? Boolean(d.locked || ['offline', 'unauthorized', 'unknown', 'fastboot'].includes(deviceState))
                    : Boolean(d.claimed || deviceState !== 'available');
                const transport = String(d.transport || d.properties?.transport || '').toLowerCase();
                const sourceWorker = d.adb_proxy_source_worker_id
                    || d.properties?.adb_proxy_source_worker_id
                    || d.usbip_source_worker_id
                    || d.properties?.usbip_source_worker_id
                    || '';
                const transportLabel = transport === 'adb_proxy'
                    ? ` · ADB Proxy${sourceWorker ? ` · ${sourceWorker}` : ''} · 仅免刷机测试`
                    : (transport === 'usbip'
                        ? ` · USB/IP${sourceWorker ? ` · ${sourceWorker}` : ''}`
                        : '');
                const label = `${id}${transportLabel}${unavailable ? `（${statusLabel(deviceState)}）` : ''}`;
                const flashUnsupported = currentFlashMode() !== 'skip' && transport === 'adb_proxy';
                return `<label class="checkbox-item${unavailable || flashUnsupported ? ' muted' : ''}"><input type="checkbox" value="${esc(id)}" data-transport="${esc(transport)}" data-base-disabled="${unavailable ? 'true' : 'false'}"${unavailable || flashUnsupported ? ' disabled' : ''} data-change="handleDeviceSelection" data-r0="el"> <span>${esc(label)}</span></label>`;
            }).join('')
            : '<div class="muted">未发现设备</div>';
        await applyAutomationWorkspaceContext(atsWorkspaceContext);
        handleFlashModeChange({invalidate: false});
        updateStepIndicators();
    } catch (err) { toast(err.message); }
}

async function loadTestSuitesForAutomation() {
    try {
        const previousType = qs('automation-test-type')?.value || '';
        const previousSuite = qs('automation-test-suite')?.value || '';
        const workerId = selectedWorkerId();
        const endpoint = isLocalAutomationWorker(workerId) ? '/api/test/suites'
            : `/api/cluster/suites?worker_id=${encodeURIComponent(workerId)}`;
        const resp = await fetch(endpoint, {cache: 'no-store'});
        const data = await resp.json();
        testSuites = (data.suites || data.data?.suites || [])
            .filter(suite => suite.available !== false)
            .map(suite => ({...suite,
            test_type: suite.test_type || suite.suite_type,
            full_path: suite.full_path || suite.tools_path}));
        qs('automation-test-type').innerHTML = TEST_TYPE_OPTIONS
            .map(type => `<option value="${type}">${type}</option>`).join('');
        setSelectValue('automation-test-type', TEST_TYPE_OPTIONS.includes(previousType) ? previousType : 'CTS');
        renderSuiteOptions(atsWorkspaceContext.suite_path || previousSuite);
    } catch (err) { toast(err.message); }
}
