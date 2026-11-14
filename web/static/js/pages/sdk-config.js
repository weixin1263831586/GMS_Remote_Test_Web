// ==================== SDK 配置（用户管理页第三个页签） ====================
// 管理 Agent 只读源码检索的数据源绑定。逻辑自 agent-access.js 拆出
// （2026-11-14 三页签整理）：Agent 接入页签管接入凭据，本页签管 SDK 源。
// 列表 = GET /api/sdk/sources（registry 全量，sdk.read）叠加
// GET /api/sdk/manage/sources（用户注册源 + 每次刷新的远端路径校验，
// human-only）。管理面（probe/添加/删除）走 /api/sdk/manage/*：
// 读需登录人类，写需管理员提权（服务端 require_elevated_admin 强制）。
// repo_root 永不出现在只读列表里；SSH 口令任何响应都不回传。

let agentSdkSourcesLoaded = false;
let agentSdkSourcesSequence = 0;
let agentSdkSourcesCache = [];
let agentSdkUserLiveCache = {};

const AGENT_SDK_PROVIDER_LABELS = {
    local_git: '本机 Git 仓库',
    ssh_git: '编译服务器（SSH 只读）',
    codesearch: 'OpenGrok 代码索引',
};

function sdkConfigPanelIsOpen() {
    const panel = document.getElementById('sdk-config-panel');
    if (!panel) return false;
    if (panel.style.display === 'flex') return true;
    return getComputedStyle(panel).display !== 'none';
}

async function sdkConfigToggle(show) {
    const target = (show === undefined ? !sdkConfigPanelIsOpen() : Boolean(show)) ? 'sdk' : 'list';
    return usersViewSwitch(target);
}

async function sdkConfigLoadSources(button) {
    const tbody = document.getElementById('agent-sdk-sources-tbody');
    if (!tbody || (button && button.disabled)) return;
    if (agentSdkSourcesLoaded && !button) return; // 打开页签时自动加载一次
    const sequence = ++agentSdkSourcesSequence;
    if (button) {
        button.disabled = true;
        const label = button.lastElementChild?.nextSibling;
        if (label) label.textContent = ' 刷新中…';
    }
    try {
        // 「刷新」按钮（button 非空）走 sync 强校验；页签自动加载走
        // auto（fresh 缓存直接显示，stale/pending 转后台校验不阻塞）。
        const verifyMode = button ? 'sync' : 'auto';
        const [registryResp, manageResp] = await Promise.allSettled([
            apiCall('/api/sdk/sources', 'GET', null, {silentToast: true}),
            apiCall(`/api/sdk/manage/sources?verify=${verifyMode}`, 'GET', null, {silentToast: true}),
        ]);
        if (sequence !== agentSdkSourcesSequence) return;
        if (registryResp.status === 'rejected') throw registryResp.reason;
        const sources = Array.isArray(registryResp?.value?.data?.sources)
            ? registryResp.value.data.sources : [];
        agentSdkUserLiveCache = {};
        if (manageResp.status === 'fulfilled') {
            (manageResp.value?.data?.sources || []).forEach(source => {
                agentSdkUserLiveCache[source.source_id] = source;
            });
        }
        // manage 面不可用（如权限不足）时只降级为无校验列，不阻塞展示。
        agentSdkSourcesCache = sources;
        sdkConfigRenderSources(tbody, sources);
        agentSdkSourcesLoaded = true;
    } catch (error) {
        if (sequence !== agentSdkSourcesSequence) return;
        tbody.innerHTML = `<tr><td colspan="7" class="agent-access-empty">加载失败：${agentAccessEscape(error.message || '需要 sdk.read 权限')}</td></tr>`;
    } finally {
        if (sequence === agentSdkSourcesSequence && button) {
            button.disabled = false;
            const label = button.lastElementChild?.nextSibling;
            if (label) label.textContent = ' 刷新';
        }
    }
}

// ---------- 顶部统计栏（模板与用户列表/Agent 接入一致） ----------

