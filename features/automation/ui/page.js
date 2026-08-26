const AUTOMATION_WORKFLOW_STORAGE_KEY = 'gms_automation_workflow_pane';
const AUTOMATION_STATUS_STORAGE_KEY = 'gms_automation_status_filter';
const AUTOMATION_WORKFLOW_PANES = new Set(['overview', 'create', 'runs', 'build', 'events', 'reports']);
const AUTOMATION_STATUS_FILTERS = new Set(['', 'queued', 'testing', 'completed']);

function restoreAutomationViewValue(queryKey, storageKey, allowed, fallback) {
    let value = new URLSearchParams(window.location.search).get(queryKey) || '';
    if (!allowed.has(value)) {
        try { value = window.sessionStorage.getItem(storageKey) || ''; } catch (_error) { value = ''; }
    }
    return allowed.has(value) ? value : fallback;
}

let atsProfiles = [];
let atsRuns = [];
let atsStatus = restoreAutomationViewValue(
    'status', AUTOMATION_STATUS_STORAGE_KEY, AUTOMATION_STATUS_FILTERS, ''
);
let selectedRunId = '';
let buildServers = [];
let buildTemplates = [];
let buildJobs = [];
let buildPasswordCache = {};
let testSuites = [];
let connectedDevices = [];
let selectedBuildJobId = '';
let activeWorkflowPane = restoreAutomationViewValue(
    'tab', AUTOMATION_WORKFLOW_STORAGE_KEY, AUTOMATION_WORKFLOW_PANES, 'overview'
);
let toastTimer = null;
let atsWorkspaceContext = {};
let applyingWorkspaceContext = false;
let atsLocalWorkerId = 'ats-worker-controller';
let pendingBuildWorkspace = '';
let pendingBuildLunchTarget = '';
let workspaceDiscoveryRequest = 0;
let lunchDiscoveryRequest = 0;
let lunchOptionsContext = '';
let atsTimelineEvents = [];
let selectedRunTrace = null;
let buildLogRaw = '';
let lastPreflightSignature = '';
let lastPreflightData = null;

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
        updateStepIndicators();
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
            if (worker.status === 'draining') return '停止派发';
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
const STATUS_LABELS_ZH = {
    online: '在线', offline: '离线', busy: '忙碌', draining: '停止派发',
    available: '可用', allocated: '已分配', reserved: '已预留',
    external_busy: '外部占用', unauthorized: '未授权', unknown: '未知',
    fastboot: 'Fastboot',
    created: '已创建', queued: '排队', running: '运行中',
    jenkins_queued: '构建排队',
    jenkins_building: '编译中', artifact_ready: '固件就绪',
    waiting_device: '等待设备', device_locked: '设备已锁',
    flashing: '刷机中', flash_verified: '刷机校验',
    testing: '启动测试', test_running: '测试中',
    report_collecting: '收集报告', analyzing: '分析中',
    reporting: '上报中', completed: '完成', cancelled: '已取消',
    failed: '失败', jenkins_failed: '构建失败',
    artifact_missing: '固件缺失', flash_failed: '刷机失败',
    test_failed: '测试失败', analysis_failed: '分析失败',
    reporting_failed: '上报失败',
};
const TEST_TYPE_OPTIONS = ['CTS', 'GSI', 'GTS', 'GTS-ROOT', 'STS', 'VTS', 'APTS'];

function suiteTypeForTest(testType) {
    const normalized = String(testType || '').trim().toUpperCase();
    if (normalized === 'GSI') return 'CTS';
    if (normalized === 'GTS-ROOT') return 'GTS';
    return normalized;
}

function suiteVersionParts(value) {
    const text = String(value || '');
    const match = text.match(/(\d+(?:\.\d+)*)(?:[_-][rR](\d+))?/);
    return {
        main: match ? match[1].split('.').map(Number) : [0],
        revision: match ? Number(match[2] || 0) : 0,
    };
}

function compareSuitesNewest(first, second) {
    const a = suiteVersionParts(first.version || first.suite_version || first.tools_path);
    const b = suiteVersionParts(second.version || second.suite_version || second.tools_path);
    const length = Math.max(a.main.length, b.main.length);
    for (let index = 0; index < length; index += 1) {
        const difference = (b.main[index] || 0) - (a.main[index] || 0);
        if (difference) return difference;
    }
    return b.revision - a.revision;
}

