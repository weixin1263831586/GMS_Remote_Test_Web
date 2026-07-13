let atsProfiles = [];
let atsRuns = [];
let atsStatus = '';
let selectedRunId = '';
let buildServers = [];
let buildTemplates = [];
let buildJobs = [];
let buildPasswordCache = {};
let testSuites = [];
let connectedDevices = [];
let selectedBuildJobId = '';
let activeWorkflowPane = 'overview';
let toastTimer = null;

function selectedWorkerId() {
    return qs('automation-worker')?.value || 'worker-local';
}

async function loadClusterWorkers() {
    const select = qs('automation-worker');
    if (!select) return;
    const previous = select.value || 'worker-local';
    try {
        const statusResponse = await fetch('/api/cluster/status', {cache: 'no-store'});
        const status = await statusResponse.json();
        if (!statusResponse.ok || !status.enabled) {
            select.innerHTML = '<option value="worker-local">Controller / Local Worker</option>';
            if (select.closest('label')) select.closest('label').style.display = 'none';
            return;
        }
        if (select.closest('label')) select.closest('label').style.display = '';
        const response = await fetch('/api/cluster/hosts', {cache: 'no-store'});
        const payload = await response.json();
        const hosts = payload.hosts || [];
        select.innerHTML = hosts.map(host => `<option value="${esc(host.worker_id)}"${host.status === 'offline' ? ' disabled' : ''}>${esc(host.name || host.worker_id)}${host.status === 'offline' ? '（离线）' : ''}</option>`).join('');
        if (hosts.some(host => host.worker_id === previous && host.status !== 'offline')) select.value = previous;
    } catch (_) {
        select.innerHTML = '<option value="worker-local">Controller / Local Worker</option>';
    }
}

// 13 段流水线阶段（顺序即推进顺序）
const PIPELINE_STAGES = [
    'queued', 'jenkins_queued', 'jenkins_building', 'artifact_ready',
    'waiting_device', 'device_locked', 'flashing', 'flash_verified',
    'testing', 'report_collecting', 'analyzing', 'reporting', 'completed',
];
const STAGE_LABELS_ZH = {
    queued: '排队', jenkins_queued: '构建排队', jenkins_building: '编译中',
    artifact_ready: '固件就绪', waiting_device: '等待设备', device_locked: '设备已锁',
    flashing: '刷机中', flash_verified: '刷机校验', testing: '测试中',
    report_collecting: '收集报告', analyzing: '分析中', reporting: '上报中', completed: '完成',
};
const TERMINAL_STATUSES = new Set([
    'completed', 'cancelled', 'failed', 'jenkins_failed', 'artifact_missing',
    'flash_failed', 'test_failed', 'analysis_failed', 'reporting_failed',
]);
const FAILURE_STATUSES = new Set([
    'failed', 'jenkins_failed', 'artifact_missing', 'flash_failed',
    'test_failed', 'analysis_failed', 'reporting_failed',
]);
// 失败状态 → 对应失败阶段的索引（用于进度条标红定位）
const FAILURE_STAGE_INDEX = {
    jenkins_failed: 2, artifact_missing: 3, flash_failed: 6,
    test_failed: 8, analysis_failed: 10, reporting_failed: 11,
};