// 版本卡排序：Android<数字> 按数字升序；其余（含"未识别"）排最后。
function sdkConfigVersionSortKey(version) {
    const match = /^(?:Android|android)[-_. ]?(\d+)$/.exec(version);
    if (match) return [0, Number(match[1])];
    if (/^\d+$/.test(version)) return [0, Number(version)]; // 兼容纯数字写法
    return [1, version]; // "未识别" 等排最后
}

function sdkConfigRenderStats(sources) {
    const row = document.getElementById('agent-sdk-stat-row');
    if (!row) return;
    const total = sources.length;
    // 按 android_version 分组计数；空值归「未识别」。
    const counts = new Map();
    sources.forEach(source => {
        const version = (source.android_version || '').trim() || '未识别';
        counts.set(version, (counts.get(version) || 0) + 1);
    });
    const versions = Array.from(counts.entries())
        .sort((a, b) => {
            const [ga, va] = sdkConfigVersionSortKey(a[0]);
            const [gb, vb] = sdkConfigVersionSortKey(b[0]);
            return ga !== gb ? ga - gb : va - vb;
        });
    const card = (icon, tone, value, label) => `
        <div class="page-stat-card workbench-stat">
            <span class="workbench-stat-icon" aria-hidden="true"><svg class="ui-icon" focusable="false"><use href="/static/icons/ui.svg#${icon}"></use></svg></span><span class="workbench-stat-copy">
            <span class="page-stat-value ${tone}">${value}</span>
            <span class="page-stat-label">${label}</span>
        </span></div>`;
    row.innerHTML = card('users', 'tone-primary', total, '全部 SDK 源')
        + versions.map(([version, count]) =>
            card('test', version === '未识别' ? 'tone-warning' : 'tone-success', count, version === '未识别' ? '未识别版本' : version)
        ).join('');
}

function sdkConfigRenderSources(tbody, sources) {
    sdkConfigRenderStats(sources);
    const count = document.getElementById('agent-sdk-sources-count');
    if (count) {
        count.textContent = sources.length
            ? `分析 Redmine 问题时的只读源码取证来源，共 ${sources.length} 个（repo_root 不对外暴露）。`
            : '尚未配置任何 SDK 源；点击「添加SDK」绑定编译服务器目录，或由管理员写入部署配置。';
    }
    if (!sources.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="agent-access-empty">未配置 SDK 源。点击右上角「＋ 添加SDK」从编译服务器选择 SDK 根目录绑定。</td></tr>';
        return;
    }
    tbody.innerHTML = sources.map(source => {
        const providerLabel = AGENT_SDK_PROVIDER_LABELS[source.provider] || agentAccessEscape(source.provider);
        let location = '—';
        if (source.provider === 'ssh_git') {
            location = `${agentAccessEscape(source.ssh_username || '')}@${agentAccessEscape(source.ssh_host || '')}:${source.ssh_port ?? 22}`;
        } else if (source.provider === 'codesearch') {
            location = 'OpenGrok 服务（管理员配置）';
        } else {
            location = '本机仓库（路径不对外暴露）';
        }
        let mode = '—';
        if (source.provider === 'ssh_git') {
            mode = source.discover_repos
                ? '全量自动发现（TTL 缓存）'
                : (source.multi_repo ? '多仓清单' : '单仓');
        } else if (source.provider === 'codesearch') {
            mode = '动态索引';
        } else {
            mode = 'commit 绑定';
        }
        const revision = source.default_revision
            ? agentAccessEscape(source.default_revision)
            : '<span class="agent-access-empty">未设置</span>';
        const androidVersion = source.android_version
            ? agentAccessEscape(source.android_version)
            : '<span class="agent-access-empty">—</span>';
        // 全部条目现在都是用户注册源（静态配置已迁移）：显示实时校验
        // 结果 + 删除操作。manage 面不可用时不渲染删除（避免误报）。
        const userRecord = agentSdkUserLiveCache[source.source_id];
        let live = '<span class="agent-access-empty">—</span>';
        let action = '<span class="agent-access-empty">—</span>';
        if (userRecord) {
            const state = userRecord.verify_state || 'stale';
            const ok = userRecord.live ? Boolean(userRecord.live.verified) : null;
            const reason = agentAccessEscape(userRecord.live?.reason || '');
            const verifiedAt = userRecord.last_verified_at
                ? agentAccessEscape(userRecord.last_verified_at) : '';
            let badge;
            if (ok === null) {
                // 后台校验进行中（pending=从未校验 / stale=上次结果已过期）。
                const tip = state === 'pending'
                    ? '尚未校验，后台校验进行中；稍后刷新查看结果'
                    : `上次校验（${verifiedAt || '时间未知'}）已过期，后台校验进行中`;
                badge = `<span class="agent-access-status-badge pending" title="${tip}">… 校验中</span>`;
            } else {
                const title = ok
                    ? ' title="最近一次刷新时路径存在且含 git 仓库"'
                    : ` title="${reason}"`;
                const badgeText = ok ? '✓ 存在' : `✗ ${reason || '不可用'}`;
                badge = `<span class="agent-access-status-badge ${ok ? 'ok' : 'bad'}"${title}${verifiedAt ? ` data-verified="${verifiedAt}"` : ''}>${badgeText}</span>`;
            }
            live = badge;
            action = `<button class="btn-xxs" type="button" data-click="sdkConfigDeleteSource" data-a0="${agentAccessEscape(source.source_id)}">删除</button>`;
        }
        return `<tr>
            <td>${agentAccessEscape(source.source_id)}</td>
            <td>${providerLabel}</td>
            <td>${location}</td>
            <td>${mode}</td>
            <td>${androidVersion}</td>
            <td>${revision}</td>
            <td>${live}</td>
            <td>${action}</td>
        </tr>`;
    }).join('');
}

