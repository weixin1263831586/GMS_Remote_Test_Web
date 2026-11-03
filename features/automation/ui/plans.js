// Test plan drafts stay in memory; only explicit user actions save or dispatch.
function syncTestPlanSchedule() {
    const kind = qs('test-plan-kind').value;
    qs('test-plan-interval-field').hidden = kind !== 'interval';
    qs('test-plan-time-field').hidden = !['daily', 'weekly'].includes(kind);
    qs('test-plan-weekdays').hidden = kind !== 'weekly';
}

function renderTestPlanDraft() {
    qs('test-plan-draft').innerHTML = atsPlanDraft.map((step, index) => `
        <li><span>${esc([step.test_plan?.test_type, step.test_plan?.test_module || '完整套件', step.test_plan?.worker_id].filter(Boolean).join(' · '))}</span>
        <span class="plan-step-actions"><button type="button" aria-label="上移步骤 ${index + 1}" data-click="moveTestPlanStep" data-a0="${index}" data-a1="-1" ${index === 0 ? 'disabled' : ''}>上移</button><button type="button" aria-label="下移步骤 ${index + 1}" data-click="moveTestPlanStep" data-a0="${index}" data-a1="1" ${index === atsPlanDraft.length - 1 ? 'disabled' : ''}>下移</button><button type="button" data-click="removeTestPlanStep" data-a0="${index}">移除</button></span></li>`).join('');
    qs('test-plan-draft-description').textContent = atsEditingPlan && !atsPlanDraft.length
        ? `正在编辑「${atsEditingPlan.name}」的调度；保留已保存的 ${atsEditingPlan.step_count} 个步骤。添加草稿步骤后保存会替换完整步骤列表。`
        : `草稿共 ${atsPlanDraft.length} 步，按顺序执行。草稿仅保存在当前页面，刷新会清空。`;
}

async function addCurrentRunToPlan() {
    try {
        if (atsPlanDraft.length >= 20) throw new Error('每个计划最多 20 个步骤');
        const {payload} = await collectRunPayload();
        if (!payload.test_plan.test_suite) throw new Error('请先选择测试套件');
        atsPlanDraft.push(payload);
        renderTestPlanDraft();
        switchWorkflowPane('plans');
        toast(`已加入第 ${atsPlanDraft.length} 步；尚未保存或启动`);
    } catch (error) { toast(error.message); }
}

function removeTestPlanStep(index) {
    atsPlanDraft.splice(index, 1);
    renderTestPlanDraft();
}

function moveTestPlanStep(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= atsPlanDraft.length) return;
    [atsPlanDraft[index], atsPlanDraft[target]] = [atsPlanDraft[target], atsPlanDraft[index]];
    renderTestPlanDraft();
}

function clearTestPlanDraft() {
    atsPlanDraft = [];
    atsEditingPlan = null;
    qs('test-plan-form').reset();
    syncTestPlanSchedule();
    renderTestPlanDraft();
}

function editTestPlanSettings(planId) {
    const plan = atsTestPlans.find(item => item.id === planId);
    if (!plan) return;
    atsEditingPlan = plan;
    atsPlanDraft = [];
    qs('test-plan-name').value = plan.name;
    qs('test-plan-labels').value = (plan.labels || []).join(', ');
    qs('test-plan-kind').value = plan.schedule.kind;
    qs('test-plan-timezone').value = plan.schedule.timezone;
    qs('test-plan-time').value = plan.schedule.time;
    qs('test-plan-interval').value = plan.schedule.interval_minutes;
    qs('test-plan-enabled').checked = Boolean(plan.enabled);
    qs('test-plan-stop-on-failure').checked = Boolean(plan.stop_on_failure);
    qs('test-plan-weekdays').querySelectorAll('input').forEach(input => {
        input.checked = plan.schedule.weekdays.includes(Number(input.value));
    });
    syncTestPlanSchedule();
    renderTestPlanDraft();
    qs('test-plan-name').focus();
}

function testPlanScheduleLabel(schedule) {
    if (schedule.kind === 'manual') return '手动';
    if (schedule.kind === 'interval') return `每 ${schedule.interval_minutes} 分钟`;
    const days = schedule.kind === 'weekly' ? `周 ${(schedule.weekdays || []).map(day => '一二三四五六日'[day]).join('、')}` : '每日';
    return `${days} ${schedule.time} (${schedule.timezone})`;
}