function qs(id) { return document.getElementById(id); }
function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
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
function renderStageBar(status, currentStage) {
    // 单索引模型：cursor 是“当前/失败”段的下标；它之前的段全部 done。
    // completed 时 cursor 越界 → 全 done；失败时 cursor 落在失败段并标红。
    let cursor = Math.max(0, PIPELINE_STAGES.indexOf(currentStage || status));
    const isCompleted = status === 'completed';
    const isFailed = FAILURE_STATUSES.has(status);
    if (isCompleted) cursor = PIPELINE_STAGES.length;
    else if (isFailed) cursor = FAILURE_STAGE_INDEX[status] ?? cursor;
    return `<div class="run-stage-bar">${PIPELINE_STAGES.map((stage, idx) => {
        let cls = '';
        if (idx < cursor) cls = 'done';
        else if (idx === cursor) cls = isFailed ? 'failed' : (isCompleted ? '' : 'current');
        const label = STAGE_LABELS_ZH[stage] || stage;
        return `<span class="stage-cell ${cls}" title="${esc(label)}（${esc(stage)}）"></span>`;
    }).join('')}</div>`;
}
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
    select.onchange = applySelectedProfile;
    qs('automation-profiles').innerHTML = atsProfiles.length
        ? atsProfiles.map(p => `<div><span class="badge">${esc(p.enabled ? 'enabled' : 'disabled')}</span> <strong>${esc(p.name || p.id)}</strong><div class="muted">${esc(p.id)} / ${esc((p.jenkins || {}).job || 'manual')}</div></div>`).join('')
        : '<div class="muted">未配置 automation_profiles.json，当前使用 example 配置。</div>';
    applySelectedProfile();
}

function selectedProfile() {
    return atsProfiles.find(profile => profile.id === qs('automation-profile').value) || {};
}

function setSelectValue(id, value) {
    const select = qs(id);
    if (!select || value === undefined || value === null || value === '') return;
    const text = String(value);
    if (!Array.from(select.options).some(option => option.value === text)) {
        select.insertAdjacentHTML('beforeend', `<option value="${esc(text)}">${esc(text)}</option>`);
    }
    select.value = text;
}

function applySelectedProfile() {
    const profile = selectedProfile();
    const build = profile.build || {};
    const parameters = build.parameters || {};
    qs('automation-enable-build').checked = Boolean(build.server_id || build.template_id || build.provider);
    setSelectValue('build-server', build.server_id);
    setSelectValue('build-template', build.template_id);
    setSelectValue('build-workspace', parameters.workspace);
    setSelectValue('build-lunch-target', parameters.lunch_target);
    setSelectValue('build-command', parameters.build_command);

    const testPlan = profile.test_plan || {};
    setSelectValue('automation-test-type', testPlan.test_type);
    renderSuiteOptions();
    setSelectValue('automation-test-suite', testPlan.test_suite);
    qs('automation-test-module').value = testPlan.test_module || (testPlan.modules || [])[0] || '';
}

async function loadBuildConfig() {
    const servers = await api('/api/build/servers');
    const templates = await api('/api/build/templates?enabled_only=true');
    buildServers = servers.items || [];
    buildTemplates = templates.items || [];
    qs('build-server').innerHTML = buildServers.map(s => `<option value="${esc(s.id)}">${esc(s.name || s.id)}</option>`).join('');
    qs('build-template').innerHTML = buildTemplates.map(t => `<option value="${esc(t.id)}">${esc(t.name || t.id)}</option>`).join('');
    qs('build-server').onchange = () => {
        renderBuildWorkspaces([]);
        renderLunchOptions([]);
    };
    qs('build-workspace').onchange = () => renderLunchOptions([]);
}

function collectBuildPlan() {
    if (!qs('automation-enable-build').checked) return null;
    const serverId = qs('build-server').value;
    const templateId = qs('build-template').value;
    const workspace = qs('build-workspace').value;
    const lunchTarget = qs('build-lunch-target').value;
    const buildCommand = qs('build-command').value;
    if (!serverId || !templateId || !workspace || !lunchTarget) return null;
    return {
        provider: 'ssh',
        server_id: serverId,
        template_id: templateId,
        parameters: {
            workspace,
            lunch_target: lunchTarget,
            build_command: buildCommand || './build.sh -UCKApu -J 8',
        },
    };
}

function promptBuildPassword() {
    return new Promise(resolve => {
        const backdrop = document.createElement('div');
        backdrop.className = 'password-backdrop';
        backdrop.innerHTML = `
            <div class="password-dialog">
                <div class="password-title">编译服务器 SSH 密码</div>
                <input id="build-password-input" type="password" autocomplete="current-password" placeholder="请输入密码">
                <div class="password-actions">
                    <button type="button" id="build-password-cancel">取消</button>
                    <button type="button" class="primary" id="build-password-ok">确认</button>
                </div>
            </div>
        `;
        document.body.appendChild(backdrop);
        const input = backdrop.querySelector('#build-password-input');
        const finish = value => {
            backdrop.remove();
            resolve(value || '');
        };
        backdrop.querySelector('#build-password-cancel').onclick = () => finish('');
        backdrop.querySelector('#build-password-ok').onclick = () => finish(input.value);
        input.addEventListener('keydown', event => {
            if (event.key === 'Enter') finish(input.value);
            if (event.key === 'Escape') finish('');
        });
        input.focus();
    });
}