function qs(id) { return document.getElementById(id); }
function statusLabel(value) {
    const status = String(value || 'unknown');
    return STATUS_LABELS_ZH[status] || status;
}
function syncAutomationOverlayState() {
    const hasOverlay = Boolean(
        document.querySelector('.password-backdrop')
        || qs('ats-trace-drawer')?.classList.contains('open')
    );
    document.body.classList.toggle('overlay-open', hasOverlay);
}
const esc = escapeHtml;
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
function openRunAnalysis(event, runId) {
    event?.stopPropagation?.();
    const run = atsRuns.find(item => item.id === runId);
    if (!run?.report_timestamp) return;
    syncAutomationWorkspaceSelection({
        worker_id: run.worker_id || atsLocalWorkerId,
        cluster_job_id: run.cluster_job_id || '',
        attempt_id: run.attempt_id || '',
        automation_run_id: run.id,
        report_id: run.report_id || '',
        report_timestamp: run.report_timestamp,
        origin_page: 'automation',
    });
    if (typeof window.parent?.analyzeReport === 'function') {
        window.parent.analyzeReport(run.report_timestamp, run.report_id || '');
        return;
    }
    window.GmsEmbeddedWorkspace?.navigate('report-analysis', {
        automation_run_id: run.id,
        report_id: run.report_id || '',
        report_timestamp: run.report_timestamp,
        origin_page: 'automation',
    });
}

