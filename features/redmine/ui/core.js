// Redmine core; loaded by page.html in dependency order.
function scrollToSection(id) {
  var el = document.getElementById(id);
  if (!el) return;
  var header = document.querySelector('header');
  var offset = (header ? header.getBoundingClientRect().height : 0) + 14;
  var top = el.getBoundingClientRect().top + window.pageYOffset - offset;
  window.scrollTo({top: Math.max(0, top), behavior: 'smooth'});
}
let currentTab = 'stats';
let currentPage = 1;
const pageSize = 15;
let currentRunId = '';
let statsUserInitialized = false;
let statsConfig = {stale_days: 20, window_days: 60, cache_ttl: 600, freshness_days: 180, redmine: {base_url: ''}, dashboard: {profiles: [], defaults: {list_limit: 50, issue_limit: 500}}};
let departmentProfileId = '';
let projectProfileId = '';
const redmineDashboardRequestGeneration = {department: 0, stats: 0, project: 0};
// 趋势明细点击上下文：当前看板作用的指派人姓名列表（个人=[name]，部门=全员）
let redmineTrendNames = [];
function updateRedmineTrendNames(selectedName, meta) {
  if (selectedName) {
    redmineTrendNames = [selectedName];
    return;
  }
  redmineTrendNames = ((meta || {}).owner_names || []).map(function(name) {
    return String(name || '').trim();
  }).filter(Boolean);
}
let pendingDepartmentTargetSelect = '';
let pendingTrendChartKey = '';
let projectOpenOnly = false;
let redmineWorkspaceContext = {};
let pendingWorkspaceIssueId = '';

function getSelectedStatsAssignee() {
  var input = document.getElementById('syncAssigneeInput');
  var explicit = input ? String(input.value || '').trim() : '';
  if (explicit) return explicit;
  if (currentTab !== 'stats') return '';
  var select = document.getElementById('statsUserSelect');
  var selected = select ? String(select.value || '').trim() : '';
  if (selected && selected !== '加载中...') return selected;
  try {
    return (new URLSearchParams(window.location.search).get('name') || '').trim();
  } catch (_) {
    return '';
  }
}

// ---- Load stats config from backend (cached 60s) ----
let _statsConfigCacheTs = 0;
async function loadStatsConfig() {
  if (statsConfig.stale_days && Date.now() - _statsConfigCacheTs < 60000) return;
  try {
    statsConfig = await api('/api/redmine-agent/config/stats');
    _statsConfigCacheTs = Date.now();
  } catch (_) {}
}

// ---- API helper ----
async function api(url, options) {
  const r = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...(options || {})});
  const text = await r.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch (e) {
    throw new Error((r.status ? 'HTTP ' + r.status + ': ' : '') + (text || e.message).slice(0, 180));
  }
  if (!r.ok) {
    const detail = data.error || data.detail || ('HTTP ' + r.status);
    const error = new Error(r.status === 401 ? '登录状态已失效，请刷新后重新登录' : detail);
    error.status = r.status;
    throw error;
  }
  if (!data.success) throw new Error(data.error || '请求失败');
  return data.data || data;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function trunc(s, n) {
  s = String(s || '');
  return s.length > n ? s.slice(0, n) + '...' : s;
}
function redmineBaseUrl() {
  return String(((statsConfig.redmine || {}).base_url) || '').replace(new RegExp('/+$'), '');
}
function redmineIssueUrl(issueId) {
  return redmineBaseUrl() + '/issues/' + encodeURIComponent(String(issueId || '').trim());
}
function renderRedmineIssueLink(issueId, options) {
  const id = String(issueId || '').trim();
  if (!id) return '-';
  const opts = options || {};
  const label = opts.label || ('#' + id);
  const stop = opts.stopPropagation === false ? '' : ' data-click="_actStopPropagation" data-r0="event"';
  return '<a class="redmine-issue-link" data-redmine-issue-id="' + esc(id) + '" href="' + redmineIssueUrl(id) + '" target="_blank" rel="noopener"' + stop + '>' + esc(label) + '</a>';
}