async function getBuildPassword(serverId) {
    if (buildPasswordCache[serverId]) return buildPasswordCache[serverId];
    const password = await promptBuildPassword();
    if (password) buildPasswordCache[serverId] = password;
    return password;
}

function selectedBuildServer() {
    return buildServers.find(s => s.id === qs('build-server').value) || {};
}

function renderBuildWorkspaces(items) {
    const server = selectedBuildServer();
    const root = String(server.workspace_root || '').replace(/\/$/, '');
    const options = (items || []).map(name => {
        const value = name.startsWith('/') ? name : `${root}/${name}`;
        return `<option value="${esc(value)}">${esc(name)}</option>`;
    });
    qs('build-workspace').innerHTML = options.length ? options.join('') : '<option value="">请选择 SDK 目录</option>';
}

function renderLunchOptions(items) {
    qs('build-lunch-target').innerHTML = (items || []).length
        ? items.map(item => `<option value="${esc(item)}">${esc(item)}</option>`).join('')
        : '<option value="">请选择 lunch target</option>';
}

async function refreshBuildWorkspaces() {
    try {
        const serverId = qs('build-server').value;
        const password = await getBuildPassword(serverId);
        const data = await api('/api/build/discover/workspaces', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({server_id: serverId, server_password: password}),
        });
        renderBuildWorkspaces(data.items || []);
        renderLunchOptions([]);
        toast(`发现 ${(data.items || []).length} 个 SDK 目录`);
    } catch (err) { toast(err.message); }
}

async function refreshLunchOptions() {
    try {
        const serverId = qs('build-server').value;
        const workspace = qs('build-workspace').value;
        if (!workspace) throw new Error('请先选择 SDK 目录');
        const password = await getBuildPassword(serverId);
        const data = await api('/api/build/discover/lunch-options', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({server_id: serverId, workspace, server_password: password}),
        });
        const items = data.items || [];
        renderLunchOptions(items);
        toast(`已从 ${workspace} 读取 ${items.length} 个 lunch 选项`);
    } catch (err) { toast(err.message); }
}

async function loadDevices(forceRefresh = false) {
    try {
        const workerId = selectedWorkerId();
        const endpoint = workerId === 'worker-local'
            ? `/api/devices/list?force_refresh=${forceRefresh ? '1' : '0'}`
            : `/api/cluster/devices?worker_id=${encodeURIComponent(workerId)}`;
        const resp = await fetch(endpoint, {cache: 'no-store'});
        const payload = await resp.json();
        connectedDevices = workerId === 'worker-local' ? payload : (payload.devices || []);
        const items = Array.isArray(connectedDevices) ? connectedDevices : [];
        qs('automation-device-list').innerHTML = items.length
            ? items.map(d => {
                const id = d.id || d.device_id || d.serial || d.serial_no || '';
                const label = `${id}${d.locked ? ' (locked)' : ''}`;
                return `<label class="checkbox-item"><input type="checkbox" value="${esc(id)}"> <span>${esc(label)}</span></label>`;
            }).join('')
            : '<div class="muted">未发现设备</div>';
    } catch (err) { toast(err.message); }
}

