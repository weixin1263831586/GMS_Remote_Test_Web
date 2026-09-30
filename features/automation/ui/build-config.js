// Automation page build-config chunk; loaded by page.html in dependency order.
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