// ---------- 「添加SDK」弹框 ----------

function sdkConfigOnTypeChange() {
    const type = document.getElementById('agent-sdk-add-type')?.value || 'ssh_git';
    const sshOnly = document.querySelectorAll('.agent-sdk-ssh-only');
    sshOnly.forEach(el => { el.hidden = type === 'local_git'; });
    sdkConfigResetPathOptions();
}

function sdkConfigResetPathOptions() {
    const list = document.getElementById('agent-sdk-add-path-list');
    if (list) {
        list.innerHTML = '<div class="agent-sdk-path-empty">请先填写服务器并按回车「发现目录」</div>';
        list.dataset.count = '0';
    }
    const actions = document.getElementById('agent-sdk-add-path-actions');
    if (actions) actions.hidden = true;
}

function sdkConfigSelectAllPaths(all) {
    const list = document.getElementById('agent-sdk-add-path-list');
    if (!list) return;
    list.querySelectorAll('input[type="checkbox"]').forEach(box => { box.checked = Boolean(all); });
    sdkConfigRefreshPathHint();
}

function sdkConfigSelectedPaths() {
    const list = document.getElementById('agent-sdk-add-path-list');
    if (!list) return [];
    return Array.from(list.querySelectorAll('input[type="checkbox"]:checked'))
        .map(box => box.value);
}

function sdkConfigRefreshPathHint() {
    const hint = document.getElementById('agent-sdk-add-path-hint');
    if (!hint) return;
    const selected = sdkConfigSelectedPaths().length;
    const total = document.getElementById('agent-sdk-add-path-list')?.dataset.count || '0';
    hint.textContent = selected
        ? `已选 ${selected} / ${total} 条；提交时将按目录名批量注册（重名自动加 -2/-3 后缀）。`
        : '勾选要添加的 SDK 根目录（可多选批量添加）；只列出 repo 工作区与 Android 源码树。';
}

function sdkConfigOnTargetChange() {
    // 目标变化后旧候选目录失效
    sdkConfigResetPathOptions();
}

function sdkConfigOnTargetKeydown() {
    // 编译服务器输入框回车 = 自动发现目录（data-key=Enter 已过滤）。
    // data-prevent 已阻止表单默认提交；发现进行中由按钮 disabled 防抖。
    return sdkConfigDiscoverRoots();
}