async function loadTestSuitesForAutomation() {
    try {
        const workerId = selectedWorkerId();
        const endpoint = workerId === 'worker-local' ? '/api/test/suites'
            : `/api/cluster/suites?worker_id=${encodeURIComponent(workerId)}`;
        const resp = await fetch(endpoint, {cache: 'no-store'});
        const data = await resp.json();
        testSuites = data.suites || data.data?.suites || [];
        testSuites = testSuites.map(suite => ({...suite,
            test_type: suite.test_type || suite.suite_type,
            full_path: suite.full_path || suite.tools_path}));
        const types = [...new Set(testSuites.map(s => String(s.test_type || '').toUpperCase()).filter(Boolean))].sort();
        qs('automation-test-type').innerHTML = types.length
            ? types.map(type => `<option value="${esc(type)}">${esc(type)}</option>`).join('')
            : ['CTS', 'GTS', 'VTS', 'STS'].map(type => `<option value="${type}">${type}</option>`).join('');
        renderSuiteOptions();
    } catch (err) { toast(err.message); }
}

function renderSuiteOptions() {
    const type = String(qs('automation-test-type').value || '').toLowerCase();
    const suites = testSuites.filter(s => String(s.test_type || '').toLowerCase() === type);
    qs('automation-test-suite').innerHTML = suites.length
        ? suites.map(s => `<option value="${esc(s.tools_path || '')}">${esc(s.version || s.suite_version || s.tools_path || '')}</option>`).join('')
        : '<option value="">自动匹配最新套件</option>';
}

async function loadRuns() {
    const query = atsStatus ? `?status=${encodeURIComponent(atsStatus)}&limit=100` : '?limit=100';
    const data = await api('/api/automation/runs' + query);
    atsRuns = data.items || [];
    const list = qs('automation-runs');
    const reportList = qs('automation-runs-report');
    if (!atsRuns.length) {
        list.innerHTML = '<div class="muted">暂无运行记录。</div>';
        if (reportList) reportList.innerHTML = '<div class="muted">暂无运行记录。</div>';
        return;
    }
    const html = atsRuns.map(run => `
        <article class="run-card${run.id === selectedRunId ? ' active' : ''}" onclick="loadEvents('${esc(run.id)}')">
            <div class="run-main">
                <div class="run-title">
                    <span class="badge ${esc(run.status)}">${esc(run.status)}</span>
                    <strong>${esc(run.profile_id || 'manual')}</strong>
                    <span class="muted">${esc(run.source_type || 'manual')}</span>
                </div>
                ${runMeta(run) ? `<div class="muted">${esc(runMeta(run))}</div>` : ''}
                <div class="run-detail-grid">
                    <div>
                        <div class="field-label">固件</div>
                        <div class="run-value">${esc(run.artifact_path || run.artifact_url || '-')}</div>
                    </div>
                    <div>
                        <div class="field-label">设备</div>
                        <div class="run-value">${esc(formatDevices(run.devices_json))}</div>
                    </div>
                    <div>
                        <div class="field-label">更新时间</div>
                        <div class="run-value nowrap">${esc(compactTime(run.updated_at || run.created_at))}</div>
                    </div>
                    <div>
                        <div class="field-label">报告</div>
                        <div class="run-value">${run.report_url ? `<a href="${esc(run.report_url)}" target="_blank" onclick="event.stopPropagation()">查看报告</a>` : '<span class="muted">-</span>'}</div>
                    </div>
                    ${(FAILURE_STATUSES.has(run.status) && run.error) ? `
                    <div style="grid-column: 1 / -1">
                        <div class="field-label">错误原因</div>
                        <div class="run-value error">${esc(run.error)}</div>
                    </div>` : ''}
                </div>
                ${renderStageBar(run.status, run.current_stage)}
            </div>
            <div class="run-actions">
                <button type="button" onclick="event.stopPropagation(); loadTrace('${esc(run.id)}')">链路</button>
                <button type="button" onclick="event.stopPropagation(); retryRun('${esc(run.id)}')">重试</button>
                <button type="button" class="danger" onclick="event.stopPropagation(); cancelRun('${esc(run.id)}')">取消</button>
            </div>
        </article>
    `).join('');
    list.innerHTML = html;
    if (reportList) reportList.innerHTML = html;
}