function selectRedmineWorkspaceIssue(issueId) {
  const id = String(issueId || '').replace(/^#/, '').trim();
  if (!id) return;
  redmineWorkspaceContext = Object.assign({}, redmineWorkspaceContext, {redmine_issue_id: id});
  window.GmsEmbeddedWorkspace?.update({redmine_issue_id: id, origin_page: 'redmine'});
}

function navigateFromRedmineIssue(page, issueId) {
  const id = String(issueId || '').replace(/^#/, '').trim();
  selectRedmineWorkspaceIssue(id);
  window.GmsEmbeddedWorkspace?.navigate(page, {redmine_issue_id: id, origin_page: 'redmine'});
}

async function applyRedmineWorkspaceContext(next, navigate) {
  redmineWorkspaceContext = Object.assign({}, redmineWorkspaceContext, next || {});
  const issueId = String(redmineWorkspaceContext.redmine_issue_id || '').replace(/^#/, '').trim();
  if (!navigate || !issueId || pendingWorkspaceIssueId === issueId) return;
  pendingWorkspaceIssueId = issueId;
  const input = document.getElementById('searchInput');
  if (input) input.value = issueId;
  switchTab('issues');
  try {
    await smartSearch();
    const card = document.querySelector(`.issue-card[data-issue-id="${CSS.escape(issueId)}"]`);
    if (card) card.scrollIntoView({behavior: 'smooth', block: 'start'});
  } finally {
    pendingWorkspaceIssueId = '';
  }
}
function renderRedmineIssueLinks(issueIds, options) {
  const ids = (issueIds || []).map(function(id) { return String(id || '').trim(); }).filter(Boolean);
  return ids.length ? ids.map(function(id) { return renderRedmineIssueLink(id, options); }).join(' ') : '-';
}
function linkifyRedmineIssueRefs(escapedText, options) {
  return String(escapedText || '').replace(/(^|[^\w/])#(\d{5,})\b/g, function(_, prefix, id) {
    return prefix + renderRedmineIssueLink(id, options);
  });
}
function redmineIssueAttachmentsUrl(issueId) {
  return redmineIssueUrl(issueId) + '#attachments';
}
function redmineAttachmentDownloadUrl(attachmentId) {
  return redmineBaseUrl() + '/attachments/download/' + encodeURIComponent(String(attachmentId || '').trim()) + '/';
}
function redmineIssueUrls(items) {
  return (items || []).map(function(item) { return item.issue_id || ''; }).filter(Boolean).map(redmineIssueUrl);
}
function formatBytes(bytes) {
  var n = Number(bytes || 0);
  if (!n) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(n >= 100 * 1024 ? 0 : 1) + ' KB';
  return (n / 1024 / 1024).toFixed(2) + ' MB';
}
function departmentProfiles() {
  return ((statsConfig.dashboard || {}).profiles || []);
}
function projectProfiles() {
  return ((statsConfig.dashboard || {}).project_profiles || []);
}
function departmentOptionsHtml(selectedId, includeAll) {
  var profiles = departmentProfiles().filter(function(item) { return includeAll || item.id !== 'all'; });
  return profiles.map(function(item) {
    var selected = item.id === selectedId ? ' selected' : '';
    return '<option value="' + esc(item.id || '') + '"' + selected + '>' + esc(item.name || item.id || '-') + '</option>';
  }).join('');
}
function projectOptionsHtml(selectedId) {
  return projectProfiles().map(function(item) {
    var selected = item.id === selectedId ? ' selected' : '';
    return '<option value="' + esc(item.id || '') + '"' + selected + '>' + esc(item.name || item.project_id || '-') + '</option>';
  }).join('');
}

// ---- Tab switching ----
function syncDailyBriefStickyTop() {
  // 区块工具栏（「Redmine 单号分析」/「每日晨报 · 待回复事项」）吸附点 =
  // 真实页头（tabs+筛选行）的实测高度；页头 sticky/fixed 在视口顶部，
  // 窄屏换行变高时 resize 后重算。
  var header = document.querySelector('body > header');
  var top = header ? Math.ceil(header.getBoundingClientRect().height) : 44;
  document.querySelectorAll('.daily-brief-card').forEach(function (card) {
    card.style.setProperty('--daily-brief-sticky-top', top + 'px');
  });
}

function switchTab(tab, force) {
  var target = document.getElementById('tab-' + tab) ? tab : 'stats';
  currentTab = target;
  updateRedmineToolbar();
  if (target === 'daily-brief') syncDailyBriefStickyTop();
  try { window.sessionStorage.setItem('redmineLastTab', target); } catch(_) {}
  var url = new URL(window.location.href);
  if (target === 'stats') url.searchParams.delete('tab');
  else url.searchParams.set('tab', target);
  window.history.replaceState({}, '', url.toString());
  document.querySelectorAll('.tab').forEach(t => {
    var active = t.dataset.tab === target;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('.tab-content').forEach(t => t.classList.toggle('active', t.id === 'tab-' + target));
  if (target === 'issues') return loadIssues();
  if (target === 'cases') return loadCases();
  if (target === 'runs') return loadRuns();
  if (target === 'department') return loadDepartmentOverdue(false);
  if (target === 'project') return loadProjectDashboard(false);
  if (target === 'daily-brief') {
    loadSingleIssueDevices();
    return loadDailyBrief();
  }  if (target === 'stats') return loadStatistics(force === true);
  return Promise.resolve();
}

// ARIA tablist 方向键导航：Home/End 跳转，←/→ 移动焦点并激活目标页签
// （roving tabindex 简化版：页签是真实 <button>，Tab 键天然可达）。
function redmineTabKeydown(event) {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
  if (!tabs.length) return;
  var index = tabs.indexOf(document.activeElement);
  if (index === -1) return;
  var next = null;
  if (event.key === 'ArrowLeft') next = tabs[(index - 1 + tabs.length) % tabs.length];
  else if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
  else if (event.key === 'Home') next = tabs[0];
  else if (event.key === 'End') next = tabs[tabs.length - 1];
  if (!next) return;
  event.preventDefault();
  next.focus();
  if (next.dataset.tab !== currentTab) switchTab(next.dataset.tab);
}

function updateRedmineToolbar() {
  var filters = document.getElementById('issuesToolbarFilters');
  var action = document.getElementById('scanBtn');
  var refresh = document.getElementById('refreshBtn');
  var isIssues = currentTab === 'issues';
  var isAnalysisTab = ['issues', 'cases', 'runs'].includes(currentTab);

  if (filters) {
    filters.hidden = !isIssues;
    filters.setAttribute('aria-hidden', String(!isIssues));
  }
  if (action) {
    action.hidden = !isAnalysisTab;
    action.setAttribute('aria-hidden', String(!isAnalysisTab));
    action.disabled = false;
    action.title = '';
    if (currentTab === 'cases') {
      action.textContent = '📚 导入';
      action.title = '批量导入工单并进行结构化分析';
    } else {
      action.textContent = '🔍 扫描';
      action.title = currentTab === 'runs'
        ? '扫描最近 48 小时工单并生成扫描记录'
        : '扫描最近 48 小时工单并更新工单分析';
    }
  }
  if (refresh) {
    refresh.title = currentTab === 'daily-brief'
      ? '只刷新每日晨报展示，不启动新的 AI 分析'
      : '绕过缓存，重新加载当前页面数据';
  }
}

async function runRedmineContextAction() {
  if (currentTab === 'cases') return showBatchImportModal();
  return startScan();
}

function setRefreshButtonBusy(busy) {
  var btn = document.getElementById('refreshBtn');
  if (!btn) return;
  btn.disabled = busy;
  // 忙碌文案不带省略号：与空闲态宽度尽量接近，避免按钮尺寸跳动。
  btn.textContent = busy ? '⏳ 刷新中' : '🔃 刷新';
}

// 切换部门/统计身份、工具栏刷新共用同一忙碌语义：期间禁用并显示
// 「⏳ 刷新中」，让切换触发的强制刷新有可见反馈。
async function withRefreshButtonBusy(task) {
  setRefreshButtonBusy(true);
  try {
    await task();
  } finally {
    setRefreshButtonBusy(false);
  }
}

async function refreshCurrentTab() {
  await withRefreshButtonBusy(async function() {
    if (currentTab === 'issues') {
      await refreshRedmineSnapshots();
      await loadIssues();
    } else if (currentTab === 'runs') {
      await refreshRedmineSnapshots();
      await loadRuns();
    } else if (currentTab === 'cases') {
      await loadCases();
    } else if (currentTab === 'department') {
      await loadDepartmentOverdue(true);
    } else if (currentTab === 'project') {
      await loadProjectDashboard(true);
    } else if (currentTab === 'daily-brief') {
      // 先同步 Redmine 最新标题（subject 在分析入队时冻结，改标题后本地
      // 不会自动跟随），再重拉晨报与单号分析历史。
      try {
        var synced = await api('/api/redmine-agent/daily-brief/sync-subjects', {method: 'POST'}) || {};
        var renamed = (synced.issues || []).length;
        if (renamed) notifyUser('标题已同步', renamed + ' 个单号的 Redmine 标题已更新', 'success');
      } catch (_) { /* 标题同步失败不阻断刷新 */ }
      await loadDailyBrief();
    } else {
      await loadStatistics(true);
    }
  });
}

async function refreshRedmineSnapshots() {
  const assignee = getSelectedStatsAssignee();
  const params = new URLSearchParams({max_analyze: '0'});
  if (assignee) params.set('assignee_name', assignee);
  const started = await api(`/api/redmine-agent/sync?${params}`, {method:'POST'});
  await waitForRun(started.run_id, '刷新', {reload: false});
}

async function initStatsUserSelect() {
  if (statsUserInitialized) return;
  statsUserInitialized = true;
  var select = document.getElementById('statsUserSelect');
  if (!select) return;
  try {
    var data = await api('/api/redmine-agent/users');
    var items = (data.items || []).slice().sort(function(a, b) { return (a.name || '').localeCompare(b.name || ''); });
    select.innerHTML = items.map(function(item) {
      var name = item.name || '';
      return '<option value="' + esc(name) + '">' + esc(name) + '</option>';
    }).join('');
    // 默认选中当前登录用户：优先 URL 上的 name，否则用后端返回的 current_name。
    var q = new URLSearchParams(window.location.search);
    var name = q.get('name') || data.current_name || '';
    if (name) select.value = name;
  } catch (_) {}
}

async function onStatsUserChange() {
  var select = document.getElementById('statsUserSelect');
  var name = select ? select.value : '';
  var url = new URL(window.location.href);
  if (name) url.searchParams.set('name', name);
  else url.searchParams.delete('name');
  url.searchParams.set('tab', 'stats');
  window.history.replaceState({}, '', url.toString());
  if (select) select.disabled = true;
  try {
    // 切换统计身份强制绕过缓存重拉（refresh=true），刷新按钮同步进入
    // 刷新中状态，避免旧身份的数据滞留成「切换未生效」的错觉。
    await withRefreshButtonBusy(function() { return loadStatistics(true); });
  } finally {
    if (select) select.disabled = false;
  }
}

// ---- Shared modal helpers ----
// Modal 生命周期统一走共享控制器（web/static/js/embedded-ui/modal-controller.js）：
// 栈/z-index/inert/aria/Escape/backdrop/focus trap 单一所有者，页面只保留
// 全局函数别名——运行时冒烟测试与 data-click 契约依赖这些名字。
function showModal(id) {
  window.EmbeddedModalController.open(id);
}
function hideModal(id) {
  window.EmbeddedModalController.close(id);
}
function removeDynamicModal(id) {
  window.EmbeddedModalController.remove(id);
}
function showDailyBriefAnalysisModal(modal) {
  // The analysis reader should occupy exactly the Redmine content area below
  // the Tabs. Measure the live header because it wraps to two rows on narrow
  // viewports; a fixed 44px offset would overlap it in that state.
  var header = document.querySelector('body > header');
  if (header) {
    modal.style.setProperty('--daily-brief-modal-top', Math.ceil(header.getBoundingClientRect().height) + 'px');
  }
  document.body.appendChild(modal);
  showModal(modal.id);
}
function notifyUser(title, message, level) {
  level = level || 'info';
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({type:'redmine-agent-notification', title:title, message:message, level:level}, window.location.origin);
    }
  } catch (_) {}
  var old = document.getElementById('redmine-local-toast');
  if (old) old.remove();
  var toast = document.createElement('div');
  toast.id = 'redmine-local-toast';
  toast.textContent = title + (message ? ': ' + message : '');
  toast.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:20000;max-width:min(460px,calc(100vw - 32px));padding:10px 12px;border-radius:6px;background:#111827;color:#f8fafc;border:1px solid #334155;box-shadow:0 8px 24px rgba(0,0,0,.28);font-size:12px;overflow-wrap:anywhere;pointer-events:none;';
  document.body.appendChild(toast);
  setTimeout(function(){ if (toast.parentNode) toast.remove(); }, 3600);
}

async function confirmUserAction(title, message) {
  if (typeof window.parent?.showConfirmDialog === 'function') {
    return window.parent.showConfirmDialog(title, message);
  }
  return window.confirm(message);
}

function openRedmineReplyModal(issueId, replyText, meta) {
  issueId = String(issueId || '').trim();
  meta = meta || {};
  const modalId = 'redmineReplyModal-' + Date.now();
  const issueInputId = modalId + '-issue';
  const replyTextId = modalId + '-reply';
  const fileInputId = modalId + '-files';
  const fileListId = modalId + '-file-list';
  const modal = document.createElement('div');
  modal.id = modalId;
  modal.className = 'modal';
  modal.innerHTML = `
    <div class="modal-content redmine-reply-modal">
      <div class="modal-header" style="background:linear-gradient(135deg,#0ea5e9,#6366f1)">
        <span class="modal-title">📝 Redmine回复</span>
        <button type="button" class="modal-close" aria-label="关闭" data-click="removeDynamicModal" data-a0="${modalId}">&times;</button>
      </div>
      <div class="modal-body">
        ${meta.summaryHtml ? `<div class="muted">${meta.summaryHtml}</div>` : (meta.summary ? `<div class="muted">${esc(meta.summary)}</div>` : '')}
        <div>
          <label>Redmine Issue ID</label>
          <input type="text" id="${issueInputId}" data-redmine-issue-input value="${esc(issueId)}" placeholder="输入 Redmine Issue ID">
        </div>
        <div>
          <label>回复内容</label>
          <textarea id="${replyTextId}" data-redmine-reply-text rows="14" placeholder="输入回复内容...">${esc(replyText || '')}</textarea>
        </div>
        <div>
          <label>📎 附件</label>
          <input type="file" id="${fileInputId}" data-redmine-files multiple style="display:none" data-change="updateRedmineReplyFileList" data-a0="${fileInputId}" data-a1="${fileListId}">
          <div id="${fileInputId}-drop" class="redmine-reply-drop" data-click="_actClickById" data-a0="${fileInputId}">拖拽文件到此处，或点击选择文件</div>
          <div id="${fileListId}" class="redmine-reply-file-list"></div>
        </div>
        <div class="modal-buttons">
          <button class="secondary" data-click="removeDynamicModal" data-a0="${modalId}">取消</button>
          <button data-click="confirmAndSendRedmineReply" data-a0="${modalId}">确认并发送</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);
  showModal(modalId);
  const drop = document.getElementById(fileInputId + '-drop');
  if (drop) {
    drop.addEventListener('dragover', function(e) { e.preventDefault(); drop.classList.add('drag-over'); });
    drop.addEventListener('dragleave', function(e) { e.preventDefault(); drop.classList.remove('drag-over'); });
    drop.addEventListener('drop', function(e) {
      e.preventDefault();
      drop.classList.remove('drag-over');
      if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
      const input = document.getElementById(fileInputId);
      const dt = new DataTransfer();
      Array.from(input.files || []).forEach(function(file) { dt.items.add(file); });
      Array.from(e.dataTransfer.files || []).forEach(function(file) { dt.items.add(file); });
      input.files = dt.files;
      updateRedmineReplyFileList(fileInputId, fileListId);
    });
  }
  const area = document.getElementById(replyTextId);
  if (area) area.focus();
}

function updateRedmineReplyFileList(fileInputId, fileListId) {
  const input = document.getElementById(fileInputId);
  const box = document.getElementById(fileListId);
  if (!input || !box) return;
  const files = Array.from(input.files || []);
  if (!files.length) { box.innerHTML = ''; return; }
  box.innerHTML = files.map(function(file, idx) {
    return `<div class="redmine-reply-file"><span>📎 ${esc(file.name)} <span class="muted">(${formatBytes(file.size) || '0 B'})</span></span><button type="button" data-click="removeRedmineReplyFile" data-a0="${fileInputId}" data-a1="${fileListId}" data-a2="${idx}">移除</button></div>`;
  }).join('');
}

function removeRedmineReplyFile(fileInputId, fileListId, index) {
  const input = document.getElementById(fileInputId);
  if (!input) return;
  const dt = new DataTransfer();
  Array.from(input.files || []).forEach(function(file, idx) {
    if (idx !== index) dt.items.add(file);
  });
  input.files = dt.files;
  updateRedmineReplyFileList(fileInputId, fileListId);
}

async function confirmAndSendRedmineReply(modalId) {
  const modal = document.getElementById(modalId);
  const issueId = (modal && modal.querySelector('[data-redmine-issue-input]') ? modal.querySelector('[data-redmine-issue-input]').value : '').trim();
  const replyText = (modal && modal.querySelector('[data-redmine-reply-text]') ? modal.querySelector('[data-redmine-reply-text]').value : '').trim();
  const fileInput = modal ? modal.querySelector('[data-redmine-files]') : null;
  if (!issueId) { notifyUser('缺少 Issue ID', '请输入 Redmine Issue ID', 'error'); return; }
  if (!replyText) { notifyUser('回复为空', '请填写回复内容', 'error'); return; }
  const formData = new FormData();
  formData.append('issue_id', issueId);
  formData.append('reply_text', replyText);
  Array.from((fileInput && fileInput.files) || []).forEach(function(file) { formData.append('files', file); });
  const sendBtn = modal ? modal.querySelector('.modal-buttons button:last-child') : null;
  if (sendBtn) { sendBtn.disabled = true; sendBtn.textContent = '发送中...'; }
  try {
    const data = await api('/api/redmine/reply', {method:'POST', body: formData});
    notifyUser('已发送', (data && data.message) || ('回复已发送到 Redmine #' + issueId));
    removeDynamicModal(modalId);
  } catch (e) {
    notifyUser('发送失败', e.message, 'error');
    if (sendBtn) { sendBtn.disabled = false; sendBtn.textContent = '确认并发送'; }
  }
}


// ---- Formatted content rendering ----
var _BT = String.fromCharCode(96);
var _F3 = _BT+_BT+_BT;
var _NL = String.fromCharCode(10);
var _HTML_RE = /<pre><code(?:\s+class="(\w*)")?\s*>([\s\S]*?)<\/code><\/pre>/g;

function _nl2br(s) { return s.replace(new RegExp(_NL, 'g'), '<br>'); }
function normalizeDisplayText(text) {
  return String(text == null ? '' : text).replace(/\\r\\n/g, _NL).replace(/\\n/g, _NL).replace(/\r\n/g, _NL);
}

function renderIssueRichText(text, defaultClass) {
  text = normalizeDisplayText(text);
  if (!text.trim()) return '<div class="muted">-</div>';
  return '<div class="' + (defaultClass || 'rich-field') + '">' + renderMarkdownDoc(text) + '</div>';
}

function renderFormattedContent(text, defaultClass) {
  if (!text) return '';
  text = normalizeDisplayText(text);
  var cls = defaultClass || 'field-content';
  var result = '';
  var parts = []; // {type:'text'|'code', content, lang}
  var lastIdx = 0;

  // 1. Extract HTML <pre><code class="lang">...</code></pre> blocks
  _HTML_RE.lastIndex = 0;
  var m;
  while ((m = _HTML_RE.exec(text)) !== null) {
    if (m.index > lastIdx) parts.push({type:'text', content:text.slice(lastIdx, m.index), lang:''});
    parts.push({type:'code', content:m[2]||'', lang:(m[1]||'').toLowerCase()});
    lastIdx = _HTML_RE.lastIndex;
  }
  if (lastIdx < text.length) parts.push({type:'text', content:text.slice(lastIdx), lang:''});

  // If no HTML blocks found, try markdown ```lang``` blocks
  if (parts.length <= 1 && parts[0] && parts[0].type === 'text') {
    parts = [];
    lastIdx = 0;
    var mdRe = new RegExp(_F3 + '(\\w*)' + _NL + '([\\s\\S]*?)' + _F3, 'g');
    var mm;
    while ((mm = mdRe.exec(text)) !== null) {
      if (mm.index > lastIdx) parts.push({type:'text', content:text.slice(lastIdx, mm.index), lang:''});
      parts.push({type:'code', content:mm[2]||'', lang:(mm[1]||'').toLowerCase()});
      lastIdx = mdRe.lastIndex;
    }
    if (lastIdx < text.length) parts.push({type:'text', content:text.slice(lastIdx), lang:''});
  }

  // 自动识别堆栈、命令和键值日志并格式化为代码块。
  if (!parts.length || (parts.length === 1 && parts[0].type === 'text')) {
    var raw = parts.length ? parts[0].content : text;
    var auto = _splitAutoCode(raw);
    if (auto.length > 1 || (auto.length === 1 && auto[0].type === 'code')) {
      parts = auto;
    }
  }

  // If still no blocks, return escaped text
  if (!parts.length) return _nl2br(linkifyRedmineIssueRefs(esc(text), {stopPropagation: false}));

  // 2. Render each part
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i];
    if (p.type === 'text') {
      if (p.content.trim()) result += '<div class="' + cls + '">' + _nl2br(linkifyRedmineIssueRefs(esc(p.content), {stopPropagation: false})) + '</div>';
    } else {
      if (p.lang === 'diff') result += renderDiffBlock(p.content);
      else if (p.lang === 'shell' || p.lang === 'bash' || p.lang === 'sh') result += renderShellBlock(p.content);
      else result += renderGenericCodeBlock(p.content, p.lang);
    }
  }
  return result || _nl2br(linkifyRedmineIssueRefs(esc(text), {stopPropagation: false}));
}

// 将普通文本按可识别的代码行拆分为文本和代码段。
function _splitAutoCode(text) {
  text = String(text || '');
  if (!text.trim()) return [];
  var lines = text.split(_NL);
  var segments = [];
  var textBuf = [];
  var codeBuf = [];
  var inCode = false;

  function flush() {
    if (codeBuf.length) { segments.push({type:'code', content: codeBuf.join(_NL), lang: _guessCodeLang(codeBuf)}); codeBuf = []; }
    if (textBuf.length) { segments.push({type:'text', content: textBuf.join(_NL), lang:''}); textBuf = []; }
  }

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    var isCode = _isCodeLikeLine(line, lines, i);
    if (isCode && !inCode) { flush(); inCode = true; }
    else if (!isCode && inCode) {
      // Allow a single blank line inside a code run to continue it.
      if (line.trim() === '' && codeBuf.length && i + 1 < lines.length && _isCodeLikeLine(lines[i+1], lines, i+1)) {
        codeBuf.push(line); continue;
      }
      flush(); inCode = false;
    }
    if (inCode) codeBuf.push(line); else textBuf.push(line);
  }
  flush();
  return segments;
}

function _isCodeLikeLine(line, lines, idx) {
  var s = String(line || '');
  if (!s.trim()) return false;
  // 识别 Redmine 中粘贴的命令和 unified diff。
  if (/^\s*diff\s+--git\s+/.test(s)) return true;
  if (/^\s*index\s+[0-9a-f]+\.\.[0-9a-f]+/.test(s)) return true;
  if (/^\s*(---|\+\+\+)\s+[ab]\//.test(s)) return true;
  if (/^\s*@@\s+[-+0-9, ]+@@/.test(s)) return true;
  if (idx > 0 && (/^\s*[+-]/.test(s)) && lines && lines.slice(Math.max(0, idx - 6), idx).some(function(prev) {
    return /^\s*(diff\s+--git|---\s+[ab]\/|\+\+\+\s+[ab]\/|@@\s+)/.test(prev);
  })) return true;
  // Stack trace: "at com.foo.Bar.method(File.java:123)"
  if (/^\s*at\s+[\w.$]+\(/.test(s)) return true;
  // Caused by / Exception / Error
  if (/^\s*(Caused by:|Exception|Error|FATAL|AssertionFailedError)/.test(s)) return true;
  // File:line failure (gtest/vts): "path.cpp:123: Failure"
  if (/[\w./\\]+\.(cpp|java|kt|py|h|c|cc):\d+:\s*(Failure|error|FAIL)?/i.test(s)) return true;
  // Shell command prefixes
  if (/^\s*\$\s/.test(s)) return true;
  if (/^\s*(run\s+\w+|adb\s|fastboot\s|python\d?\s|git\s|make\s|cd\s)/.test(s)) return true;
  // Logcat / kernel log: "12-12 15:41:11.123 X/Tag( 123): ..."
  if (/^\s*\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}/.test(s)) return true;
  if (/^\s*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) return true;
  // Test result lines: "[1/1] abc def TestRunner"
  if (/^\s*\[\d+\/\d+\]\s/.test(s)) return true;
  if (/\bFAILURE\b|\[\s*FAILED\s*\]|^\s*(Value of:|Actual:|Expected:)/i.test(s)) return true;
  // 混合代码时识别失败模块、用例和关键报错字段。
  return false;
}

function _guessCodeLang(codeLines) {
  var sample = (codeLines || []).slice(0, 16).join(_NL);
  if (/^\s*diff\s+--git\s+/m.test(sample) || /^---\s+[ab]\//m.test(sample) || /^\+\+\+\s+[ab]\//m.test(sample) || /^@@\s+/m.test(sample)) return 'diff';
  if (/^\s*\$\s/m.test(sample) || /^\s*(run|adb|fastboot|python|git|make|cd)\s/m.test(sample)) return 'shell';
  return '';
}

// 工单文档的轻量 Markdown 渲染。
function renderMarkdownDoc(text) {
  text = normalizeDisplayText(text);
  if (!text.trim()) return '';
  var lines = text.split(_NL);
  var html = '';
  var i = 0;
  while (i < lines.length) {
    var line = lines[i];
    // Fenced code block ```lang ... ```
    var fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      var lang = fence[1] || '';
      var buf = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++; // skip closing fence
      var code = buf.join(_NL);
      if (lang === 'diff') html += renderDiffBlock(code);
      else if (lang === 'shell' || lang === 'bash' || lang === 'sh') html += renderShellBlock(code);
      else html += renderGenericCodeBlock(code, lang);
      continue;
    }
    // HTML <pre><code> 代码块。
    if (/<pre><code/.test(line)) {
      var hbuf = [];
      while (i < lines.length && !/<\/code><\/pre>/.test(lines[i])) { hbuf.push(lines[i]); i++; }
      if (i < lines.length) { hbuf.push(lines[i]); i++; }
      html += renderFormattedContent(hbuf.join(_NL), 'field-content');
      continue;
    }
    // Table: a line with | followed by a delimiter row（GFM 分隔行：单元格只含
    // - 与对齐冒号，如 |---|---|、| --- | --- |、|:---|---:|。旧正则的
    // [\s:-|] 意外构成 ':'-'|' 字符区间、把字面 - 排除，紧凑分隔行命不中，
    // 整张表退化为纯文本；这里按「单元格必须含 -」重写，内容行不会误判）。
    if (/^\s*\|/.test(line) && i + 1 < lines.length
      && /^\s*\|(?:\s*:?-+:?\s*\|)+(?:\s*:?-+:?\s*\|?)?\s*$/.test(lines[i + 1])) {
      var rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(lines[i]); i++; }
      html += _renderMarkdownTable(rows);
      continue;
    }
    // Headings
    var h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      var level = h[1].length;
      html += '<h' + level + ' class="md-h">' + esc(h[2]) + '</h' + level + '>';
      i++; continue;
    }
    // Unordered list
    if (/^\s*[-*]\s+/.test(line)) {
      var items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*]\s+/, '')); i++; }
      html += '<ul class="md-ul">' + items.map(function(t){ return '<li>' + _inlineMd(esc(t)) + '</li>'; }).join('') + '</ul>';
      continue;
    }
    // Ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      var oitems = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { oitems.push(lines[i].replace(/^\s*\d+\.\s+/, '')); i++; }
      html += '<ol class="md-ol">' + oitems.map(function(t){ return '<li>' + _inlineMd(esc(t)) + '</li>'; }).join('') + '</ol>';
      continue;
    }
    // Analysis key/value lines: "失败模块: ...", "关键报错: ...",
    // "高度相似的历史单: #621439 ..." etc.
    if (_isKvLine(line)) {
      var kvRows = [];
      while (i < lines.length && _isKvLine(lines[i])) {
        var kv = lines[i].split(/[:：]/);
        var key = kv.shift() || '';
        var val = kv.join(':') || '';
        kvRows.push({key:key.trim(), val:val.trim()});
        i++;
      }
      html += '<div class="md-kv-list">' + kvRows.map(function(row) {
        return '<div class="md-kv-row"><b>' + esc(row.key) + '</b><span>' + _inlineMd(esc(row.val || '-')) + '</span></div>';
      }).join('') + '</div>';
      continue;
    }
    // Code-like log lines in plain text.
    if (_isCodeLikeLine(line, lines, i)) {
      var cbuf = [];
      while (i < lines.length && (_isCodeLikeLine(lines[i], lines, i) || (lines[i].trim() === '' && cbuf.length))) {
        cbuf.push(lines[i]); i++;
      }
      html += renderGenericCodeBlock(cbuf.join(_NL), _guessCodeLang(cbuf));
      continue;
    }
    // Blank line
    if (!line.trim()) { i++; continue; }
    // Paragraph (merge consecutive non-empty plain lines)
    var para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|[-*]\s|\d+\.\s|\|)/.test(lines[i]) && !/<pre><code/.test(lines[i])) {
      para.push(lines[i]); i++;
    }
    if (!para.length) {
      html += '<p class="md-p">' + _inlineMd(esc(line)) + '</p>';
      i++;
      continue;
    }
    html += '<p class="md-p">' + _nl2br(_inlineMd(esc(para.join(_NL)))) + '</p>';
  }
  return html;
}

function _renderMarkdownTable(rows) {
  if (rows.length < 2) return esc(rows.join(_NL));
  var header = _splitMarkdownTableRow(rows[0]);
  var body = rows.slice(2).map(function(r){
    var cells = _splitMarkdownTableRow(r);
    // AI 输出可能行列不齐：补空/截断到表头列数，保证弹框内网格对齐。
    while (cells.length < header.length) cells.push('');
    if (cells.length > header.length) cells = cells.slice(0, header.length);
    return '<tr>' + cells.map(function(c){return '<td>' + _inlineMd(esc(c)) + '</td>';}).join('') + '</tr>';
  }).join('');
  return '<table class="md-table"><thead><tr>' + header.map(function(c){return '<th>' + esc(c) + '</th>';}).join('') + '</tr></thead><tbody>' + body + '</tbody></table>';
}

function sanitizeHref(raw) {
  // URL scheme 白名单：Markdown 链接来自 Redmine issue/评论、AI 输出与
  // 历史分析结果，禁止 javascript:/data:/vbscript:/file: 等点击触发型
  // unsafe navigation / XSS 入口。相对路径仅允许站内 /... 形式。
  var value = String(raw == null ? '' : raw).trim();
  if (!value) return '#';
  // 站内相对地址(以 / 开头且非 // 协议相对形式)。
  if (value.charAt(0) === '/' && value.charAt(1) !== '/') return value;
  // 必须是显式 http(s) 绝对地址;其余(含 ../、./、裸文本)一律拒绝。
  if (!/^https?:[/][/]/i.test(value)) return '#';
  try {
    var u = new URL(value, window.location.origin);
    return u.href;
  } catch (e) {
    return '#';
  }
}

function _inlineMd(text) {
  // `code`, **bold**, [link](url)
  return String(text || '')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, function(_, label, url) {
      // url 已经过外层 esc();sanitizeHref 再做 scheme 白名单。
      return '<a href="' + esc(sanitizeHref(url)) + '" target="_blank" rel="noopener">' + label + '</a>';
    })
    .replace(/(^|[^\w/])#(\d{5,})\b/g, function(_, prefix, id) {
      return prefix + renderRedmineIssueLink(id, {stopPropagation: false});
    });
}

function _isKvLine(line) {
  var s = String(line || '').trim();
  if (!s || s.length > 500) return false;
  if (!/^[^:：]{2,40}[:：]\s*\S/.test(s)) return false;
  return /^(失败模块|失败用例|关键报错|高度相似|描述\/附件报错|附件证据|历史回复|测试模块|测试用例|模块|问题|根因|方案说明|解决方法|补丁方向|验证方式|参考文档|应用目录|建议参考历史单)[:：]/.test(s);
}
function renderDiffBlock(code) {
  var lines = code.split(_NL).map(function(line) {
    var e = esc(line);
    if (line.startsWith('---') || line.startsWith('+++')) return '<span class="diff-header">' + e + '</span>';
    if (line.startsWith('@@')) return '<span class="diff-hunk">' + e + '</span>';
    if (line.startsWith('+')) return '<span class="diff-add">' + e + '</span>';
    if (line.startsWith('-')) return '<span class="diff-remove">' + e + '</span>';
    return e;
  }).join(_NL);
  return '<div class="code-block diff-block"><div class="code-block-lang">diff</div><pre><code>' + lines + '</code></pre><button class="copy-btn" data-click="copyCode" data-r0="el">复制</button></div>';
}
function renderShellBlock(code) {
  var lines = code.split(_NL).map(function(line) {
    var e = esc(line);
    if (/^\$\s/.test(line)) return '<span class="shell-cmd">' + e + '</span>';
    return e;
  }).join(_NL);
  return '<div class="code-block shell-block"><div class="code-block-lang">shell</div><pre><code>' + lines + '</code></pre><button class="copy-btn" data-click="copyCode" data-r0="el">复制</button></div>';
}
function renderGenericCodeBlock(code, lang) {
  var langLabel = lang || 'code';
  return '<div class="code-block"><div class="code-block-lang">' + esc(langLabel) + '</div><pre><code>' + esc(code) + '</code></pre><button class="copy-btn" data-click="copyCode" data-r0="el">复制</button></div>';
}
function copyCode(btn) {
  var code = btn.previousElementSibling.querySelector('code');
  navigator.clipboard.writeText(code.textContent).then(function() {
    btn.textContent = '已复制';
    setTimeout(function() { btn.textContent = '复制'; }, 1500);
  });
}
function copyText(text, btn) {
  navigator.clipboard.writeText(String(text || '')).then(function() {
    if (!btn) return;
    var old = btn.textContent;
    btn.textContent = '✓';
    setTimeout(function() { btn.textContent = old; }, 1500);
  });
}