function sdkConfigToggleAddSdk(force) {
    const modalId = 'agent-sdk-add-modal';
    const toggle = document.getElementById('agent-sdk-sources-add');
    const shouldOpen = force === undefined ? !ModalManager.isOpen(modalId) : Boolean(force);
    if (shouldOpen) {
        const result = document.getElementById('agent-sdk-add-probe-result');
        if (result) result.hidden = true;
        sdkConfigOnTypeChange(); // 按当前类型显隐字段并重置目录下拉
        ModalManager.onClose(modalId, () => {
            toggle?.setAttribute('aria-expanded', 'false');
        });
        toggle?.setAttribute('aria-expanded', 'true');
        ModalManager.open(modalId);
    } else {
        ModalManager.close(modalId);
    }
}

function sdkConfigTarget() {
    return document.getElementById('agent-sdk-add-target')?.value?.trim() || '';
}

function sdkConfigResolvedPath() {
    // 手动兜底优先于勾选列表（probe / 单条注册语义）
    const manual = document.getElementById('agent-sdk-add-path-manual')?.value?.trim() || '';
    if (manual) return manual;
    return sdkConfigSelectedPaths()[0] || '';
}

// 选中目录/手动路径变化后预填 Android 版本：仅当输入框为空时填入后端
// 识别结果（用户手动填过的值不覆盖）。走 probe 接口顺带返回，不发新请求。
async function sdkConfigPrefillAndroidVersion() {
    const input = document.getElementById('agent-sdk-add-android-version');
    if (!input || input.value.trim()) return;
    const repoRoot = sdkConfigResolvedPath();
    if (!repoRoot) return;
    try {
        const resp = await apiCall('/api/sdk/manage/probe', 'POST', {
            provider: document.getElementById('agent-sdk-add-type')?.value || 'ssh_git',
            target: sdkConfigTarget(),
            repo_root: repoRoot,
            password: document.getElementById('agent-sdk-add-password')?.value || '',
        }, {silentToast: true});
        const guessed = resp?.data?.probe?.android_version || '';
        if (guessed && !input.value.trim()) input.value = guessed;
    } catch (_error) {
        // 预填失败静默：识别只是便利功能，不阻塞添加流程。
    }
}

function sdkConfigDiscoverRoots() {
    // 回车触发点：act-bridge 对 async handler 已兜底上报，这里保持同步
    // 调用避免表单默认提交；防抖由 discover 进行中的按钮 disabled 承担。
    return sdkConfigDiscoverRootsInner();
}

async function sdkConfigDiscoverRootsInner() {
    const type = document.getElementById('agent-sdk-add-type')?.value || 'ssh_git';
    const target = sdkConfigTarget();
    const button = document.getElementById('agent-sdk-add-discover-btn');
    const list = document.getElementById('agent-sdk-add-path-list');
    const actions = document.getElementById('agent-sdk-add-path-actions');
    const hint = document.getElementById('agent-sdk-add-path-hint');
    const password = document.getElementById('agent-sdk-add-password')?.value || '';
    if (type !== 'local_git' && !target) {
        showToast('请先填写编译服务器地址（user@host）', 'warning');
        return;
    }
    if (button) { button.disabled = true; button.textContent = '发现中…'; }
    if (list) {
        list.innerHTML = '<div class="agent-sdk-path-empty">发现中…</div>';
        list.dataset.count = '0';
    }
    if (actions) actions.hidden = true;
    try {
        let candidates;
        if (type === 'local_git') {
            // 本机：扫 home 一级目录 + 当前项目根的同级（有界，本地无 SSH）
            candidates = await sdkConfigDiscoverLocalRoots();
        } else {
            const resp = await apiCall('/api/sdk/manage/discover', 'POST', {target, password}, {silentToast: true});
            candidates = Array.isArray(resp?.data?.candidates) ? resp.data.candidates : [];
        }
        if (!candidates.length) {
            if (list) list.innerHTML = '<div class="agent-sdk-path-empty">未发现候选目录；可改用手动路径兜底</div>';
            if (hint) hint.textContent = '列表为空时，在「手动路径兜底」输入完整绝对路径。';
            return;
        }
        if (list) {
            // 默认全选：用户通常想把发现的 SDK 树一次加完，取消勾选即可剔除。
            list.innerHTML = candidates.map(c => `
                <label class="agent-sdk-path-item">
                    <input type="checkbox" value="${agentAccessEscape(c.path)}" checked data-change="sdkConfigRefreshPathHint">
                    <span class="agent-sdk-path-kind">${agentAccessEscape(c.kind_label || c.kind)}</span>
                    <span class="agent-sdk-path-value mono">${agentAccessEscape(c.path)}</span>
                </label>`).join('');
            list.dataset.count = String(candidates.length);
            if (actions) actions.hidden = false;
            if (hint) sdkConfigRefreshPathHint();
        }
    } catch (error) {
        if (list) list.innerHTML = `<div class="agent-sdk-path-empty">发现失败：${agentAccessEscape(error.message || '')}</div>`;
        if (hint) hint.textContent = '发现失败时可直接在「手动路径兜底」输入完整路径。';
    } finally {
        if (button) { button.disabled = false; button.textContent = '发现目录'; }
    }
}