async function loadEvents(runId) {
    selectedRunId = runId;
    const run = atsRuns.find(r => r.id === runId);
    qs('events-title').textContent = run ? `事件 / ${run.profile_id || run.id}` : '事件';
    qs('automation-runs').querySelectorAll('.run-card').forEach(el => el.classList.remove('active'));
    const card = qs('automation-runs').querySelector(`.run-card[onclick="loadEvents('${runId}')"]`);
    if (card) card.classList.add('active');
    const data = await api(`/api/automation/runs/${encodeURIComponent(runId)}/events`);
    const items = data.items || [];
    qs('automation-events').innerHTML = items.length
        ? items.map(ev => `<div class="event"><div class="event-meta">${esc(ev.created_at)} / ${esc(ev.stage)} / ${esc(ev.level)}</div><div>${esc(ev.message)}</div></div>`).join('')
        : '<div class="muted">暂无事件。</div>';
    switchWorkflowPane('events');
}

async function refreshSelectedEvents() {
    if (!selectedRunId) { toast('请先选择一条运行。'); return; }
    await loadEvents(selectedRunId);
}

function switchMonitorPane(pane) {
    switchWorkflowPane(pane);
}

function switchWorkflowPane(pane) {
    activeWorkflowPane = pane;
    const panes = ['overview', 'create', 'runs', 'build', 'events', 'reports'];
    panes.forEach(key => {
        const el = qs(`workflow-pane-${key}`);
        if (el) el.classList.toggle('active', key === pane);
    });
    document.querySelectorAll('.workflow-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.workflow === pane);
    });
    if (pane === 'overview') loadDashboard().catch(err => toast(err.message));
    if (pane === 'runs' || pane === 'reports') loadRuns();
    if (pane === 'build') loadBuildJobs();
}

function collectTestPlan() {
    let extra = {};
    const raw = qs('automation-test-plan').value.trim();
    if (raw) extra = JSON.parse(raw);
    const profile = selectedProfile();
    const profilePlan = profile.test_plan || {};
    const plan = {
        ...profilePlan,
        flash: profile.flash || profilePlan.flash || {},
        device_selector: profile.device_selector || profilePlan.device_selector || {},
        reporting: profile.reporting || profilePlan.reporting || {},
        ...extra,
        test_type: qs('automation-test-type').value.trim(),
        test_suite: qs('automation-test-suite').value,
        test_module: qs('automation-test-module').value.trim(),
        worker_id: selectedWorkerId(),
    };
    const build = collectBuildPlan();
    if (build) plan.build = build;
    return plan;
}

async function loadBuildJobs() {
    const data = await api('/api/build/jobs?limit=20');
    buildJobs = data.items || [];
    qs('build-jobs').innerHTML = buildJobs.length
        ? buildJobs.map(job => {
            const terminal = TERMINAL_STATUSES.has(job.status);
            return `<div class="build-job ${job.id === selectedBuildJobId ? 'active' : ''}" onclick="loadBuildLog('${esc(job.id)}')">
                <div class="build-job-head"><span class="badge ${esc(job.status)}">${esc(job.status)}</span><strong>${esc(job.template_id)}</strong>
                ${terminal ? `<button type="button" class="build-job-delete" title="删除历史任务" onclick="event.stopPropagation(); deleteBuildJob('${esc(job.id)}')">删除</button>` : ''}</div>
                <div class="muted">${esc(job.id)} / ${esc(job.remote_workspace || '')}</div><div>${esc((job.artifacts || [])[0]?.path || job.error || '')}</div></div>`;
        }).join('')
        : '<div class="muted">暂无构建任务。</div>';
}

async function deleteBuildJob(jobId) {
    if (!window.confirm(`确定删除历史构建任务 ${jobId}？\n此操作只删除平台记录，不删除远端源码和构建产物。`)) return;
    try {
        await api(`/api/build/jobs/${encodeURIComponent(jobId)}`, {method: 'DELETE'});
        if (selectedBuildJobId === jobId) {
            selectedBuildJobId = '';
            qs('build-log-title').textContent = '未选择任务';
            qs('build-log').textContent = '选择构建任务查看日志。';
        }
        toast(`已删除历史构建任务 ${jobId}`);
        await loadBuildJobs();
    } catch (err) { toast(err.message); }
}

