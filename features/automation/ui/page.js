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
let atsWorkspaceContext = {};
let applyingWorkspaceContext = false;
let atsLocalWorkerId = 'worker-local';
let pendingBuildWorkspace = '';
let pendingBuildLunchTarget = '';
let workspaceDiscoveryRequest = 0;
let lunchDiscoveryRequest = 0;
let lunchOptionsContext = '';

function isLocalAutomationWorker(workerId) {
    return !workerId || workerId === atsLocalWorkerId;
}

function workspaceDeviceSerial(value) {
    const text = String(value || '');
    const worker = selectedWorkerId();
    return !isLocalAutomationWorker(worker) && text.startsWith(`${worker}:`)
        ? text.slice(worker.length + 1) : text;
}

function syncAutomationWorkspaceSelection(extra = {}) {
    if (applyingWorkspaceContext) return;
    const workerId = selectedWorkerId();
    const checked = Array.from(document.querySelectorAll(
        '#automation-device-list input[type="checkbox"]:checked'
    )).map(input => input.value);
    const suitePath = qs('automation-test-suite')?.value || '';
    const suite = testSuites.find(item => (item.tools_path || item.full_path) === suitePath);
    window.GmsEmbeddedWorkspace?.update({
        worker_id: workerId,
        device_ids: [...new Set(checked)].map(value =>
            isLocalAutomationWorker(workerId) || value.startsWith(`${workerId}:`)
                ? value : `${workerId}:${value}`),
        suite_key: suite?.suite_key || suitePath,
        suite_path: suitePath,
        origin_page: 'automation',
        ...extra,
    });
}

async function applyAutomationWorkspaceContext(next, navigate = false) {
    atsWorkspaceContext = {...atsWorkspaceContext, ...(next || {})};
    applyingWorkspaceContext = true;
    try {
        const worker = atsWorkspaceContext.scope_mode === 'cluster'
            ? (atsWorkspaceContext.worker_id || atsLocalWorkerId) : atsLocalWorkerId;
        const workerSelect = qs('automation-worker');
        const workerOption = workerSelect
            ? Array.from(workerSelect.options).find(option => option.value === worker && !option.disabled)
            : null;
        if (workerSelect && workerOption) {
            const changed = workerSelect.value !== worker;
            workerSelect.value = worker;
            if (changed) {
                await loadTestSuitesForAutomation();
                await loadDevices(false);
            }
        }
        if (atsWorkspaceContext.suite_path) {
            setSelectValue('automation-test-suite', atsWorkspaceContext.suite_path);
        }
        const selected = new Set((atsWorkspaceContext.device_ids || []).map(workspaceDeviceSerial));
        document.querySelectorAll('#automation-device-list input[type="checkbox"]').forEach(input => {
            input.checked = !input.disabled && selected.has(workspaceDeviceSerial(input.value));
        });
        if (atsWorkspaceContext.gerrit_change_id) {
            if (qs('dryrun-change-id')) qs('dryrun-change-id').value = atsWorkspaceContext.gerrit_change_id;
            if (qs('dryrun-patchset')) qs('dryrun-patchset').value = atsWorkspaceContext.gerrit_patchset || '';
        }
        if (navigate && atsWorkspaceContext.automation_run_id) {
            await loadRuns();
            if (atsRuns.some(run => run.id === atsWorkspaceContext.automation_run_id)) {
                await loadEvents(atsWorkspaceContext.automation_run_id);
            }
        }
    } finally {
        applyingWorkspaceContext = false;
    }
}

window.addEventListener('gms:embedded-workspace', event => {
    applyAutomationWorkspaceContext(
        event.detail?.context || {},
        event.detail?.type === 'workspace-context-navigate'
    ).catch(error => toast(error.message));
});

function selectedWorkerId() {
    return qs('automation-worker')?.value || atsLocalWorkerId;
}