async function sdkConfigDiscoverLocalRoots() {
    // 本机候选：复用 discover 语义但走本地 API（provider=local_git 时
    // 由后端 _probe_local_path 校验；这里只列 home 一级 + 项目根同级）。
    try {
        const resp = await apiCall('/api/sdk/manage/discover-local', 'POST', {}, {silentToast: true});
        return Array.isArray(resp?.data?.candidates) ? resp.data.candidates : [];
    } catch (error) {
        return [];
    }
}

async function sdkConfigProbePath() {
    const type = document.getElementById('agent-sdk-add-type')?.value || 'ssh_git';
    const repoRoot = sdkConfigResolvedPath();
    const target = sdkConfigTarget();
    const button = document.getElementById('agent-sdk-add-probe-btn');
    const box = document.getElementById('agent-sdk-add-probe-result');
    const text = document.getElementById('agent-sdk-add-probe-text');
    if (type === 'ssh_git' && !target) {
        showToast('请先填写编译服务器地址（user@host）', 'warning');
        return;
    }
    if (!repoRoot) {
        showToast('请先选择或填写 SDK 路径', 'warning');
        return;
    }
    if (button) { button.disabled = true; button.textContent = '校验中…'; }
    if (box) box.hidden = true;
    try {
        const resp = await apiCall('/api/sdk/manage/probe', 'POST', {
            provider: type, target, repo_root: repoRoot,
            password: document.getElementById('agent-sdk-add-password')?.value || '',
        }, {silentToast: true});
        const probe = resp?.data?.probe || {};
        if (text) {
            const detail = probe.path_exists
                ? `${probe.summary || '目录存在'}${probe.is_git_repo ? '；单 git 仓' : ''}${probe.is_repo_workspace ? '；repo 工作区' : ''}`
                : '路径不存在';
            text.textContent = `${repoRoot}：${detail}`;
            text.style.color = probe.path_exists && (probe.git_repo_count || probe.is_git_repo || probe.is_repo_workspace)
                ? 'var(--success-color, #2e7d32)' : 'var(--danger-color, #c62828)';
        }
        if (box) box.hidden = false;
    } catch (error) {
        if (text) {
            text.textContent = `校验失败：${error.message || '未知错误'}`;
            text.style.color = 'var(--danger-color, #c62828)';
        }
        if (box) box.hidden = false;
    } finally {
        if (button) { button.disabled = false; button.textContent = '校验路径'; }
    }
}

