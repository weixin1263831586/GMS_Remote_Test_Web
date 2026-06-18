let atsProfiles = [];
let atsRuns = [];
let atsStatus = '';

function qs(id) { return document.getElementById(id); }
function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function toast(message) { qs('automation-toast').textContent = message; }
async function api(path, options) {
    const resp = await fetch(path, options);
    const data = await resp.json();
    if (!data.success) throw new Error(data.error || data.message || '请求失败');
    return data.data;
}

async function loadProfiles() {
    const data = await api('/api/automation/profiles');
    atsProfiles = data.items || [];
    const select = qs('automation-profile');
    select.innerHTML = atsProfiles.map(p => `<option value="${esc(p.id)}">${esc(p.name || p.id)}</option>`).join('');
    qs('automation-profiles').innerHTML = atsProfiles.length
        ? atsProfiles.map(p => `<div><span class="badge">${esc(p.enabled ? 'enabled' : 'disabled')}</span> <strong>${esc(p.name || p.id)}</strong><div class="muted">${esc(p.id)} / ${esc((p.jenkins || {}).job || 'manual')}</div></div>`).join('')
        : '<div class="muted">未配置 automation_profiles.json，当前使用 example 配置。</div>';
}

async function loadRuns() {
    const query = atsStatus ? `?status=${encodeURIComponent(atsStatus)}&limit=100` : '?limit=100';
    const data = await api('/api/automation/runs' + query);
    atsRuns = data.items || [];
    const body = qs('automation-runs').querySelector('tbody');
    if (!atsRuns.length) {
        body.innerHTML = '<tr><td colspan="9" class="muted">暂无运行记录。</td></tr>';
        return;
    }
    body.innerHTML = atsRuns.map(run => `
        <tr class="run-row" onclick="loadEvents('${esc(run.id)}')">
            <td><span class="badge ${esc(run.status)}">${esc(run.status)}</span></td>
            <td>${esc(run.profile_id)}</td>
            <td>${esc(run.source_type || 'manual')}<div class="muted">${esc(run.owner || '')}</div></td>
            <td>${esc(run.project || '')}<div class="muted">${esc(run.gerrit_change_id || '')}${run.gerrit_patchset ? ' / PS' + esc(run.gerrit_patchset) : ''}</div></td>
            <td>${esc(run.artifact_path || run.artifact_url || '')}</td>
            <td>${esc(run.devices_json || '')}</td>
            <td>${run.report_url ? `<a href="${esc(run.report_url)}" target="_blank">报告</a>` : '<span class="muted">-</span>'}</td>
            <td class="nowrap">${esc(run.updated_at || run.created_at || '')}</td>
            <td class="nowrap">
                <button type="button" onclick="event.stopPropagation(); retryRun('${esc(run.id)}')">重试</button>
                <button type="button" class="danger" onclick="event.stopPropagation(); cancelRun('${esc(run.id)}')">取消</button>
            </td>
        </tr>
    `).join('');
}

async function loadEvents(runId) {
    const data = await api(`/api/automation/runs/${encodeURIComponent(runId)}/events`);
    const items = data.items || [];
    qs('automation-events').innerHTML = items.length
        ? items.map(ev => `<div class="event"><div class="event-meta">${esc(ev.created_at)} / ${esc(ev.stage)} / ${esc(ev.level)}</div><div>${esc(ev.message)}</div></div>`).join('')
        : '<div class="muted">暂无事件。</div>';
}

function collectTestPlan() {
    let extra = {};
    const raw = qs('automation-test-plan').value.trim();
    if (raw) extra = JSON.parse(raw);
    return {
        ...extra,
        test_type: qs('automation-test-type').value.trim(),
        test_suite: qs('automation-test-suite').value.trim(),
        test_module: qs('automation-test-module').value.trim(),
    };
}

async function createRun() {
    try {
        const artifact = qs('automation-artifact').value.trim();
        const devices = qs('automation-devices').value.split(',').map(v => v.trim()).filter(Boolean);
        const payload = {
            profile_id: qs('automation-profile').value,
            source_type: 'manual',
            artifact_path: artifact.startsWith('http') ? '' : artifact,
            artifact_url: artifact.startsWith('http') ? artifact : '',
            devices,
            test_plan: collectTestPlan(),
        };
        const run = await api('/api/automation/runs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        toast(`已创建 ${run.id}`);
        await loadRuns();
        await loadEvents(run.id);
    } catch (err) {
        toast(err.message);
    }
}

async function pollGerrit() {
    try {
        const data = await api('/api/automation/gerrit/poll', {method: 'POST'});
        toast(`Gerrit poll 创建 ${data.created_count || 0} 条，已存在 ${data.existing_count || 0} 条`);
        await loadRuns();
    } catch (err) { toast(err.message); }
}

async function tickWorker(executor) {
    try {
        const run = await api(`/api/automation/worker/tick?executor=${encodeURIComponent(executor)}`, {method: 'POST'});
        toast(run ? `推进到 ${run.status}: ${run.id}` : '没有可推进的运行');
        await loadRuns();
        if (run && run.id) await loadEvents(run.id);
    } catch (err) { toast(err.message); }
}

async function retryRun(runId) {
    try {
        const run = await api(`/api/automation/runs/${encodeURIComponent(runId)}/retry`, {method: 'POST'});
        toast(`已创建重试 ${run.id}`);
        await loadRuns();
    } catch (err) { toast(err.message); }
}

async function cancelRun(runId) {
    try {
        const run = await api(`/api/automation/runs/${encodeURIComponent(runId)}/cancel`, {method: 'POST'});
        toast(`已取消 ${run.id}`);
        await loadRuns();
        await loadEvents(run.id);
    } catch (err) { toast(err.message); }
}

function setStatusFilter(status) {
    atsStatus = status;
    document.querySelectorAll('.tab').forEach(btn => btn.classList.toggle('active', btn.dataset.status === status));
    loadRuns().catch(err => toast(err.message));
}

async function loadAll() {
    try {
        await loadProfiles();
        await loadRuns();
        toast('已刷新');
    } catch (err) { toast(err.message); }
}

document.addEventListener('DOMContentLoaded', loadAll);