async function loadClusterWorkers() {
    const select = qs('automation-worker');
    if (!select) return;
    try {
        const statusResponse = await fetch('/api/cluster/status', {cache: 'no-store'});
        const status = await statusResponse.json();
        atsLocalWorkerId = String(status.local_worker_id || atsLocalWorkerId);
        const contextWorker = atsWorkspaceContext.scope_mode === 'cluster'
            ? atsWorkspaceContext.worker_id : atsLocalWorkerId;
        const previous = contextWorker || select.value || atsLocalWorkerId;
        if (!statusResponse.ok || !status.enabled) {
            select.innerHTML = `<option value="${esc(atsLocalWorkerId)}">Controller / Local Worker（需启用集群 Agent）</option>`;
            select.disabled = true;
            select.title = 'GMS ATS 使用持久化任务队列，请先启用集群能力和 Worker Agent';
            return;
        }
        const response = await fetch('/api/cluster/workers', {cache: 'no-store'});
        const payload = await response.json();
        const workers = payload.workers || [];
        const workerAvailability = worker => {
            if (!['online', 'busy'].includes(worker.status)) return '离线';
            if (
                isLocalAutomationWorker(worker.id)
                && String(worker.agent_version || '').startsWith('controller-')
            ) return '未安装 ATS Agent';
            if (!isLocalAutomationWorker(worker.id) && !status.remote_dispatch_enabled) {
                return '未启用远程派发';
            }
            return '';
        };
        const labelForWorker = worker => {
            const name = String(worker.name || worker.id);
            const host = String(worker.address || worker.hostname || '');
            return host && !name.includes(host) ? `${name} / ${host}` : name;
        };
        const eligible = workers.filter(worker => !workerAvailability(worker));
        select.innerHTML = workers.map(worker => {
            const reason = workerAvailability(worker);
            const label = `${labelForWorker(worker)}${reason ? `（${reason}）` : ''}`;
            return `<option value="${esc(worker.id)}"${reason ? ' disabled' : ''}>${esc(label)}</option>`;
        }).join('');
        select.disabled = !eligible.length;
        const unavailable = workers.filter(workerAvailability).map(labelForWorker);
        select.title = eligible.length
            ? (unavailable.length ? `${unavailable.join('、')} 当前不可用` : '')
            : '没有安装持久化 Agent 的在线 Worker';
        const selected = eligible.find(worker => worker.id === previous) || eligible[0];
        if (selected) select.value = selected.id;
    } catch (_) {
        select.innerHTML = `<option value="${esc(atsLocalWorkerId)}">Controller / Local Worker</option>`;
        select.disabled = true;
    }
}

// 14 段流水线阶段（顺序即推进顺序）
const PIPELINE_STAGES = [
    'queued', 'jenkins_queued', 'jenkins_building', 'artifact_ready',
    'waiting_device', 'device_locked', 'flashing', 'flash_verified',
    'testing', 'test_running', 'report_collecting', 'analyzing', 'reporting', 'completed',
];
const STAGE_LABELS_ZH = {
    queued: '排队', jenkins_queued: '构建排队', jenkins_building: '编译中',
    artifact_ready: '固件就绪', waiting_device: '等待设备', device_locked: '设备已锁',
    flashing: '刷机中', flash_verified: '刷机校验', testing: '启动测试', test_running: '测试中',
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
    test_failed: 9, analysis_failed: 11, reporting_failed: 12,
};

function qs(id) { return document.getElementById(id); }
function syncAutomationOverlayState() {
    const hasOverlay = Boolean(
        document.querySelector('.password-backdrop')
        || qs('ats-trace-drawer')?.classList.contains('open')
    );
    document.body.classList.toggle('overlay-open', hasOverlay);
}
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
function openRunReport(event, runId) {
    event.stopPropagation();
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
    const text = await resp.text();
    let data;
    try {
        data = text ? JSON.parse(text) : {};
    } catch (_error) {
        data = {success: false, error: text || `HTTP ${resp.status}`};
    }
    if (!resp.ok || !data.success) {
        throw new Error(data.error || data.message || `请求失败 (HTTP ${resp.status})`);
    }
    return data.data;
}

async function loadProfiles() {
    const data = await api('/api/automation/profiles?enabled_only=true');
    atsProfiles = data.items || [];
    const select = qs('automation-profile');
    select.innerHTML = atsProfiles.length
        ? atsProfiles.map(p => `<option value="${esc(p.id)}">${esc(p.name || p.id)}</option>`).join('')
        : '<option value="">手动配置（未套用 Profile）</option>';
    select.onchange = applySelectedProfile;
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
    if (id === 'build-command') select.title = text;
}

