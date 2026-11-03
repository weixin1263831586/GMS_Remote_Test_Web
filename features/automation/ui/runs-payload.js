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
    if (target === 'plans') loadTestPlans().catch(err => toast(err.message));
    if (target === 'resources') loadResourceCatalog().catch(err => toast(err.message));
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
        shard_count: Number(qs('automation-shard-count').value),
        queue_timeout_seconds: Number(qs('automation-queue-timeout').value),
        execution_timeout_seconds: Number(qs('automation-execution-timeout').value),
        output_idle_timeout_seconds: Number(qs('automation-idle-timeout').value),
        preparations: [...atsPreparationOrder],
        retry_policy: {
            max_retries: Number(qs('automation-max-retries').value),
            delay_seconds: Number(qs('automation-retry-delay').value),
        },
    };
    const build = currentFlashMode() === 'skip' ? null : collectBuildPlan();
    plan.device_selector = {...plan.device_selector, min_count: Number(qs('automation-selector-count').value)};
    for (const key of ['product', 'variant', 'build_id', 'android_version', 'board', 'serial_prefix']) {
        plan.device_selector[key] = qs(`automation-selector-${key}`).value.trim();
    }
    if (build) plan.build = build;
    else delete plan.build;
    return plan;
}

function changePreparation(input) {
    atsPreparationOrder = atsPreparationOrder.filter(action => action !== input.value);
    if (input.checked) atsPreparationOrder.push(input.value);
    renderPreparationOrder();
    invalidateRunPreflight();
}

function movePreparation(index, delta) {
    const next = index + delta;
    if (next < 0 || next >= atsPreparationOrder.length) return;
    [atsPreparationOrder[index], atsPreparationOrder[next]] = [atsPreparationOrder[next], atsPreparationOrder[index]];
    renderPreparationOrder();
    invalidateRunPreflight();
}

function renderPreparationOrder() {
    const labels = {stay_awake: '充电时常亮', animations_off: '关闭系统动画', wifi_on: '启用 Wi-Fi'};
    qs('automation-preparation-order').innerHTML = atsPreparationOrder.map((action, index) => `<div>${index + 1}. ${esc(labels[action] || action)} <button type="button" data-click="movePreparation" data-a0="${index}" data-a1="-1" ${index ? '' : 'disabled'}>上移</button><button type="button" data-click="movePreparation" data-a0="${index}" data-a1="1" ${index === atsPreparationOrder.length - 1 ? 'disabled' : ''}>下移</button></div>`).join('');
}