async function sdkConfigSubmitAdd(event) {
    event?.preventDefault?.();
    const submit = document.getElementById('agent-sdk-add-submit');
    if (submit?.disabled || !ModalManager.isOpen('agent-sdk-add-modal')) return;
    const type = document.getElementById('agent-sdk-add-type')?.value || 'ssh_git';
    const target = sdkConfigTarget();
    const manual = document.getElementById('agent-sdk-add-path-manual')?.value?.trim() || '';
    const selected = sdkConfigSelectedPaths();
    // 手动兜底路径只作为单条添加；勾选列表（含 1 条）都走批量端点。
    const paths = manual ? [manual] : selected;
    const password = document.getElementById('agent-sdk-add-password')?.value || '';
    const revision = document.getElementById('agent-sdk-add-revision')?.value?.trim() || '';
    if (type === 'ssh_git' && !target) { showToast('请填写编译服务器地址（user@host）', 'warning'); return; }
    if (!paths.length) { showToast('请先「发现目录」并勾选，或在手动路径兜底里填写', 'warning'); return; }
    if (submit) { submit.disabled = true; submit.textContent = paths.length > 1 ? `添加中（${paths.length} 条）…` : '添加中…'; }
    try {
        const granted = window.requestElevatedAccess
            ? await window.requestElevatedAccess(`添加 SDK 源码源：${paths.join('、')}`)
            : true;
        if (!granted || !ModalManager.isOpen('agent-sdk-add-modal')) return;
        const payload = {
            provider: type,
            target,
            password,
            default_revision: revision,
            android_version: document.getElementById('agent-sdk-add-android-version')?.value?.trim() || '',
        };
        let summary;
        if (paths.length === 1) {
            const resp = await apiCall('/api/sdk/manage/sources', 'POST', {
                ...payload,
                repo_root: paths[0],
                source_id: document.getElementById('agent-sdk-add-id')?.value?.trim() || '',
            }, {silentToast: true});
            const created = resp?.data?.source || {};
            summary = {ok: 1, bad: 0, first: created.source_id || paths[0]};
        } else {
            // 批量：一次 SSH 连接探测全部路径；source_id 一律按目录名生成。
            const resp = await apiCall('/api/sdk/manage/sources/batch', 'POST', {
                ...payload,
                repo_roots: paths,
            }, {silentToast: true});
            const created = Array.isArray(resp?.data?.created) ? resp.data.created : [];
            const failed = Array.isArray(resp?.data?.failed) ? resp.data.failed : [];
            summary = {ok: created.length, bad: failed.length, first: created[0]?.source_id || ''};
        }
        if (summary.bad) {
            showToast(`已添加 ${summary.ok} 条，${summary.bad} 条失败（详情见列表校验列）`, 'warning');
        } else if (summary.ok > 1) {
            showToast(`已批量添加 ${summary.ok} 个 SDK 源（如「${summary.first}」等），Agent 检索立即可用`, 'success');
        } else {
            showToast(`已添加 SDK 源「${summary.first}」，Agent 检索立即可用`, 'success');
        }
        ModalManager.close('agent-sdk-add-modal');
        document.getElementById('agent-sdk-add-path-manual').value = '';
        document.getElementById('agent-sdk-add-id').value = '';
        document.getElementById('agent-sdk-add-password').value = '';
        const androidInput = document.getElementById('agent-sdk-add-android-version');
        if (androidInput) androidInput.value = '';
        const probeBox = document.getElementById('agent-sdk-add-probe-result');
        if (probeBox) probeBox.hidden = true;
        agentSdkSourcesLoaded = false;
        sdkConfigLoadSources();
    } catch (error) {
        showToast(`添加失败：${error.message || '未知错误'}`, 'error');
    } finally {
        if (submit) { submit.disabled = false; submit.textContent = '添加'; }
    }
}

async function sdkConfigDeleteSource(sourceId) {
    const record = agentSdkUserLiveCache[sourceId];
    if (!record) return;
    const isLocal = record.provider === 'local_git';
    const target = isLocal
        ? `本机路径 ${record.repo_root || ''}`
        : `${record.ssh_username}@${record.ssh_host}:${record.ssh_port ?? 22} 上的 ${record.repo_root || ''}`;
    const confirmed = await showConfirmDialog(
        '删除 SDK 源码源',
        `确定删除「${sourceId}」吗？\n\n仅解除本平台与 ${target} 的检索绑定，不删除、不修改任何代码文件。`,
    );
    if (!confirmed) return;
    try {
        const granted = window.requestElevatedAccess
            ? await window.requestElevatedAccess(`删除 SDK 源码源：${sourceId}`)
            : true;
        if (!granted) return;
        await apiCall(`/api/sdk/manage/sources/${encodeURIComponent(sourceId)}`, 'DELETE', null, {silentToast: true});
        showToast(`已删除 SDK 源「${sourceId}」`, 'success');
        delete agentSdkUserLiveCache[sourceId];
        agentSdkSourcesLoaded = false;
        sdkConfigLoadSources();
    } catch (error) {
        showToast(`删除失败：${error.message || '未知错误'}`, 'error');
    }
}