function downloadText(filename, content) {
    const blob = new Blob([String(content || '')], {type: 'text/plain;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

async function copyText(content, successMessage) {
    const value = String(content || '');
    if (!value) throw new Error('当前没有可复制的日志');
    if (navigator.clipboard?.writeText) {
        try {
            await navigator.clipboard.writeText(value);
            toast(successMessage);
            return;
        } catch (_) { /* 非安全上下文时回退到隐藏 textarea */ }
    }
    const input = document.createElement('textarea');
    input.value = value;
    input.style.position = 'fixed';
    input.style.opacity = '0';
    document.body.appendChild(input);
    input.select();
    const copied = document.execCommand('copy');
    input.remove();
    if (!copied) throw new Error('浏览器未允许复制，请使用日志下载');
    toast(successMessage);
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
async function api(path, options, retried = false) {
    const resp = await fetch(path, options);
    const text = await resp.text();
    let data;
    try {
        data = text ? JSON.parse(text) : {};
    } catch (_error) {
        data = {success: false, error: text || `HTTP ${resp.status}`};
    }
    const detail = data && data.detail;
    if (
        resp.status === 403
        && !retried
        && detail
        && typeof detail === 'object'
        && detail.elevation_required
        && typeof window.parent?.requestElevatedAccess === 'function'
    ) {
        const granted = await window.parent.requestElevatedAccess(
            '执行 GMS ATS 管理操作'
        );
        if (granted) return api(path, options, true);
    }
    if (!resp.ok || !data.success) {
        const message = typeof detail === 'object'
            ? (detail.message || JSON.stringify(detail))
            : (detail || data.error || data.message);
        throw new Error(message || `请求失败 (HTTP ${resp.status})`);
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
    const flashPlan = profile.flash || testPlan.flash || {};
    setSelectValue('automation-flash-mode', flashPlan.mode || 'firmware');
    setSelectValue('automation-test-type', testPlan.test_type);
    renderSuiteOptions(testPlan.test_suite);
    qs('automation-test-module').value = testPlan.test_module || (testPlan.modules || [])[0] || '';
    handleFlashModeChange({invalidate: false});
    invalidateRunPreflight();
    updateStepIndicators();
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
    qs('build-template').onchange = () => {
        applyBuildTemplateDefaults();
        invalidateRunPreflight();
        updateStepIndicators();
    };
    qs('build-workspace').onchange = handleBuildWorkspaceChange;
    qs('build-command').onchange = () => {
        syncBuildCommandTitle();
        invalidateRunPreflight();
    };
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
    const enabled = currentFlashMode() !== 'skip'
        && !Boolean(qs('automation-artifact')?.value.trim());
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
    updateStepIndicators();
}

function syncArtifactMode() {
    const artifact = qs('automation-artifact')?.value.trim() || '';
    const hint = qs('artifact-mode-hint');
    if (hint) {
        hint.textContent = artifact
            ? '本次运行将直接使用已有固件，源码编译参数已隐藏'
            : '留空时按下方参数从源码编译；填写后直接使用该固件';
        hint.classList.toggle('ready', Boolean(artifact));
    }
    const buildSection = qs('build-config-section');
    if (buildSection) buildSection.hidden = Boolean(artifact);
    syncBuildSectionState();
    invalidateRunPreflight();
    updateStepIndicators();
}

function currentFlashMode() {
    return qs('automation-flash-mode')?.value || 'firmware';
}

function handleFlashModeChange({invalidate = true} = {}) {
    const skip = currentFlashMode() === 'skip';
    const hint = qs('flash-mode-hint');
    if (hint) {
        hint.textContent = skip
            ? '仅测试不会编译或烧写固件，可选择多台设备'
            : '烧写模式只允许选择 1 台本地 USB / USB-IP 设备';
        hint.classList.toggle('ready', skip);
    }
    const artifact = qs('automation-artifact');
    if (artifact) artifact.disabled = skip;
    const buildSection = qs('build-config-section');
    if (buildSection) buildSection.hidden = skip || Boolean(artifact?.value.trim());
    const label = qs('device-selection-label');
    if (label) label.textContent = skip ? '目标设备（可多选）' : '目标设备（烧写模式限选 1 台）';
    document.querySelectorAll('#automation-device-list input[type="checkbox"]').forEach(input => {
        const baseDisabled = input.dataset.baseDisabled === 'true';
        const flashUnsupported = !skip && input.dataset.transport === 'adb_proxy';
        input.disabled = baseDisabled || flashUnsupported;
        input.closest('.checkbox-item')?.classList.toggle('muted', input.disabled);
        if (input.disabled) input.checked = false;
    });
    if (!skip) {
        const checked = Array.from(document.querySelectorAll(
            '#automation-device-list input[type="checkbox"]:checked'
        ));
        checked.slice(1).forEach(input => { input.checked = false; });
    }
    syncBuildSectionState();
    syncAutomationWorkspaceSelection();
    if (invalidate) invalidateRunPreflight();
    updateStepIndicators();
}

function handleDeviceSelection(input) {
    if (currentFlashMode() !== 'skip' && input?.checked) {
        document.querySelectorAll('#automation-device-list input[type="checkbox"]:checked')
            .forEach(item => { if (item !== input) item.checked = false; });
    }
    syncAutomationWorkspaceSelection();
    invalidateRunPreflight();
    updateStepIndicators();
}

function runFormSignature() {
    const checked = Array.from(document.querySelectorAll(
        '#automation-device-list input[type="checkbox"]:checked'
    )).map(input => input.value);
    return JSON.stringify({
        profile: qs('automation-profile')?.value || '',
        flash: currentFlashMode(), artifact: qs('automation-artifact')?.value.trim() || '',
        server: qs('build-server')?.value || '', template: qs('build-template')?.value || '',
        workspace: qs('build-workspace')?.value || '', lunch: qs('build-lunch-target')?.value || '',
        command: qs('build-command')?.value || '', worker: selectedWorkerId(), devices: checked,
        type: qs('automation-test-type')?.value || '', suite: qs('automation-test-suite')?.value || '',
        module: qs('automation-test-module')?.value.trim() || '', extra: qs('automation-test-plan')?.value.trim() || '',
    });
}

function invalidateRunPreflight(message = '参数已变更，请重新预检') {
    if (!lastPreflightSignature && !lastPreflightData) return;
    lastPreflightSignature = '';
    lastPreflightData = null;
    const result = qs('automation-preflight');
    if (result) {
        result.className = 'preflight-result idle';
        result.innerHTML = `<strong>需要重新预检</strong><span>${esc(message)}</span>`;
    }
    updateStepIndicators();
}

function updateStepIndicators() {
    const step1 = qs('step-1');
    const step2 = qs('step-2');
    const step3 = qs('step-3');
    if (step1) step1.classList.toggle('done', Boolean(qs('automation-profile')?.options.length));
    if (step2) {
        const hasArtifact = Boolean(qs('automation-artifact')?.value.trim());
        const hasBuildParams = qs('build-server')?.value && qs('build-template')?.value
            && qs('build-workspace')?.value && qs('build-lunch-target')?.value;
        step2.classList.toggle('done', currentFlashMode() === 'skip' || hasArtifact || Boolean(hasBuildParams));
    }
    if (step3) step3.classList.toggle('done', Boolean(selectedWorkerId() && qs('automation-test-type')?.value));
    const step4 = qs('step-4');
    if (step4) step4.classList.toggle('done', Boolean(
        lastPreflightData?.ready && lastPreflightSignature === runFormSignature()
    ));
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
    updateStepIndicators();
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
    updateStepIndicators();
}

function invalidateLunchOptions(message = '选择源码目录后自动读取该目录的 Lunch Target') {
    lunchDiscoveryRequest += 1;
    lunchOptionsContext = '';
    setBuildControlBusy('build-lunch-refresh', false);
    renderLunchOptions([]);
    setBuildFieldStatus('build-lunch-status', message);
}

async function handleBuildServerChange() {
    invalidateRunPreflight();
    workspaceDiscoveryRequest += 1;
    renderBuildTemplates();
    applyBuildTemplateDefaults();
    renderBuildWorkspaces([]);
    invalidateLunchOptions();
    setBuildFieldStatus('build-workspace-status', '正在扫描所选服务器的源码目录…', 'loading');
    await refreshBuildWorkspaces();
}

async function handleBuildWorkspaceChange() {
    invalidateRunPreflight();
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
                return `<label class="checkbox-item${unavailable || flashUnsupported ? ' muted' : ''}"><input type="checkbox" value="${esc(id)}" data-transport="${esc(transport)}" data-base-disabled="${unavailable ? 'true' : 'false'}"${unavailable || flashUnsupported ? ' disabled' : ''} onchange="handleDeviceSelection(this)"> <span>${esc(label)}</span></label>`;
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

function renderSuiteOptions(preferredSuite = '') {
    const select = qs('automation-test-suite');
    const grouped = {};
    testSuites.forEach(suite => {
        const groupType = String(suite.test_type || '').trim().toUpperCase();
        if (!groupType) return;
        if (!grouped[groupType]) grouped[groupType] = [];
        grouped[groupType].push(suite);
    });
    const groups = Object.keys(grouped).sort().map(type => {
        const options = grouped[type].sort(compareSuitesNewest).map(suite => {
            const path = suite.tools_path || suite.full_path || '';
            return `<option value="${esc(path)}">${esc(path)}</option>`;
        }).join('');
        return `<optgroup label="${esc(type)}">${options}</optgroup>`;
    }).join('');
    select.innerHTML = groups || '<option value="" disabled>当前 Worker 暂无测试套件</option>';

    const preferred = String(preferredSuite || '');
    const hasPreferred = preferred && Array.from(select.options).some(option => option.value === preferred);
    if (hasPreferred) {
        select.value = preferred;
    } else {
        const suiteType = suiteTypeForTest(qs('automation-test-type').value);
        const latest = testSuites
            .filter(suite => String(suite.test_type || '').trim().toUpperCase() === suiteType)
            .sort(compareSuitesNewest)[0];
        select.value = latest ? (latest.tools_path || latest.full_path || '') : '';
    }
    invalidateRunPreflight();
    syncAutomationWorkspaceSelection();
    updateStepIndicators();
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
                    <span class="badge ${esc(run.status)}" title="${esc(run.status)}">${esc(statusLabel(run.status))}</span>
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
                        <div class="run-value">${run.report_timestamp ? `<button type="button" onclick="openRunAnalysis(event, '${esc(run.id)}')">分析报告</button>` : '<span class="muted">-</span>'}</div>
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
                <button type="button" onclick="event.stopPropagation(); loadEvents('${esc(run.id)}')">日志</button>
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
                            <span class="badge ${esc(run.status)}" title="${esc(run.status)}">${esc(statusLabel(run.status))}</span>
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
                    <div class="report-card-actions">
                        <button type="button" onclick="openRunReport(event, '${esc(run.id)}')">报告详情</button>
                        <button type="button" class="primary" onclick="openRunAnalysis(event, '${esc(run.id)}')">分析报告</button>
                    </div>
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
    const traceButton = qs('events-trace-button');
    if (traceButton) traceButton.disabled = false;
    qs('automation-runs').querySelectorAll('.run-card').forEach(el => el.classList.remove('active'));
    const card = qs('automation-runs').querySelector(`.run-card[onclick="loadEvents('${runId}')"]`);
    if (card) card.classList.add('active');
    const [data, trace] = await Promise.all([
        api(`/api/automation/runs/${encodeURIComponent(runId)}/timeline`),
        api(`/api/automation/runs/${encodeURIComponent(runId)}/trace`).catch(() => null),
    ]);
    atsTimelineEvents = data.items || [];
    selectedRunTrace = trace;
    await renderRunLogLinks(trace);
    renderEventLog();
    switchWorkflowPane('events');
}

function eventLevelClass(event) {
    const level = String(event.level || 'info').toLowerCase();
    if (level.includes('error') || level.includes('fail')) return 'error';
    if (level.includes('warn')) return 'warning';
    return 'info';
}

function eventLogLine(event) {
    const stage = event.stage || event.to_state || event.event_type || '-';
    const detail = [
        event.from_state && event.to_state ? `${event.from_state} -> ${event.to_state}` : '',
        event.operation_id ? `op=${event.operation_id}` : '',
    ].filter(Boolean).join(' ');
    return `${compactTime(event.created_at)} ${eventLevelClass(event).toUpperCase().padEnd(5)} ${String(event.domain || 'automation').padEnd(10)} ${String(stage).padEnd(20)} ${event.message || ''}${detail ? ` | ${detail}` : ''}`;
}

function filteredTimelineEvents() {
    const query = String(qs('events-search')?.value || '').trim().toLowerCase();
    const level = qs('events-level')?.value || '';
    return atsTimelineEvents.filter(event => {
        if (level && eventLevelClass(event) !== level) return false;
        return !query || eventLogLine(event).toLowerCase().includes(query);
    });
}

function renderEventLog() {
    const target = qs('automation-events');
    if (!target) return;
    const events = filteredTimelineEvents();
    const autoFollow = qs('events-auto-follow')?.checked !== false;
    const wasNearBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 48;
    const previousTop = target.scrollTop;
    target.classList.remove('muted');
    target.innerHTML = events.length ? events.map(event => {
        const level = eventLevelClass(event);
        const stage = event.stage || event.to_state || event.event_type || '-';
        const detail = [
            event.from_state && event.to_state ? `${event.from_state} → ${event.to_state}` : '',
            event.operation_id ? `op=${event.operation_id}` : '',
        ].filter(Boolean).join(' · ');
        return `<div class="event-line">
            <span class="event-line-time">${esc(compactTime(event.created_at))}</span>
            <span class="event-line-level ${level}">${esc(level === 'warning' ? 'WARN' : level.toUpperCase())}</span>
            <span class="event-line-domain">${esc(event.domain || 'automation')}</span>
            <span class="event-line-stage">${esc(stage)}</span>
            <span class="event-line-message">${esc(event.message || '-')}${detail ? ` <span class="event-line-detail">| ${esc(detail)}</span>` : ''}</span>
        </div>`;
    }).join('') : '<div class="event-log-empty">没有匹配当前筛选条件的日志。</div>';
    if (autoFollow && (wasNearBottom || !target.dataset.loaded)) {
        requestAnimationFrame(() => { target.scrollTop = target.scrollHeight; });
    } else {
        target.scrollTop = previousTop;
    }
    target.dataset.loaded = '1';
}

async function renderRunLogLinks(trace) {
    const target = qs('run-log-links');
    if (!target) return;
    const links = [];
    if (trace?.build_job_id) {
        links.push(`<button type="button" onclick="jumpToBuildLog('${esc(trace.build_job_id)}')">构建原始日志</button>`);
    }
    if (trace?.cluster_job_id) {
        try {
            const response = await fetch(`/api/cluster/jobs/${encodeURIComponent(trace.cluster_job_id)}/artifacts`, {cache: 'no-store'});
            const payload = await response.json();
            if (response.ok && payload.success !== false) {
                (payload.artifacts || []).filter(item => ['stdout.log', 'stderr.log'].includes(item.filename)).forEach(item => {
                    const url = `/api/cluster/jobs/${encodeURIComponent(trace.cluster_job_id)}/artifacts/${encodeURIComponent(item.id)}/download`;
                    links.push(`<a class="log-artifact-link" href="${url}">${esc(item.filename)}</a>`);
                });
            }
        } catch (_) { /* 日志产物入口是增强信息，不阻断时间线 */ }
    }
    target.innerHTML = links.length ? links.join('') : '时间 · 级别 · 来源 · 阶段 · 消息';
}

function copyEventLog() {
    copyText(filteredTimelineEvents().map(eventLogLine).join('\n'), '运行日志已复制')
        .catch(error => toast(error.message));
}

function downloadEventLog() {
    const content = filteredTimelineEvents().map(eventLogLine).join('\n');
    if (!content) { toast('当前没有可下载的日志'); return; }
    downloadText(`${selectedRunId || 'ats-run'}.log`, content);
}

async function refreshSelectedEvents() {
    if (!selectedRunId) { toast('请先选择一条运行。'); return; }
    await loadEvents(selectedRunId);
}

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

async function loadBuildJobs() {
    const data = await api('/api/build/jobs?limit=20');
    buildJobs = data.items || [];
    qs('build-jobs').innerHTML = buildJobs.length
        ? buildJobs.map(job => {
            const terminal = TERMINAL_STATUSES.has(job.status);
            return `<div class="build-job ${job.id === selectedBuildJobId ? 'active' : ''}" onclick="loadBuildLog('${esc(job.id)}')">
                <div class="build-job-head"><span class="badge ${esc(job.status)}" title="${esc(job.status)}">${esc(statusLabel(job.status))}</span><strong>${esc(job.template_id)}</strong>
                ${terminal ? `<button type="button" class="build-job-delete" title="删除历史任务" onclick="event.stopPropagation(); deleteBuildJob('${esc(job.id)}')">删除</button>` : ''}</div>
                <div class="muted">${esc(job.id)} / ${esc(job.remote_workspace || '')}</div>
                <div class="build-job-source">${job.automation_run_id ? `ATS ${esc(job.automation_run_id)}` : '独立调试构建'}</div>
                <div>${esc((job.artifacts || [])[0]?.path || job.error || '')}</div></div>`;
        }).join('')
        : '<div class="muted">暂无构建任务。</div>';
}

async function deleteBuildJob(jobId) {
    const confirmed = typeof window.parent?.showConfirmDialog === 'function'
        ? await window.parent.showConfirmDialog(
            '删除构建任务',
            `确定删除历史构建任务 ${jobId}？\n此操作只删除平台记录，不删除远端源码和构建产物。`
        )
        : window.confirm(`确定删除历史构建任务 ${jobId}？`);
    if (!confirmed) return;
    try {
        await api(`/api/build/jobs/${encodeURIComponent(jobId)}`, {method: 'DELETE'});
        if (selectedBuildJobId === jobId) {
            selectedBuildJobId = '';
            buildLogRaw = '';
            qs('build-log-title').textContent = '未选择任务';
            qs('build-log').textContent = '选择构建任务查看日志。';
        }
        toast(`已删除历史构建任务 ${jobId}`);
        await loadBuildJobs();
    } catch (err) { toast(err.message); }
}

async function compileAndJump() {
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
        toast(`已创建独立构建任务 ${job.id}；该任务不会自动进入烧写和测试`);
        switchWorkflowPane('build');
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
        buildLogRaw = log.text || '';
        renderBuildLog();
        qs('build-log-title').textContent = `${job.id} / ${statusLabel(job.status)}`;
        if (!silent) toast(`构建任务 ${job.id}：${statusLabel(job.status)}`);
        await loadBuildJobs();
    } catch (err) { toast(err.message); }
}

function filteredBuildLog() {
    const query = String(qs('build-log-search')?.value || '').trim().toLowerCase();
    if (!query) return buildLogRaw;
    return buildLogRaw.split('\n').filter(line => line.toLowerCase().includes(query)).join('\n');
}

function renderBuildLog() {
    const logEl = qs('build-log');
    if (!logEl) return;
    const autoFollow = qs('build-log-auto-follow')?.checked !== false;
    const wasNearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 48;
    const previousTop = logEl.scrollTop;
    logEl.textContent = filteredBuildLog() || (buildLogRaw ? '没有匹配的日志。' : '暂无日志。');
    if (autoFollow && (wasNearBottom || !logEl.dataset.loaded)) {
        requestAnimationFrame(() => { logEl.scrollTop = logEl.scrollHeight; });
    } else {
        logEl.scrollTop = previousTop;
    }
    logEl.dataset.loaded = '1';
}

function copyBuildLog() {
    copyText(filteredBuildLog(), '构建日志已复制').catch(error => toast(error.message));
}

function downloadBuildLog() {
    const content = filteredBuildLog();
    if (!content) { toast('当前没有可下载的日志'); return; }
    downloadText(`${selectedBuildJobId || 'build'}.log`, content);
}

function refreshSelectedBuildLog() {
    if (!selectedBuildJobId) {
        toast('请先选择一个构建任务');
        return;
    }
    loadBuildLog(selectedBuildJobId).catch(err => toast(err.message));
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
            '',
            '确认创建持久化 ATS Run 并启动一条龙流程？',
        ].join('\n');
        const confirmed = typeof window.parent?.showConfirmDialog === 'function'
            ? await window.parent.showConfirmDialog('启动 GMS ATS 流水线', message)
            : window.confirm(message);
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
        toast(run
            ? `推进到 ${statusLabel(run.status)}：${run.id}`
            : '没有可推进的运行');
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
            ? entries.map(([s, c]) => `<div class="breakdown-row"><span class="badge ${esc(s)}" title="${esc(s)}">${esc(statusLabel(s))}</span><span class="breakdown-count">${esc(c)}</span></div>`).join('')
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
            `<div class="trace-key">状态</div><div class="trace-val"><span class="badge ${esc(build.status)}" title="${esc(build.status)}">${esc(statusLabel(build.status))}</span></div>`,
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
            + (data.report_timestamp ? `<div class="trace-link-row"><button type="button" onclick="closeTrace(); openRunReport(event, '${esc(runId)}')">报告详情</button> <button type="button" class="primary" onclick="closeTrace(); openRunAnalysis(event, '${esc(runId)}')">分析报告</button></div>` : '')
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

function setStatusFilter(status, {persist = true, load = true} = {}) {
    const target = AUTOMATION_STATUS_FILTERS.has(status) ? status : '';
    atsStatus = target;
    document.querySelectorAll('.subtabs .tab').forEach(btn => btn.classList.toggle('active', btn.dataset.status === target));
    if (persist) {
        try { window.sessionStorage.setItem(AUTOMATION_STATUS_STORAGE_KEY, target); } catch (_error) {}
        const url = new URL(window.location.href);
        if (target) url.searchParams.set('status', target);
        else url.searchParams.delete('status');
        window.history.replaceState({}, '', url.toString());
    }
    if (load) loadRuns().catch(err => toast(err.message));
}

// page.html 先绘制默认骨架；脚本加载后、首批 API 请求前恢复当前标签页状态，
// 避免刷新时先触发错误面板的额外请求或覆盖已保存筛选。
switchWorkflowPane(activeWorkflowPane, {persist: false, load: false});
setStatusFilter(atsStatus, {persist: false, load: false});

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

// 统一定时刷新：用户正在编辑输入时跳过本轮，避免打断。
// 页面隐藏时暂停请求，重新进入后立即追平一次状态。
let automationRefreshInterval = null;
let automationRefreshStarted = false;
let automationRefreshPromise = null;
async function refreshAutomationActivity() {
    if (automationRefreshPromise) return automationRefreshPromise;
    automationRefreshPromise = refreshAutomationActivityOnce().finally(() => {
        automationRefreshPromise = null;
    });
    return automationRefreshPromise;
}

async function refreshAutomationActivityOnce() {
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
}

function syncAutomationAutoRefresh(event) {
    const visible = event?.detail?.visible
        ?? window.GmsEmbeddedWorkspace?.isVisible?.()
        ?? true;
    if (automationRefreshInterval) {
        clearInterval(automationRefreshInterval);
        automationRefreshInterval = null;
    }
    if (!visible) return;
    if (automationRefreshStarted) refreshAutomationActivity();
    automationRefreshStarted = true;
    automationRefreshInterval = setInterval(refreshAutomationActivity, 8000);
}
window.addEventListener('gms:embedded-visibility', syncAutomationAutoRefresh);
syncAutomationAutoRefresh();

document.addEventListener('DOMContentLoaded', () => {
    document.body.dataset.automationReady = 'true';
    loadAll(true).finally(() => window.GmsEmbeddedWorkspace?.markReady());
});