function renderSelectedProfileSummary(profile) {
    const summary = qs('automation-profile-summary');
    const dryrun = document.querySelector('.profile-dryrun');
    if (!profile?.id) {
        summary.textContent = '未套用 Profile；使用下方手动参数';
        if (dryrun) dryrun.hidden = true;
        return;
    }
    const build = profile.build || {};
    const plan = profile.test_plan || {};
    const selector = profile.device_selector || {};
    const parts = [];
    if (build.server_id || build.template_id) parts.push('构建默认值');
    if (plan.test_type) parts.push(String(plan.test_type).toUpperCase());
    if (plan.test_module || (plan.modules || []).length) parts.push('模块测试');
    parts.push(`设备 ${Math.max(1, Number(selector.min_count || 1))} 台`);
    summary.textContent = `已套用：${parts.join(' · ')}`;
    if (dryrun) dryrun.hidden = false;
}

function applySelectedProfile() {
    const profile = selectedProfile();
    renderSelectedProfileSummary(profile);
    if (!profile.id) {
        syncArtifactMode();
        syncBuildSectionState();
        return;
    }
    const build = profile.build || {};
    const parameters = build.parameters || {};
    pendingBuildWorkspace = String(parameters.workspace || '');
    pendingBuildLunchTarget = String(parameters.lunch_target || '');
    setSelectValue('build-server', build.server_id);
    renderBuildTemplates(build.template_id);
    setSelectValue('build-workspace', pendingBuildWorkspace);
    invalidateLunchOptions(pendingBuildLunchTarget
        ? `请读取源码目录，确认 Profile 中的 ${pendingBuildLunchTarget} 是否可用`
        : '选择源码目录后自动读取该目录的 Lunch Target');
    setSelectValue('build-command', parameters.build_command);
    syncBuildSectionState();

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
    renderBuildTemplates();
    renderBuildWorkspaces([]);
    invalidateLunchOptions('选择源码目录后自动读取该目录的 Lunch Target');
    qs('build-server').onchange = handleBuildServerChange;
    qs('build-template').onchange = () => applyBuildTemplateDefaults();
    qs('build-workspace').onchange = handleBuildWorkspaceChange;
    qs('build-command').onchange = syncBuildCommandTitle;
    syncBuildCommandTitle();
    syncArtifactMode();
    syncBuildSectionState();
}

function selectedBuildTemplate() {
    return buildTemplates.find(template => template.id === qs('build-template')?.value) || {};
}

function renderBuildTemplates(preferredTemplate = '') {
    const select = qs('build-template');
    if (!select) return;
    const serverId = qs('build-server')?.value || '';
    const matching = buildTemplates.filter(template => !template.server_id || template.server_id === serverId);
    select.innerHTML = matching.length
        ? matching.map(template => `<option value="${esc(template.id)}">${esc(template.name || template.id)}</option>`).join('')
        : '<option value="">当前服务器无可用模板</option>';
    if (preferredTemplate && matching.some(template => template.id === preferredTemplate)) {
        select.value = preferredTemplate;
    }
    syncBuildTemplateHint();
}

function syncBuildTemplateHint() {
    const hint = qs('build-template-hint');
    if (!hint) return;
    const template = selectedBuildTemplate();
    if (!template.id) {
        hint.textContent = '模板限定初始化、编译超时和产物规则';
        return;
    }
    const defaultCommand = template.parameters_schema?.build_command?.default || template.command || '';
    const init = (template.init_commands || []).join(' → ');
    hint.textContent = [init, defaultCommand ? `默认 ${defaultCommand}` : ''].filter(Boolean).join('；');
}

function applyBuildTemplateDefaults() {
    const template = selectedBuildTemplate();
    const defaultCommand = template.parameters_schema?.build_command?.default;
    if (defaultCommand) setSelectValue('build-command', defaultCommand);
    syncBuildTemplateHint();
}

function syncBuildCommandTitle() {
    const command = qs('build-command');
    if (command) command.title = command.value || '';
}