function testPlanLocalTime(value, zone) {
    if (!value) return '-';
    try { return new Date(value).toLocaleString('zh-CN', {timeZone: zone, hour12: false}); }
    catch (_) { return compactTime(value) + ' UTC'; }
}

async function loadTestPlans() {
    const request = ++atsPlanRequest;
    const label = qs('test-plan-label-filter')?.value.trim() || '';
    const data = await api('/api/automation/plans' + (label ? `?label=${encodeURIComponent(label)}` : ''));
    if (request !== atsPlanRequest) return;
    atsTestPlans = data.items || [];
    qs('test-plan-list').innerHTML = atsTestPlans.length ? atsTestPlans.map(plan => `
        <article class="run-card"><div class="run-main"><div class="run-title"><input type="checkbox" class="test-plan-selection" value="${esc(plan.id)}" aria-label="选择计划 ${esc(plan.name)}"><strong>${esc(plan.name)}</strong><span class="badge ${plan.enabled ? 'completed' : 'cancelled'}">${plan.enabled ? '已启用' : '已暂停'}</span>${(plan.labels || []).map(label => `<span class="test-outcome unknown">${esc(label)}</span>`).join('')}</div>
        <div class="run-value">${esc(testPlanScheduleLabel(plan.schedule))} · ${esc(plan.step_count)} 步 · ${plan.stop_on_failure ? '失败停止' : '失败继续'}</div>
        <div class="muted">下次：${esc(testPlanLocalTime(plan.next_run_at, plan.schedule.timezone))} · 上次：${esc(testPlanLocalTime(plan.last_run_at, plan.schedule.timezone))} (${esc(plan.schedule.timezone)})</div>
        ${plan.active_batch_id ? `<div class="muted">当前批次：${esc(plan.active_batch_id)}</div>` : ''}
        ${plan.last_error ? `<div class="error">${esc(plan.last_error)}</div>` : ''}</div>
        <div class="run-actions"><button type="button" data-click="editTestPlanSettings" data-a0="${esc(plan.id)}">编辑调度</button><button type="button" data-click="exportTestPlan" data-a0="${esc(plan.id)}">导出模板</button><button type="button" data-click="toggleTestPlan" data-a0="${esc(plan.id)}">${plan.enabled ? '暂停调度' : '启用调度'}</button><button type="button" data-click="runTestPlan" data-a0="${esc(plan.id)}" ${plan.active_batch_id ? 'disabled' : ''}>执行一次</button><button type="button" data-click="showTestPlanBatches" data-a0="${esc(plan.id)}">执行历史</button>${plan.active_batch_id ? `<button type="button" data-click="stopTestPlanBatch" data-a0="${esc(plan.id)}" title="停止当前批次后续步骤和自动重试；不取消已运行测试">停止后续</button>` : ''}</div></article>`).join('') : '<div class="empty-state">暂无测试计划。先在“创建运行”中添加测试步骤。</div>';
}

async function confirmTestPlanAction(title, message) {
    return typeof window.parent?.showConfirmDialog === 'function'
        ? window.parent.showConfirmDialog(title, message) : window.confirm(message);
}

async function saveTestPlan() {
    const button = qs('test-plan-save');
    if (button.disabled) return;
    button.disabled = true;
    try {
        if (!atsPlanDraft.length && !atsEditingPlan) throw new Error('请先添加测试步骤');
        const body = {
            name: qs('test-plan-name').value.trim(), enabled: qs('test-plan-enabled').checked,
            labels: qs('test-plan-labels').value.split(',').map(value => value.trim()).filter(Boolean),
            stop_on_failure: qs('test-plan-stop-on-failure').checked,
            schedule: {
                kind: qs('test-plan-kind').value, timezone: qs('test-plan-timezone').value.trim(),
                time: qs('test-plan-time').value, interval_minutes: Number(qs('test-plan-interval').value),
                weekdays: Array.from(qs('test-plan-weekdays').querySelectorAll('input:checked')).map(input => Number(input.value)),
            }, version: atsEditingPlan?.version || 0,
        };
        if (atsPlanDraft.length) body.runs = atsPlanDraft;
        if (!await confirmTestPlanAction('保存测试计划', `计划：${body.name}\n步骤：${atsPlanDraft.length || atsEditingPlan.step_count}\n调度：${testPlanScheduleLabel(body.schedule)}\n${body.enabled ? '启用后将按计划自动执行已配置的构建、烧写和测试。' : '保存后保持暂停，执行一次需点击计划按钮。'}`)) return;
        const suffix = atsEditingPlan ? `/${encodeURIComponent(atsEditingPlan.id)}${body.runs ? '' : '/settings'}` : '';
        await api('/api/automation/plans' + suffix, {method: atsEditingPlan ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        clearTestPlanDraft();
        await loadTestPlans();
        toast('测试计划已保存');
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
}

async function toggleTestPlan(planId) {
    const plan = atsTestPlans.find(item => item.id === planId);
    if (!plan) return;
    try {
        if (!plan.enabled && !await confirmTestPlanAction('启用自动调度', `将按「${testPlanScheduleLabel(plan.schedule)}」执行此计划的 ${plan.step_count} 个步骤，包含已配置的构建、烧写和测试。`)) return;
        await api(`/api/automation/plans/${encodeURIComponent(planId)}/enabled`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled: !plan.enabled, version: plan.version})});
        await loadTestPlans();
    } catch (error) { toast(error.message); }
}