async function createBuildJob() {
    try {
        const build = collectBuildPlan();
        if (!build) throw new Error('请填写编译服务器、模板、源码目录和 lunch target');
        const serverPassword = await getBuildPassword(build.server_id);
        const job = await api('/api/build/jobs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                server_id: build.server_id,
                template_id: build.template_id,
                server_password: serverPassword,
                parameters: build.parameters,
                source_type: 'manual-ui',
            }),
        });
        toast(`已创建构建任务 ${job.id}`);
        await loadBuildJobs();
        await loadBuildLog(job.id);
    } catch (err) { toast(err.message); }
}

async function loadBuildLog(jobId, {silent = false} = {}) {
    try {
        selectedBuildJobId = jobId;
        let job = await api(`/api/build/jobs/${encodeURIComponent(jobId)}`);
        if (job.server_id && !buildPasswordCache[job.server_id] && ['queued', 'running'].includes(job.status)) {
            const password = await getBuildPassword(job.server_id);
            if (password) {
                await api(`/api/build/jobs/${encodeURIComponent(jobId)}/password`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({server_password: password}),
                });
            }
        }
        job = await api(`/api/build/jobs/${encodeURIComponent(jobId)}?poll=true`);
        const log = await api(`/api/build/jobs/${encodeURIComponent(jobId)}/log?lines=5000`);
        const logEl = qs('build-log');
        const autoFollow = qs('build-log-auto-follow')?.checked !== false;
        const wasNearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 48;
        const previousTop = logEl.scrollTop;
        logEl.textContent = log.text || '暂无日志。';
        if (autoFollow && (wasNearBottom || !logEl.dataset.loaded)) {
            requestAnimationFrame(() => { logEl.scrollTop = logEl.scrollHeight; });
        } else {
            logEl.scrollTop = previousTop;
        }
        logEl.dataset.loaded = '1';
        qs('build-log-title').textContent = `${job.id} / ${job.status}`;
        if (!silent) toast(`构建任务 ${job.id}: ${job.status}`);
        await loadBuildJobs();
    } catch (err) { toast(err.message); }
}

function refreshSelectedBuildLog() {
    if (!selectedBuildJobId) {
        toast('请先选择一个构建任务');
        return;
    }
    loadBuildLog(selectedBuildJobId).catch(err => toast(err.message));
}