function collectBuildPlan({forceBuild = false} = {}) {
    if (!forceBuild && qs('automation-artifact')?.value.trim()) return null;
    const serverId = qs('build-server').value;
    const templateId = qs('build-template').value;
    const workspace = qs('build-workspace').value;
    const lunchTarget = qs('build-lunch-target').value;
    const buildCommand = qs('build-command').value;
    if (!serverId || !templateId || !workspace) {
        throw new Error('请先选择编译服务器、模板和源码目录');
    }
    if (!lunchTarget || lunchOptionsContext !== buildSelectionContext(serverId, workspace)) {
        throw new Error('请先读取当前源码目录的 Lunch Target');
    }
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
        const focusOrigin = document.activeElement;
        const backdrop = document.createElement('div');
        backdrop.className = 'password-backdrop';
        backdrop.setAttribute('role', 'dialog');
        backdrop.setAttribute('aria-modal', 'true');
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
        syncAutomationOverlayState();
        const input = backdrop.querySelector('#build-password-input');
        const finish = value => {
            backdrop.remove();
            syncAutomationOverlayState();
            if (focusOrigin && focusOrigin.isConnected && typeof focusOrigin.focus === 'function') {
                focusOrigin.focus({preventScroll: true});
            }
            resolve(value || '');
        };
        backdrop.addEventListener('click', event => {
            if (event.target === backdrop) finish('');
        });
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
    const server = buildServers.find(item => item.id === serverId);
    if (server?.auth?.type === 'env_password') return '';
    if (buildPasswordCache[serverId]) return buildPasswordCache[serverId];
    const password = await promptBuildPassword();
    if (password) buildPasswordCache[serverId] = password;
    return password;
}

function selectedBuildServer() {
    return buildServers.find(s => s.id === qs('build-server').value) || {};
}

function buildSelectionContext(serverId = qs('build-server')?.value, workspace = qs('build-workspace')?.value) {
    return `${String(serverId || '')}\n${String(workspace || '')}`;
}

function setBuildFieldStatus(id, message, state = '') {
    const element = qs(id);
    if (!element) return;
    element.textContent = message;
    element.className = `field-status${state ? ` ${state}` : ''}`;
}

function setBuildControlBusy(id, busy) {
    const element = qs(id);
    if (!element) return;
    element.dataset.loading = busy ? 'true' : 'false';
}

function syncBuildSectionState() {
    const enabled = !Boolean(qs('automation-artifact')?.value.trim());
    qs('automation-build-fields')?.classList.toggle('is-disabled', !enabled);
    const server = qs('build-server');
    const template = qs('build-template');
    const workspace = qs('build-workspace');
    const command = qs('build-command');
    const lunch = qs('build-lunch-target');
    if (server) server.disabled = !enabled || !Array.from(server.options).some(option => option.value);
    if (template) template.disabled = !enabled || !Array.from(template.options).some(option => option.value);
    if (workspace) workspace.disabled = !enabled || !Array.from(workspace.options).some(option => option.value);
    if (command) command.disabled = !enabled;
    if (lunch) {
        const scoped = lunchOptionsContext === buildSelectionContext();
        lunch.disabled = !enabled || !scoped || !Array.from(lunch.options).some(option => option.value);
    }
    const workspaceRefresh = qs('build-workspace-refresh');
    if (workspaceRefresh) {
        workspaceRefresh.disabled = !enabled || !server?.value || workspaceRefresh.dataset.loading === 'true';
    }
    const lunchRefresh = qs('build-lunch-refresh');
    if (lunchRefresh) {
        lunchRefresh.disabled = !enabled || !workspace?.value || lunchRefresh.dataset.loading === 'true';
    }
}

function syncArtifactMode() {
    const artifact = qs('automation-artifact')?.value.trim() || '';
    const hint = qs('artifact-mode-hint');
    if (hint) {
        hint.textContent = artifact
            ? '本次运行将直接使用已有固件，下方源码编译参数已忽略'
            : '留空时按下方参数从源码编译；填写后直接使用该固件';
        hint.classList.toggle('ready', Boolean(artifact));
    }
    syncBuildSectionState();
}

