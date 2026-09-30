// Automation page state chunk; loaded by page.html in dependency order.
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
let atsLocalWorkerId = String(
    (window.__GMS_BOOTSTRAP__ && window.__GMS_BOOTSTRAP__.localWorkerId)
    || 'ats-worker-controller'
);
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
    if (normalized === 'GTS-ROOT' || normalized === 'APTS') return 'GTS';
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
