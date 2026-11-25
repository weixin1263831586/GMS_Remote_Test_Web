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

async function collectRunPayload() {
    const artifact = currentFlashMode() === 'skip'
        ? '' : qs('automation-artifact').value.trim();
    const checkedDevices = Array.from(qs('automation-device-list').querySelectorAll('input[type="checkbox"]:checked'))
        .map(opt => opt.value)
        .filter(Boolean);
    const devices = [...new Set(checkedDevices)];
    if (currentFlashMode() !== 'skip' && devices.length > 1) {
        throw new Error('固件烧写只允许选择 1 台设备');
    }
    const testPlan = collectTestPlan();
    if (atsWorkspaceContext.redmine_issue_id) {
        testPlan.redmine_issue_id = atsWorkspaceContext.redmine_issue_id;
    }
    const buildServerPassword = testPlan.build
        ? await getBuildPassword(testPlan.build.server_id) : '';
    return {
        payload: {
            profile_id: qs('automation-profile').value,
            source_type: 'manual',
            artifact_path: artifact.startsWith('http') ? '' : artifact,
            artifact_url: artifact.startsWith('http') ? artifact : '',
            devices,
            test_plan: testPlan,
            build_server_password: buildServerPassword,
            gerrit_change_id: atsWorkspaceContext.gerrit_change_id || '',
            gerrit_patchset: atsWorkspaceContext.gerrit_patchset || '',
        },
        devices,
    };
}

function renderPreflightResult(data, state = 'ready', error = '') {
    const target = qs('automation-preflight');
    if (!target) return;
    target.className = `preflight-result ${state}`;
    if (state === 'loading') {
        target.innerHTML = '<strong>正在预检</strong><span>正在查询真实 Worker、设备库存、套件与构建配置…</span>';
        return;
    }
    if (state === 'error') {
        target.innerHTML = `<strong>预检未通过</strong><span>${esc(error || '资源或参数未就绪')}</span>`;
        return;
    }
    const buildText = data.artifact_configured
        ? '已有固件' : data.build_configured ? '自动编译' : '跳过固件';
    const devices = (data.devices || []).length
        ? (data.devices || []).map(item => typeof item === 'string' ? item : item.serial).join(', ')
        : `自动选择（当前可用 ${data.available_device_count ?? '-'} 台）`;
    target.innerHTML = `<strong>预检通过</strong><span>${esc([
        `Worker ${data.worker_id || '-'}`,
        buildText,
        data.flash_mode === 'skip' ? '不烧写' : '烧写并校验',
        `${data.test_type || '-'} / ${data.test_suite || '自动套件'}`,
        `设备 ${devices}`,
    ].join(' · '))}</span>`;
}

async function runPreflight() {
    renderPreflightResult(null, 'loading');
    try {
        const request = await collectRunPayload();
        const data = await api('/api/automation/runs/preflight', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(request.payload),
        });
        lastPreflightSignature = runFormSignature();
        lastPreflightData = data;
        renderPreflightResult(data);
        updateStepIndicators();
        return {...request, preflight: data};
    } catch (error) {
        lastPreflightSignature = '';
        lastPreflightData = null;
        renderPreflightResult(null, 'error', error.message);
        updateStepIndicators();
        throw error;
    }
}

async function preflightRunOnly() {
    const button = qs('automation-preflight-run');
    if (button) button.disabled = true;
    try {
        const {preflight} = await runPreflight();
        toast(`预检通过：${preflight.worker_id} / ${preflight.test_type}`);
    } catch (error) {
        toast(error.message);
    } finally {
        if (button) button.disabled = false;
    }
}

async function createRun() {
    const button = qs('automation-create-run');
    if (button?.dataset.busy === 'true') return;
    if (button) {
        button.dataset.busy = 'true';
        button.disabled = true;
        button.textContent = '正在预检…';
    }
    try {
        const {payload, devices, preflight} = await runPreflight();
        const flashText = preflight.flash_mode === 'skip'
            ? '跳过固件烧写' : '将锁定单台设备并执行固件烧写；刷机阶段不可取消';
        const message = [
            `Worker：${preflight.worker_id || '-'}`,
            `测试：${preflight.test_type || '-'} / ${preflight.test_suite || '自动套件'}`,
            `设备：${formatDevices(preflight.devices || devices) === '-' ? '按 Profile 自动选择' : formatDevices(preflight.devices || devices)}`,
            `固件：${preflight.artifact_configured ? '使用已有固件' : preflight.build_configured ? '自动编译' : '不使用固件'}`,
            flashText,
            `失败用例自动重试上限：${payload.test_plan.retry_policy?.max_retries || 0} 次（不重复构建或烧写）`,
            '',
            '确认创建持久化 ATS Run 并启动一条龙流程？',
        ].join('\n');
        const confirmed = typeof window.parent?.showConfirmDialog === 'function'
            ? await window.parent.showConfirmDialog('启动 GMS ATS 流水线', message)
            : await GmsDialogs.confirm('启动 GMS ATS 流水线', message);
        if (!confirmed) {
            toast('已取消启动，预检结果仍然有效');
            return;
        }
        if (button) button.textContent = '正在创建运行…';
        const run = await api('/api/automation/runs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        toast(`流水线已启动：${run.id} / ${preflight.worker_id} / ${preflight.test_type}`);
        syncAutomationWorkspaceSelection({
            automation_run_id: run.id,
            worker_id: preflight.worker_id,
            suite_path: preflight.test_suite,
            device_ids: preflight.devices || devices,
        });
        await loadRuns();
        await loadEvents(run.id);
    } catch (err) {
        toast(err.message);
    } finally {
        if (button) {
            button.dataset.busy = 'false';
            button.disabled = false;
            button.textContent = '预检并启动一条龙流程';
        }
    }
}