function renderBuildWorkspaces(items, preferredWorkspace = '') {
    const server = selectedBuildServer();
    const root = String(server.workspace_root || '').replace(/\/$/, '');
    const options = (items || []).map(name => {
        const value = name.startsWith('/') ? name : `${root}/${name}`;
        return `<option value="${esc(value)}">${esc(name)}</option>`;
    });
    const select = qs('build-workspace');
    select.innerHTML = options.length ? options.join('') : '<option value="">请先扫描源码目录</option>';
    const preferred = String(preferredWorkspace || pendingBuildWorkspace || '');
    if (preferred && Array.from(select.options).some(option => option.value === preferred)) {
        select.value = preferred;
    }
    syncBuildSectionState();
}

function renderLunchOptions(items, preferredTarget = '') {
    const select = qs('build-lunch-target');
    select.innerHTML = (items || []).length
        ? items.map(item => `<option value="${esc(item)}">${esc(item)}</option>`).join('')
        : '<option value="">尚未读取 Lunch Target</option>';
    const preferred = String(preferredTarget || '');
    if (preferred && Array.from(select.options).some(option => option.value === preferred)) {
        select.value = preferred;
    }
    syncBuildSectionState();
}

function invalidateLunchOptions(message = '选择源码目录后自动读取该目录的 Lunch Target') {
    lunchDiscoveryRequest += 1;
    lunchOptionsContext = '';
    setBuildControlBusy('build-lunch-refresh', false);
    renderLunchOptions([]);
    setBuildFieldStatus('build-lunch-status', message);
}

async function handleBuildServerChange() {
    workspaceDiscoveryRequest += 1;
    renderBuildTemplates();
    applyBuildTemplateDefaults();
    renderBuildWorkspaces([]);
    invalidateLunchOptions();
    setBuildFieldStatus('build-workspace-status', '正在扫描所选服务器的源码目录…', 'loading');
    await refreshBuildWorkspaces();
}

async function handleBuildWorkspaceChange() {
    invalidateLunchOptions('正在读取所选源码目录的 Lunch Target…');
    if (qs('build-workspace').value) await refreshLunchOptions({silent: true});
}

async function refreshBuildWorkspaces() {
    const serverId = qs('build-server').value;
    if (!serverId) {
        toast('请先选择编译服务器');
        return;
    }
    const requestId = ++workspaceDiscoveryRequest;
    const preferredWorkspace = qs('build-workspace').value || pendingBuildWorkspace;
    setBuildControlBusy('build-workspace-refresh', true);
    setBuildFieldStatus('build-workspace-status', '正在扫描源码目录…', 'loading');
    syncBuildSectionState();
    try {
        const password = await getBuildPassword(serverId);
        const data = await api('/api/build/discover/workspaces', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({server_id: serverId, server_password: password}),
        });
        if (requestId !== workspaceDiscoveryRequest || serverId !== qs('build-server').value) return;
        const items = data.items || [];
        renderBuildWorkspaces(items, preferredWorkspace);
        invalidateLunchOptions();
        setBuildFieldStatus(
            'build-workspace-status',
            items.length ? `已在当前服务器发现 ${items.length} 个源码目录` : '当前服务器未发现源码目录',
            items.length ? 'ready' : 'error',
        );
        if (qs('build-workspace').value) {
            await refreshLunchOptions({silent: true, preferredTarget: pendingBuildLunchTarget});
        }
        toast(`已刷新 ${items.length} 个源码目录`);
    } catch (err) {
        if (requestId !== workspaceDiscoveryRequest) return;
        setBuildFieldStatus('build-workspace-status', err.message, 'error');
        toast(err.message);
    } finally {
        if (requestId === workspaceDiscoveryRequest) {
            setBuildControlBusy('build-workspace-refresh', false);
            syncBuildSectionState();
        }
    }
}

