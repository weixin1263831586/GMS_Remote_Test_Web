// Automation page view-restore chunk; loaded by page.html in dependency order.
function toast(message) {
    const el = qs('automation-toast');
    if (!el) return;
    el.textContent = String(message || '').slice(0, 600);
    el.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 4200);
}
function compactTime(value) {
    if (!value) return '-';
    const text = String(value);
    return text.replace('T', ' ').replace(/Z$/, '');
}
function formatDevices(value) {
    if (!value) return '-';
    try {
        const parsed = typeof value === 'string' ? JSON.parse(value) : value;
        const items = Array.isArray(parsed) ? parsed : [parsed];
        const serials = items.map(item => {
            if (typeof item === 'string') return item;
            return item.serial || item.device_id || item.serial_no || '';
        }).filter(Boolean);
        return serials.length ? serials.join(', ') : String(value);
    } catch (_) {
        return String(value);
    }
}
function runMeta(run) {
    const parts = [];
    if (run.project) parts.push(run.project);
    if (run.gerrit_change_id) parts.push(run.gerrit_patchset ? `${run.gerrit_change_id} / PS${run.gerrit_patchset}` : run.gerrit_change_id);
    if (run.owner) parts.push(run.owner);
    return parts.join(' · ');
}
function openRunReport(event, runId) {
    event?.stopPropagation?.();
    const run = atsRuns.find(item => item.id === runId);
    if (!run?.report_timestamp) return;
    window.GmsEmbeddedWorkspace?.navigate('reports', {
        worker_id: run.worker_id || atsLocalWorkerId,
        cluster_job_id: run.cluster_job_id || '',
        attempt_id: run.attempt_id || '',
        automation_run_id: run.id,
        report_id: run.report_id || '',
        report_timestamp: run.report_timestamp,
        origin_page: 'automation',
    });
}