async function runTestPlan(planId) {
    try {
        if (!await confirmTestPlanAction('执行测试计划', '将依次执行计划的所有步骤，包含已配置的构建、烧写和测试。是否继续？')) return;
        await api(`/api/automation/plans/${encodeURIComponent(planId)}/run`, {method: 'POST'});
        await loadTestPlans();
        toast('已创建计划批次，后台 Worker 将顺序执行');
    } catch (error) { toast(error.message); }
}

async function stopTestPlanBatch(planId) {
    try {
        await api(`/api/automation/plans/${encodeURIComponent(planId)}/stop`, {method: 'POST'});
        await loadTestPlans();
        toast('已停止后续步骤和自动重试；当前已运行测试仍继续，定时调度可单独暂停');
    } catch (error) { toast(error.message); }
}

async function showTestPlanBatches(planId) {
    try {
        const data = await api(`/api/automation/plans/${encodeURIComponent(planId)}/batches`);
        qs('test-plan-batches').innerHTML = (data.items || []).map(batch => `<article class="run-card"><div class="run-main"><strong>${esc(batch.id)}</strong><div class="muted">${esc(statusLabel(batch.status))} · 已派发 ${esc(batch.next_step)} / ${esc(batch.step_count)} 步 · ${esc(compactTime(batch.created_at))}</div></div><button type="button" data-click="loadEvents" data-a0="${esc(batch.last_run_id)}">最近运行日志</button></article>`).join('') || '<div class="empty-state">暂无执行历史</div>';
    } catch (error) { toast(error.message); }
}

async function exportTestPlan(planId) {
    try {
        const data = await api(`/api/automation/plans/${encodeURIComponent(planId)}/export`);
        downloadText(`${planId}.json`, JSON.stringify(data, null, 2));
        toast('已导出无凭据模板；导入后需要重新确认本地资源配置');
    } catch (error) { toast(error.message); }
}

async function importTestPlan() {
    try {
        const body = JSON.parse(qs('test-plan-import').value);
        await api('/api/automation/plans/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        qs('test-plan-import').value = '';
        await loadTestPlans();
        toast('已导入为暂停的新计划，请核对资源和调度后启用');
    } catch (error) { toast(error.message); }
}

async function bulkTestPlanChanges(changes) {
    try {
        const selected = new Set(Array.from(document.querySelectorAll('.test-plan-selection:checked')).map(input => input.value));
        const selections = atsTestPlans.filter(plan => selected.has(plan.id)).map(plan => ({id: plan.id, version: plan.version}));
        if (!selections.length) throw new Error('请先选择计划');
        if (!await confirmTestPlanAction('批量修改计划', `将修改选中的 ${selections.length} 个计划。启用的计划会按修改后的调度执行，当前批次保留原配置。是否继续？`)) return;
        await api('/api/automation/plans/bulk-settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({selections, changes})});
        await loadTestPlans();
        toast('批量修改已完成');
    } catch (error) { toast(error.message); }
}

function bulkPauseTestPlans() { return bulkTestPlanChanges({enabled: false}); }
function bulkScheduleTestPlans() {
    return bulkTestPlanChanges({schedule: {kind: qs('test-plan-kind').value, timezone: qs('test-plan-timezone').value.trim(), time: qs('test-plan-time').value, interval_minutes: Number(qs('test-plan-interval').value), weekdays: Array.from(qs('test-plan-weekdays').querySelectorAll('input:checked')).map(input => Number(input.value))}, labels: qs('test-plan-labels').value.split(',').map(value => value.trim()).filter(Boolean)});
}