async function refreshLunchOptions({silent = false, preferredTarget = '', forceRefresh = false} = {}) {
    const serverId = qs('build-server').value;
    const workspace = qs('build-workspace').value;
    if (!workspace) {
        toast('请先选择源码目录');
        return;
    }
    const context = buildSelectionContext(serverId, workspace);
    const previousTarget = qs('build-lunch-target').value;
    const requestId = ++lunchDiscoveryRequest;
    lunchOptionsContext = '';
    setBuildControlBusy('build-lunch-refresh', true);
    renderLunchOptions([]);
    setBuildFieldStatus('build-lunch-status', `正在从 ${workspace} 读取…`, 'loading');
    syncBuildSectionState();
    try {
        const password = await getBuildPassword(serverId);
        const data = await api('/api/build/discover/lunch-options', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                server_id: serverId,
                workspace,
                server_password: password,
                force_refresh: forceRefresh,
            }),
        });
        if (requestId !== lunchDiscoveryRequest || context !== buildSelectionContext()) return;
        const items = data.items || [];
        lunchOptionsContext = context;
        renderLunchOptions(items, preferredTarget || previousTarget || pendingBuildLunchTarget);
        setBuildFieldStatus(
            'build-lunch-status',
            items.length
                ? `已从当前源码树发现 ${items.length} 个 Lunch Target`
                : `${workspace} 中未发现 Lunch Target`,
            items.length ? 'ready' : 'error',
        );
        if (!silent) toast(`已从 ${workspace} 读取 ${items.length} 个 Lunch Target`);
    } catch (err) {
        if (requestId !== lunchDiscoveryRequest) return;
        lunchOptionsContext = '';
        renderLunchOptions([]);
        setBuildFieldStatus('build-lunch-status', err.message, 'error');
        toast(err.message);
    } finally {
        if (requestId === lunchDiscoveryRequest) {
            setBuildControlBusy('build-lunch-refresh', false);
            syncBuildSectionState();
        }
    }
}

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
                const label = `${id}${unavailable ? ` (${deviceState})` : ''}`;
                return `<label class="checkbox-item${unavailable ? ' muted' : ''}"><input type="checkbox" value="${esc(id)}"${unavailable ? ' disabled' : ''} onchange="syncAutomationWorkspaceSelection()"> <span>${esc(label)}</span></label>`;
            }).join('')
            : '<div class="muted">未发现设备</div>';
        await applyAutomationWorkspaceContext(atsWorkspaceContext);
    } catch (err) { toast(err.message); }
}

