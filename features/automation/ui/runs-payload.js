// Automation page runs-payload chunk; loaded by page.html in dependency order.

function switchMonitorPane(pane) {
    switchWorkflowPane(pane);
}

function switchWorkflowPane(pane, {persist = true, load = true} = {}) {
    const target = AUTOMATION_WORKFLOW_PANES.has(pane) ? pane : 'overview';
    activeWorkflowPane = target;
    AUTOMATION_WORKFLOW_PANES.forEach(key => {
        const el = qs(`workflow-pane-${key}`);
        if (el) {
            el.classList.toggle('active', key === target);
            el.setAttribute('aria-hidden', key === target ? 'false' : 'true');
        }
    });
    document.querySelectorAll('.workflow-tab').forEach(tab => {
        const active = tab.dataset.workflow === target;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
        tab.tabIndex = active ? 0 : -1;
    });
    if (persist) {
        try { window.sessionStorage.setItem(AUTOMATION_WORKFLOW_STORAGE_KEY, target); } catch (_error) {}
        const url = new URL(window.location.href);
        if (target === 'overview') url.searchParams.delete('tab');
        else url.searchParams.set('tab', target);
        window.history.replaceState({}, '', url.toString());
    }
    if (!load) return;
    if (target === 'overview') loadDashboard().catch(err => toast(err.message));
    if (target === 'runs' || target === 'reports') {
        loadRuns().catch(err => toast(err.message));
    }
    if (target === 'build') loadBuildJobs().catch(err => toast(err.message));
}

function collectTestPlan() {
    let extra = {};
    const raw = qs('automation-test-plan').value.trim();
    if (raw) extra = JSON.parse(raw);
    const profile = selectedProfile();
    const profilePlan = profile.test_plan || {};
    const plan = {
        ...profilePlan,
        flash: {
            ...(profile.flash || profilePlan.flash || {}),
            mode: currentFlashMode(),
        },
        device_selector: profile.device_selector || profilePlan.device_selector || {},
        reporting: profile.reporting || profilePlan.reporting || {},
        ...extra,
        test_type: qs('automation-test-type').value.trim(),
        test_suite: qs('automation-test-suite').value,
        test_module: qs('automation-test-module').value.trim(),
        worker_id: selectedWorkerId(),
    };
    const build = currentFlashMode() === 'skip' ? null : collectBuildPlan();
    if (build) plan.build = build;
    else delete plan.build;
    return plan;
}
