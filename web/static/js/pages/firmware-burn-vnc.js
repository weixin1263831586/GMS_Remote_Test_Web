async function initAndStartVnc(forceRestart = false) {
    try {
        const workerId = workspaceWorkerId();
        const logMsg = forceRestart
            ? '🔄 正在重启VNC环境（杀死旧进程并重新启动）...'
            : '🔄 正在启动VNC环境...';
        addWorkerLog(workerId, logMsg, 'info');
        const request = {force_restart: forceRestart};
        request.worker_id = workerId;
        if (!isLocalWorkspaceWorker(workerId)) {
            const host = await resolveClusterHost(workerId);
            addWorkerLog(workerId, `目标测试主机: ${workerId} (${host.address})`, 'info');
        }
        const result = isLocalWorkspaceWorker(workerId)
            ? await apiCall('/api/desktop/vnc/start', 'POST', request)
            : await apiCall(`/api/cluster/workers/${encodeURIComponent(workerId)}/restart-vnc`, 'POST');
        if (!result.success) {
            throw new Error(result.error || 'VNC 启动失败');
        }
        addWorkerLog(workerId, result.message || 'VNC 服务已就绪', 'info');
        return result;
    } catch (error) {
        addLogEntry('启动 VNC 失败: ' + error.message, 'error');
        throw error;
    }
}

async function showDeviceScreen() {
    const devices = selectedTestDeviceIds();
    if (!devices.length) {
        showToast('请先选择设备', 'warning');
        return;
    }

    try {
        const workerId = selectedClusterWorker();
        if (workerId) {
            addWorkerLog(workerId, `正在 ${workerId} 启动设备投屏...`, 'info');
            const result = await apiCall('/api/cluster/devices/actions', 'POST', {
                worker_id: workerId, devices, action: 'scrcpy_start'
            });
            addWorkerLog(workerId, `已在 ${workerId} 启动 ${result.summary?.success || devices.length} 个投屏窗口`, 'success');
            window.GmsWorkspace?.update({worker_id: workerId, origin_page: 'desktop'}, {source: 'device-screen'});
            switchPage('desktop');
            return;
        }
        addLogEntry('正在检查 VNC 服务...', 'info');
        await initAndStartVnc();

        addLogEntry('正在启动屏幕投屏...', 'info');
        const result = await apiCall('/api/devices/scrcpy', 'POST', {
            devices
        });

        // Display result message
        if (result.success) {
            // Display the detailed message from backend
            if (result.message) {
                // Split multi-line message and log each part
                const lines = result.message.split('\n');
                lines.forEach(line => {
                    if (line.includes('✅')) {
                        addLogEntry(line, 'success');
                    } else if (line.includes('ℹ️')) {
                        addLogEntry(line, 'info');
                    } else if (line.includes('❌')) {
                        addLogEntry(line, 'error');
                    } else {
                        addLogEntry(line, 'success');
                    }
                });
            } else {
                addLogEntry(`屏幕投屏已启动，共 ${result.results?.length || 0} 个设备`, 'success');
            }

            // Display device info
            if (result.vnc_sessions && result.vnc_sessions.length > 0) {
                result.vnc_sessions.forEach(session => {
                    addLogEntry(`  设备 ${session.device}: ${session.message || '已启动'}`, 'info');
                });
            }

            // Show note if available
            if (result.note) {
                addLogEntry(`ℹ️ ${result.note}`, 'info');
            }

            // Auto-switch to desktop page
            setTimeout(() => {
                if (typeof switchPage === 'function') {
                    switchPage('desktop');
                } else {
                    console.error('switchPage function not found');
                }
            }, 500);

            // Show appropriate toast message
            if (result.already_running && result.already_running.length > 0) {
                if (result.newly_started && result.newly_started.length > 0) {
                    showToast(`已启动 ${result.newly_started.length} 个设备，${result.already_running.length} 个设备已在投屏`, 'success');
                } else {
                    showToast(`所有 ${result.already_running.length} 个设备已在投屏`, 'info');
                }
            } else {
                showToast('屏幕投屏已启动', 'success');
            }
        } else {
            // Screen casting failed - show errors
            addLogEntry(result.message || '屏幕投屏启动失败', 'error');

            // Display detailed error for each device
            if (result.errors && result.errors.length > 0) {
                result.errors.forEach(errorMsg => {
                    addLogEntry(`  ❌ ${errorMsg}`, 'error');
                });
            }

            // Show results for each device
            if (result.results && result.results.length > 0) {
                result.results.forEach(r => {
                    if (r.success) {
                        addLogEntry(`  ✅ ${r.device}: 已启动`, 'success');
                    } else {
                        addLogEntry(`  ❌ ${r.device}: ${r.error || r.running ? '进程未运行' : '启动失败'}`, 'error');
                    }
                });
            }

            showToast('屏幕投屏启动失败，请查看日志', 'error');
        }
    } catch (error) {
        addLogEntry('显示屏幕失败: ' + error.message, 'error');
        showToast('显示屏幕失败: ' + error.message, 'error');
    }
}