async function loadTestSuitesForAutomation() {
    try {
        const workerId = selectedWorkerId();
        const endpoint = isLocalAutomationWorker(workerId) ? '/api/test/suites'
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
        if (atsWorkspaceContext.suite_path) {
            setSelectValue('automation-test-suite', atsWorkspaceContext.suite_path);
        }
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
    const effectiveStatus = activeWorkflowPane === 'reports' ? '' : atsStatus;
    const query = effectiveStatus ? `?status=${encodeURIComponent(effectiveStatus)}&limit=100` : '?limit=100';
    const data = await api('/api/automation/runs' + query);
    atsRuns = data.items || [];
    const list = qs('automation-runs');
    const reportList = qs('automation-runs-report');
    if (!atsRuns.length) {
        list.innerHTML = '<div class="muted">暂无运行记录。</div>';
        if (reportList) reportList.innerHTML = '<div class="empty-state">暂无已生成的测试报告。</div>';
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
                        <div class="run-value">${run.report_timestamp ? `<button type="button" onclick="openRunReport(event, '${esc(run.id)}')">查看报告</button>` : '<span class="muted">-</span>'}</div>
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
                ${TERMINAL_STATUSES.has(run.status)
                    ? `<button type="button" onclick="event.stopPropagation(); retryRun('${esc(run.id)}')">重试</button>`
                    : run.status === 'flashing'
                    ? '<button type="button" class="danger" disabled title="刷机过程中断电或终止可能损坏设备">刷机中不可取消</button>'
                    : `<button type="button" class="danger" onclick="event.stopPropagation(); cancelRun('${esc(run.id)}')">取消</button>`}
            </div>
        </article>
    `).join('');
    list.innerHTML = html;
    if (reportList) {
        const reportRuns = atsRuns.filter(run => run.report_timestamp);
        reportList.innerHTML = reportRuns.length
            ? reportRuns.map(run => `
                <article class="report-card">
                    <div class="report-card-main">
                        <div class="run-title">
                            <span class="badge ${esc(run.status)}">${esc(run.status)}</span>
                            <strong>${esc(run.profile_id || 'manual')}</strong>
                            <span class="muted">${esc(run.source_type || 'manual')}</span>
                        </div>
                        ${runMeta(run) ? `<div class="muted">${esc(runMeta(run))}</div>` : ''}
                        <div class="report-card-meta">
                            <span>报告：${esc(compactTime(run.report_timestamp))}</span>
                            <span>设备：${esc(formatDevices(run.devices_json))}</span>
                            <span>运行：${esc(run.id)}</span>
                        </div>
                    </div>
                    <button type="button" class="primary" onclick="openRunReport(event, '${esc(run.id)}')">打开报告</button>
                </article>
            `).join('')
            : '<div class="empty-state">暂无已生成的测试报告。</div>';
    }
}

async function loadEvents(runId) {
    selectedRunId = runId;
    const run = atsRuns.find(r => r.id === runId);
    window.GmsEmbeddedWorkspace?.update({
        automation_run_id: runId,
        worker_id: run ? (run.worker_id || selectedWorkerId()) : selectedWorkerId(),
        cluster_job_id: run?.cluster_job_id || '',
        attempt_id: run?.attempt_id || '',
        report_id: run?.report_id || '',
        report_timestamp: run?.report_timestamp || '',
        gerrit_change_id: run?.gerrit_change_id || '',
        gerrit_patchset: run?.gerrit_patchset || '',
        origin_page: 'automation'
    });
    qs('events-title').textContent = run ? `全链路时间线 / ${run.profile_id || run.id}` : '全链路时间线';
    qs('automation-runs').querySelectorAll('.run-card').forEach(el => el.classList.remove('active'));
    const card = qs('automation-runs').querySelector(`.run-card[onclick="loadEvents('${runId}')"]`);
    if (card) card.classList.add('active');
    const data = await api(`/api/automation/runs/${encodeURIComponent(runId)}/timeline`);
    const items = data.items || [];
    qs('automation-events').innerHTML = items.length
        ? items.map(ev => {
            const level = String(ev.level || 'info').toLowerCase();
            const levelClass = level.includes('error') || level.includes('fail')
                ? 'error' : level.includes('warn') ? 'warning' : 'info';
            return `<article class="event">
                <div class="event-meta">
                    <span class="event-time">${esc(compactTime(ev.created_at))}</span>
                    <span class="badge">${esc(ev.domain || 'automation')}</span>
                    <span class="badge">${esc(ev.stage || ev.to_state || ev.event_type || '-')}</span>
                    <span class="event-level ${levelClass}">${esc(level)}</span>
                </div>
                <div class="event-body">
                    <div class="event-message">${esc(ev.message)}</div>
                    ${(ev.operation_id || ev.from_state || ev.to_state) ? `<div class="muted">${esc([
                        ev.from_state && ev.to_state ? `${ev.from_state} → ${ev.to_state}` : '',
                        ev.operation_id ? `operation ${ev.operation_id}` : '',
                    ].filter(Boolean).join(' · '))}</div>` : ''}
                </div>
            </article>`;
        }).join('')
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
        if (el) {
            el.classList.toggle('active', key === pane);
            el.setAttribute('aria-hidden', key === pane ? 'false' : 'true');
        }
    });
    document.querySelectorAll('.workflow-tab').forEach(tab => {
        const active = tab.dataset.workflow === pane;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
        tab.tabIndex = active ? 0 : -1;
    });
    if (pane === 'overview') loadDashboard().catch(err => toast(err.message));
    if (pane === 'runs' || pane === 'reports') {
        loadRuns().catch(err => toast(err.message));
    }
    if (pane === 'build') loadBuildJobs().catch(err => toast(err.message));
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
        const build = collectBuildPlan({forceBuild: true});
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
        const checkedDevices = Array.from(qs('automation-device-list').querySelectorAll('input[type="checkbox"]:checked'))
            .map(opt => opt.value)
            .filter(Boolean);
        const devices = [...new Set(checkedDevices)];
        const testPlan = collectTestPlan();
        if (atsWorkspaceContext.redmine_issue_id) {
            testPlan.redmine_issue_id = atsWorkspaceContext.redmine_issue_id;
        }
        const buildServerPassword = testPlan.build ? await getBuildPassword(testPlan.build.server_id) : '';
        const payload = {
            profile_id: qs('automation-profile').value,
            source_type: 'manual',
            artifact_path: artifact.startsWith('http') ? '' : artifact,
            artifact_url: artifact.startsWith('http') ? artifact : '',
            devices,
            test_plan: testPlan,
            build_server_password: buildServerPassword,
            gerrit_change_id: atsWorkspaceContext.gerrit_change_id || '',
            gerrit_patchset: atsWorkspaceContext.gerrit_patchset || '',
        };
        const preflight = await api('/api/automation/runs/preflight', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const run = await api('/api/automation/runs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        toast(`已创建 ${run.id} / ${preflight.worker_id} / ${preflight.test_type}`);
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
    }
}

async function pollGerrit() {
    try {
        const data = await api('/api/automation/gerrit/poll', {method: 'POST'});
        const rejected = data.rejected_count || 0;
        const suffix = rejected ? `，预检拒绝 ${rejected} 条：${data.rejected?.[0]?.error || '资源未就绪'}` : '';
        toast(`Gerrit poll 创建 ${data.created_count || 0} 条，已存在 ${data.existing_count || 0} 条${suffix}`);
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

        qs('trace-artifact').innerHTML = [
            traceField('产物路径', data.artifact_path),
            traceField('产物 ID', data.build_artifact_id),
            traceField('Worker', data.worker_id),
            traceField('设备预约', data.device_reservation_id),
            traceField('烧写暂存', data.flash_stage_id),
            traceField('烧写命令', data.flash_command_id),
        ].join('') || '<div class="muted">（无固件产物）</div>';

        qs('trace-test').innerHTML = [
            traceField('Trace ID', data.trace_id),
            traceField('状态版本', data.state_version),
            traceField('恢复次数', data.recovery_count),
            traceField('Cluster Job', data.cluster_job_id),
            traceField('Attempt', data.attempt_id),
            traceField('任务状态', data.cluster_job?.status),
            traceField('Worker', data.cluster_job?.assigned_worker_id),
        ].join('') || '<div class="muted">尚未创建集群测试任务。</div>';

        const summary = data.result_summary || {};
        const reportRows = [
            traceField('报告时间', compactTime(data.report_timestamp)),
            traceField('报告 ID', data.report_id),
            ...Object.entries(summary).slice(0, 8).map(([k, v]) => traceField(k, typeof v === 'object' ? JSON.stringify(v) : v)),
        ].join('');
        qs('trace-report').innerHTML = reportRows
            + (data.report_timestamp ? `<div class="trace-link-row"><button type="button" onclick="closeTrace(); openRunReport(event, '${esc(runId)}')">查看完整报告</button></div>` : '')
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
    window.atsTraceFocusOrigin = document.activeElement;
    qs('ats-trace-drawer').classList.add('open');
    qs('ats-trace-backdrop').classList.add('open');
    qs('ats-trace-drawer').setAttribute('aria-hidden', 'false');
    syncAutomationOverlayState();
    qs('ats-trace-drawer').querySelector('button')?.focus({preventScroll: true});
}
function closeTrace() {
    qs('ats-trace-drawer').classList.remove('open');
    qs('ats-trace-backdrop').classList.remove('open');
    qs('ats-trace-drawer').setAttribute('aria-hidden', 'true');
    syncAutomationOverlayState();
    if (window.atsTraceFocusOrigin && window.atsTraceFocusOrigin.isConnected) {
        window.atsTraceFocusOrigin.focus({preventScroll: true});
    }
    window.atsTraceFocusOrigin = null;
}
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && qs('ats-trace-drawer')?.classList.contains('open')) {
        event.preventDefault();
        event.stopPropagation();
        closeTrace();
    }
});

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
        await applyAutomationWorkspaceContext(atsWorkspaceContext);
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

document.addEventListener('DOMContentLoaded', () => {
    document.body.dataset.automationReady = 'true';
    loadAll(true);
});