async function createRun() {
    try {
        const artifact = qs('automation-artifact').value.trim();
        const typedDevices = qs('automation-devices').value
            .split(/[,\s]+/)
            .map(item => item.trim())
            .filter(Boolean);
        const checkedDevices = Array.from(qs('automation-device-list').querySelectorAll('input[type="checkbox"]:checked'))
            .map(opt => opt.value)
            .filter(Boolean);
        const devices = [...new Set([...typedDevices, ...checkedDevices])];
        const testPlan = collectTestPlan();
        const buildServerPassword = testPlan.build ? await getBuildPassword(testPlan.build.server_id) : '';
        const payload = {
            profile_id: qs('automation-profile').value,
            source_type: 'manual',
            artifact_path: artifact.startsWith('http') ? '' : artifact,
            artifact_url: artifact.startsWith('http') ? artifact : '',
            devices,
            test_plan: testPlan,
            build_server_password: buildServerPassword,
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

function traceField(label, value) {
    if (value === undefined || value === null || value === '') return '';
    return `<div class="trace-key">${esc(label)}</div><div class="trace-val">${esc(value)}</div>`;
}

async function loadDashboard() {
    const data = await api('/api/automation/dashboard');
    const stats = qs('dashboard-stats');
    if (!stats) return;
    const byStatus = data.run_by_status || {};
    let inProgress = 0, failedTotal = 0;
    for (const [s, c] of Object.entries(byStatus)) {
        if (!TERMINAL_STATUSES.has(s)) inProgress += c;
        if (FAILURE_STATUSES.has(s)) failedTotal += c;
    }
    const card = (value, label, cls = '') => `<div class="stat-card ${cls}"><div class="stat-value">${esc(value)}</div><div class="stat-label">${esc(label)}</div></div>`;
    stats.innerHTML = [
        card(data.run_total || 0, '运行总数'),
        card(inProgress, '进行中', 'primary'),
        card(data.completed_total || 0, '已完成', 'ok'),
        card(failedTotal, '失败', failedTotal ? 'danger' : ''),
        card(data.build_total || 0, '构建任务'),
    ].join('');

    const statusEl = qs('dashboard-status-breakdown');
    if (statusEl) {
        const entries = Object.entries(byStatus).sort((a, b) => b[1] - a[1]);
        statusEl.innerHTML = entries.length
            ? entries.map(([s, c]) => `<div class="breakdown-row"><span class="badge ${esc(s)}">${esc(s)}</span><span class="breakdown-count">${esc(c)}</span></div>`).join('')
            : '<div class="muted">暂无运行。</div>';
    }

    const profileEl = qs('dashboard-profile-breakdown');
    if (profileEl) {
        const profiles = data.run_by_profile || {};
        const rows = Object.entries(profiles).map(([pid, counts]) => {
            const total = Object.values(counts).reduce((a, b) => a + b, 0);
            const done = (counts.completed || 0);
            const failed = Object.entries(counts).filter(([s]) => FAILURE_STATUSES.has(s)).reduce((s, [, c]) => s + c, 0);
            return `<div class="breakdown-row"><span><strong>${esc(pid)}</strong></span><span class="muted">${esc(done)}✓ / ${esc(failed)}✗ / ${esc(total)}</span></div>`;
        });
        profileEl.innerHTML = rows.length ? rows.join('') : '<div class="muted">暂无数据。</div>';
    }
}

async function loadTrace(runId) {
    try {
        const data = await api(`/api/automation/runs/${encodeURIComponent(runId)}/trace`);
        qs('trace-title').textContent = `运行链路 / ${data.run_id}`;
        const commit = data.commit || {};
        qs('trace-commit').innerHTML = [
            traceField('Change-Id', commit.gerrit_change_id),
            traceField('Patchset', commit.gerrit_patchset ? `PS${commit.gerrit_patchset}` : ''),
            traceField('分支', commit.branch),
            traceField('项目', data.profile_id),
            traceField('主题', commit.gerrit_subject),
        ].join('') || '<div class="muted">无 Gerrit 提交信息（手动运行）。</div>';

        const build = data.build_job;
        qs('trace-build').innerHTML = build ? [
            `<div class="trace-key">状态</div><div class="trace-val"><span class="badge ${esc(build.status)}">${esc(build.status)}</span></div>`,
            traceField('模板', build.template_id),
            traceField('工作目录', build.remote_workspace),
            traceField('产物', (build.artifacts || [])[0]?.path || ''),
            data.build_job_id ? `<div class="trace-key">任务</div><div class="trace-val"><a href="#" onclick="event.preventDefault(); jumpToBuildLog('${esc(data.build_job_id)}')">${esc(data.build_job_id)}</a></div>` : '',
        ].join('') : '<div class="muted">无关联构建任务。</div>';

        qs('trace-artifact').textContent = data.artifact_path || '（无固件产物）';

        const summary = data.result_summary || {};
        const reportRows = [
            traceField('报告时间', compactTime(data.report_timestamp)),
            ...Object.entries(summary).slice(0, 8).map(([k, v]) => traceField(k, typeof v === 'object' ? JSON.stringify(v) : v)),
        ].join('');
        qs('trace-report').innerHTML = reportRows
            + (data.report_url ? `<div class="trace-link-row"><a href="${esc(data.report_url)}" target="_blank">查看完整报告</a></div>` : '')
            || '<div class="muted">尚未生成报告。</div>';

        openTrace();
    } catch (err) { toast(err.message); }
}

function jumpToBuildLog(jobId) {
    closeTrace();
    switchWorkflowPane('build');
    loadBuildLog(jobId).catch(err => toast(err.message));
}

function openTrace() {
    qs('ats-trace-drawer').classList.add('open');
    qs('ats-trace-backdrop').classList.add('open');
    qs('ats-trace-drawer').setAttribute('aria-hidden', 'false');
}
function closeTrace() {
    qs('ats-trace-drawer').classList.remove('open');
    qs('ats-trace-backdrop').classList.remove('open');
    qs('ats-trace-drawer').setAttribute('aria-hidden', 'true');
}

async function dryRunProfile() {
    try {
        const profileId = qs('automation-profile').value;
        if (!profileId) throw new Error('请先选择运行配置');
        const payload = {
            project: qs('dryrun-project').value.trim(),
            branch: qs('dryrun-branch').value.trim(),
            change_id: qs('dryrun-change-id').value.trim(),
            patchset: qs('dryrun-patchset').value.trim(),
        };
        const data = await api(`/api/automation/profiles/${encodeURIComponent(profileId)}/dry-run`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const result = qs('dryrun-result');
        const req = data.run_request || {};
        result.innerHTML = `<span class="badge ${data.matched ? 'completed' : 'failed'}">${data.matched ? '匹配' : '未匹配'}</span>`
            + (data.matched ? `<div class="muted" style="margin-top:6px">将创建运行：${esc(req.profile_id || profileId)} / ${esc(req.test_plan?.test_type || '-')} / 设备 ${(req.devices || []).length}</div>` : '');
        toast(data.matched ? '试运行：匹配，会创建运行' : '试运行：未匹配');
    } catch (err) { toast(err.message); }
}

function setStatusFilter(status) {
    atsStatus = status;
    document.querySelectorAll('.subtabs .tab').forEach(btn => btn.classList.toggle('active', btn.dataset.status === status));
    loadRuns().catch(err => toast(err.message));
}

async function loadWorkerStatus() {
    try {
        const data = await api('/api/automation/worker/status');
        renderWorkerStatus(data);
    } catch { /* worker status is informational only */ }
}

function renderWorkerStatus(data) {
    const el = qs('worker-indicator');
    if (!el) return;
    if (!data) { el.className = 'worker-dot down'; el.title = 'Worker 未知'; return; }
    const ago = data.last_tick_seconds_ago;
    const alive = data.running && (ago === null || ago < data.interval_seconds * 4);
    el.className = `worker-dot ${alive ? 'up' : 'down'}`;
    el.title = alive
        ? `Worker 运行中（${data.executor}，${data.interval_seconds}s，上次 tick ${ago === null ? '-' : ago + 's'} 前）`
        : `Worker 未运行或停滞${ago !== null ? '（上次 tick ' + ago + 's 前）' : ''}`;
}

async function loadAll(silent = false) {
    try {
        await loadDashboard();
    } catch (_) { /* 概览失败不阻断主流程 */ }
    try {
        await Promise.all([loadBuildConfig(), loadClusterWorkers()]);
        await loadTestSuitesForAutomation();
        await Promise.all([loadDevices(false), loadProfiles()]);
        await Promise.all([loadRuns(), loadBuildJobs(), loadWorkerStatus()]);
        if (!silent) toast('已刷新');
    } catch (err) { toast(err.message); }
}

function userIsEditing() {
    const el = document.activeElement;
    if (!el) return false;
    const tag = (el.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
}

// 统一定时刷新：用户正在编辑输入时跳过本轮，避免打断
setInterval(async () => {
    if (userIsEditing()) return;
    try {
        await loadWorkerStatus();
        if (activeWorkflowPane === 'overview') await loadDashboard();
        if (activeWorkflowPane === 'runs' || activeWorkflowPane === 'reports') await loadRuns();
        if (activeWorkflowPane === 'build') {
            await loadBuildJobs();
            if (selectedBuildJobId) await loadBuildLog(selectedBuildJobId, {silent: true});
        }
        if (activeWorkflowPane === 'events' && selectedRunId) await refreshSelectedEvents();
    } catch (_) { /* 后台刷新静默失败 */ }
}, 8000);

document.addEventListener('DOMContentLoaded', () => loadAll(true));
