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
let statsConfig = {stale_days: 20, window_days: 60, cache_ttl: 600, redmine: {base_url: 'https://redmine.rock-chips.com'}, dashboard: {profiles: [], defaults: {list_limit: 50, issue_limit: 500}}};
let departmentProfileId = '';
let projectProfileId = '';
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
  const r = await fetch(url, options || {});
  const text = await r.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch (e) {
    throw new Error((r.status ? 'HTTP ' + r.status + ': ' : '') + (text || e.message).slice(0, 180));
  }
  if (!r.ok) throw new Error(data.error || data.detail || ('HTTP ' + r.status));
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
  return String(((statsConfig.redmine || {}).base_url) || 'https://redmine.rock-chips.com').replace(new RegExp('/+$'), '');
}
function redmineIssueUrl(issueId) {
  return redmineBaseUrl() + '/issues/' + encodeURIComponent(String(issueId || '').trim());
}
function redmineIssueUrls(items) {
  return (items || []).map(function(item) { return item.issue_id || ''; }).filter(Boolean).map(redmineIssueUrl);
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

// ---- Formatted content rendering ----
var _BT = String.fromCharCode(96);
var _F3 = _BT+_BT+_BT;
var _NL = String.fromCharCode(10);
var _HTML_RE = /<pre><code(?:\s+class="(\w*)")?\s*>([\s\S]*?)<\/code><\/pre>/g;

function _nl2br(s) { return s.replace(new RegExp(_NL, 'g'), '<br>'); }

function renderFormattedContent(text, defaultClass) {
  if (!text) return '';
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

  // If still no blocks, return escaped text
  if (!parts.length) return _nl2br(esc(text));

  // 2. Render each part
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i];
    if (p.type === 'text') {
      if (p.content.trim()) result += '<div class="' + cls + '">' + _nl2br(esc(p.content)) + '</div>';
    } else {
      if (p.lang === 'diff') result += renderDiffBlock(p.content);
      else if (p.lang === 'shell' || p.lang === 'bash' || p.lang === 'sh') result += renderShellBlock(p.content);
      else result += renderGenericCodeBlock(p.content, p.lang);
    }
  }
  return result || _nl2br(esc(text));
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
  return '<div class="code-block diff-block"><div class="code-block-lang">diff</div><pre><code>' + lines + '</code></pre><button class="copy-btn" onclick="copyCode(this)">复制</button></div>';
}
function renderShellBlock(code) {
  var lines = code.split(_NL).map(function(line) {
    var e = esc(line);
    if (/^\$\s/.test(line)) return '<span class="shell-cmd">' + e + '</span>';
    return e;
  }).join(_NL);
  return '<div class="code-block shell-block"><div class="code-block-lang">shell</div><pre><code>' + lines + '</code></pre><button class="copy-btn" onclick="copyCode(this)">复制</button></div>';
}
function renderGenericCodeBlock(code, lang) {
  var langLabel = lang || 'code';
  return '<div class="code-block"><div class="code-block-lang">' + esc(langLabel) + '</div><pre><code>' + esc(code) + '</code></pre><button class="copy-btn" onclick="copyCode(this)">复制</button></div>';
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

// ---- Tab switching ----
function switchTab(tab) {
  currentTab = tab;
  try { window.sessionStorage.setItem('redmineLastTab', tab); } catch(_) {}
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.toggle('active', t.id === 'tab-' + tab));
  if (tab === 'issues') loadIssues();
  else if (tab === 'runs') loadRuns();
  else if (tab === 'department') loadDepartmentOverdue(false);
  else if (tab === 'project') loadProjectDashboard(false);
  else if (tab === 'stats') loadStatistics();
}

async function refreshCurrentTab() {
  var refreshBtns = document.querySelectorAll('.btn-group .secondary');
  var targetBtn = null;
  refreshBtns.forEach(function(b) { if (b.textContent.includes('刷新')) targetBtn = b; });
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = '⏳ 刷新中...'; }
  try {
    await (currentTab === 'issues' ? loadIssues() : currentTab === 'runs' ? loadRuns() : currentTab === 'department' ? loadDepartmentOverdue(true) : currentTab === 'project' ? loadProjectDashboard(true) : loadStatistics());
  } finally {
    if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = '刷新'; }
  }
}

async function initStatsUserSelect() {
  if (statsUserInitialized) return;
  statsUserInitialized = true;
  var select = document.getElementById('statsUserSelect');
  if (!select) return;
  try {
    var data = await api('/api/redmine-agent/users');
    var items = (data.items || []).slice().sort(function(a, b) { return (a.name || '').localeCompare(b.name || ''); });
    select.innerHTML = '<option value="">当前登录用户</option>' + items.map(function(item) {
      var name = item.name || '';
      return '<option value="' + esc(name) + '">' + esc(name) + '</option>';
    }).join('');
    var q = new URLSearchParams(window.location.search);
    var name = q.get('name') || '';
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
  document.getElementById('statsContent').innerHTML = '<div class="muted" style="padding:20px;text-align:center">⏳ 正在加载 ' + esc(name || '当前用户') + ' 的统计数据...</div>';
  try {
    await loadStatistics();
  } finally {
    if (select) select.disabled = false;
  }
}

// ---- Shared modal helpers ----
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') document.querySelectorAll('.modal.show').forEach(function(m) { m.classList.remove('show'); });
});
document.addEventListener('click', function(e) {
  if (e.target && e.target.classList && e.target.classList.contains('modal')) e.target.classList.remove('show');
});
function showModal(id) { document.getElementById(id).classList.add('show'); }
function hideModal(id) { document.getElementById(id).classList.remove('show'); }
function notifyUser(title, message, level) {
  level = level || 'info';
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({type:'redmine-agent-notification', title:title, message:message, level:level}, '*');
    }
  } catch (_) {}
  var old = document.getElementById('redmine-local-toast');
  if (old) old.remove();
  var toast = document.createElement('div');
  toast.id = 'redmine-local-toast';
  toast.textContent = title + (message ? ': ' + message : '');
  toast.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:10000;max-width:min(460px,calc(100vw - 32px));padding:10px 12px;border-radius:6px;background:#111827;color:#f8fafc;border:1px solid #334155;box-shadow:0 8px 24px rgba(0,0,0,.28);font-size:12px;';
  document.body.appendChild(toast);
  setTimeout(function(){ if (toast.parentNode) toast.remove(); }, 3600);
}

// 趋势柱状图点击：粒度+标签 → 日期范围 [start, end)（ISO，闭开区间）
function utcDateText(date) {
  return date.toISOString().slice(0, 10);
}
function utcDate(year, monthIndex, day) {
  return new Date(Date.UTC(year, monthIndex, day));
}
function trendLabelToDateRange(granularity, label) {
  label = String(label || '');
  if (granularity === 'date') {
    var parts = label.split('-').map(function(v) { return parseInt(v, 10); });
    if (parts.length !== 3 || !parts[0] || !parts[1] || !parts[2]) return null;
    var d = utcDate(parts[0], parts[1] - 1, parts[2]);
    if (isNaN(d.getTime())) return null;
    return [label, utcDateText(new Date(d.getTime() + 86400000))];
  }
  if (granularity === 'month') {
    var mp = label.split('-').map(function(v) { return parseInt(v, 10); });
    if (mp.length !== 2 || !mp[0] || !mp[1]) return null;
    var m = utcDate(mp[0], mp[1] - 1, 1);
    if (isNaN(m.getTime())) return null;
    return [label + '-01', utcDateText(utcDate(mp[0], mp[1], 1))];
  }
  if (granularity === 'year') {
    var y = parseInt(label, 10); if (!y) return null;
    return [y + '-01-01', (y + 1) + '-01-01'];
  }
  if (granularity === 'week') {
    var match = /^(\d{4})-W(\d{2})$/.exec(label);
    if (!match) return null;
    var year = parseInt(match[1], 10), week = parseInt(match[2], 10);
    var jan4 = utcDate(year, 0, 4);
    var dow = (jan4.getUTCDay() + 6) % 7;
    var week1Monday = new Date(jan4.getTime() - dow * 86400000);
    var ws = new Date(week1Monday.getTime() + (week - 1) * 7 * 86400000);
    return [utcDateText(ws), utcDateText(new Date(ws.getTime() + 7 * 86400000))];
  }
  return null;
}
function displayTrendRange(range) {
  var parts = String(range[1] || '').split('-').map(function(v) { return parseInt(v, 10); });
  var end = parts.length === 3 && parts[0] && parts[1] && parts[2] ? utcDate(parts[0], parts[1] - 1, parts[2]) : null;
  var displayEnd = end ? utcDateText(new Date(end.getTime() - 86400000)) : range[1];
  return range[0] + ' 至 ' + displayEnd;
}
async function showRedmineTrendDetail(granularity, label, namesCsv, profileId) {
  var range = trendLabelToDateRange(granularity, label);
  var title = document.getElementById('trendDetailTitle');
  var body = document.getElementById('trendDetailBody');
  if (!range || !title || !body) { notifyUser('无法解析时段', label, 'warning'); return; }
  title.textContent = '解决Redmine问题明细：' + label + '（' + displayTrendRange(range) + '）';
  body.innerHTML = '<div class="muted">查询中…</div>';
  showModal('trendDetailModal');
  try {
    var names = String(namesCsv || '').trim();
    profileId = String(profileId || '').trim();
    if (!names && redmineTrendNames && redmineTrendNames.length) names = redmineTrendNames.join(',');
    var url = '/api/redmine-agent/statistics/resolved-by-date?start=' + encodeURIComponent(range[0])
      + '&end=' + encodeURIComponent(range[1])
      + (names ? '&names=' + encodeURIComponent(names) : '')
      + (profileId ? '&profile_id=' + encodeURIComponent(profileId) : '');
    var data = await api(url);
    var items = (data && data.items) || [];
    if (!items.length) { body.innerHTML = '<div class="muted">该时段无已解决的问题单。</div>'; return; }
    body.innerHTML = '<div class="muted" style="margin-bottom:8px">共 ' + items.length + ' 条</div><div class="wrap"><table class="dept-table"><thead><tr><th>#</th><th>主题</th><th>状态</th><th>指派人</th><th>解决日期</th></tr></thead><tbody>'
      + items.slice(0, 200).map(function(i) {
        var issueId = i.issue_id || '';
        var issueCell = issueId ? '<a href="' + redmineIssueUrl(issueId) + '" target="_blank">#' + esc(issueId) + '</a>' : '-';
        return '<tr><td>' + issueCell + '</td><td>' + esc((i.subject || '-').slice(0, 60)) + '</td><td>' + esc(i.status_name || '-') + '</td><td>' + esc(i.assigned_to_name || '-') + '</td><td>' + esc(i.closed_on || '-') + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  } catch (e) {
    body.innerHTML = '<span class="error">' + esc(e.message) + '</span>';
  }
}

// ---- Add User Modal ----
async function populateDepartmentSelect(selectId, selectedId, includeAll) {
  await loadStatsConfig();
  var select = document.getElementById(selectId);
  if (!select) return;
  var html = departmentOptionsHtml(selectedId || '', includeAll);
  select.innerHTML = html || '<option value="">暂无部门</option>';
}
async function showAddUserModal() {
  document.getElementById('addUserId').value = '';
  document.getElementById('addUserName').value = '';
  document.getElementById('addUserEmail').value = '';
  var selected = (currentTab === 'department' && departmentProfileId && departmentProfileId !== 'all') ? departmentProfileId : '';
  await populateDepartmentSelect('addUserDepartment', selected, false);
  showModal('addUserModal');
  document.getElementById('addUserId').focus();
}
function hideAddUserModal() { hideModal('addUserModal'); }
async function submitAddUser() {
  var id = document.getElementById('addUserId').value.trim();
  var name = document.getElementById('addUserName').value.trim();
  var email = document.getElementById('addUserEmail').value.trim();
  var profileId = (document.getElementById('addUserDepartment') || {}).value || '';
  if (!id || !name) { notifyUser('添加用户失败', '请输入用户 ID 和姓名', 'warning'); return; }
  try {
    await api('/api/redmine-agent/users', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: Number(id), name: name, email: email, department_id: profileId})
    });
    hideModal('addUserModal');
    statsUserInitialized = false;
    _statsConfigCacheTs = 0;
    await initStatsUserSelect();
    document.getElementById('statsUserSelect').value = name;
    if (currentTab === 'department') loadDepartmentOverdue(true);
    else onStatsUserChange();
  } catch (e) { notifyUser('添加用户失败', e.message, 'error'); }
}

// ---- Add Department Modal ----
function showAddDepartmentModal(targetSelectId) {
  pendingDepartmentTargetSelect = targetSelectId || 'departmentProfileSelect';
  document.getElementById('addDepartmentName').value = '';
  document.getElementById('addDepartmentId').value = '';
  showModal('addDepartmentModal');
  document.getElementById('addDepartmentName').focus();
}
function hideAddDepartmentModal() { hideModal('addDepartmentModal'); }
async function submitAddDepartment() {
  var name = document.getElementById('addDepartmentName').value.trim();
  var id = document.getElementById('addDepartmentId').value.trim();
  if (!name) { notifyUser('添加部门失败', '请输入部门名称', 'warning'); return; }
  try {
    var result = await api('/api/redmine-agent/dashboard/profiles', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, id: id})
    });
    hideAddDepartmentModal();
    _statsConfigCacheTs = 0;
    await loadStatsConfig();
    var profile = result.profile || {};
    if (pendingDepartmentTargetSelect === 'addUserDepartment') {
      await populateDepartmentSelect('addUserDepartment', profile.id || '', false);
    } else {
      departmentProfileId = profile.id || departmentProfileId;
      loadDepartmentOverdue(true);
    }
  } catch (e) { notifyUser('添加部门失败', e.message, 'error'); }
}

// ---- Add Project Modal ----
function showAddProjectModal() {
  document.getElementById('addProjectName').value = '';
  document.getElementById('addProjectId').value = '';
  showModal('addProjectModal');
  document.getElementById('addProjectName').focus();
}
function hideAddProjectModal() { hideModal('addProjectModal'); }
async function submitAddProject() {
  var name = document.getElementById('addProjectName').value.trim();
  var projectId = document.getElementById('addProjectId').value.trim();
  if (!projectId) { notifyUser('添加项目失败', '请输入项目标识', 'warning'); return; }
  try {
    var result = await api('/api/redmine-agent/dashboard/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, project_id: projectId})
    });
    hideAddProjectModal();
    _statsConfigCacheTs = 0;
    await loadStatsConfig();
    projectProfileId = (result.profile || {}).id || projectProfileId;
    loadProjectDashboard(true);
  } catch (e) { notifyUser('添加项目失败', e.message, 'error'); }
}

// ---- Settings Modal ----
function showSettingsModal() {
  showModal('settingsModal');
  (async function() {
    try {
      await loadStatsConfig();
      document.getElementById('settingStaleDays').value = statsConfig.stale_days || 20;
      document.getElementById('settingWindowDays').value = statsConfig.window_days || 0;
      document.getElementById('settingCacheTtl').value = statsConfig.cache_ttl || 600;
      // SMTP fields from statsConfig (returned by get_stats_config)
      var cfg = await api('/api/redmine-agent/config/stats');
      var email = (cfg.dashboard || {}).email || {};
      document.getElementById('settingSmtpHost').value = email.smtp_host || '';
      document.getElementById('settingSmtpPort').value = email.smtp_port || 465;
      document.getElementById('settingFromAddr').value = email.from_addr || email.default_from_addr || '';
      document.getElementById('settingSmtpUser').value = email.username || '';
      document.getElementById('settingSmtpPass').value = '';
      // Redmine 凭据状态（已配置则回显用户名，密码不回显）
      try {
        var creds = await api('/api/redmine-agent/config/credentials');
        document.getElementById('settingRedmineUser').value = (creds && creds.username) || '';
      } catch (_) {}
      document.getElementById('settingRedminePass').value = '';
    } catch (_) {}
  })();
}
function hideSettingsModal() { hideModal('settingsModal'); }
async function saveSettings() {
  var stale = parseInt(document.getElementById('settingStaleDays').value) || 20;
  var window_ = parseInt(document.getElementById('settingWindowDays').value) || 60;
  var cacheTtl = parseInt(document.getElementById('settingCacheTtl').value) || 600;
  try {
    // Save stats config
    var result = await api('/api/redmine-agent/config/stats', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({stale_days: stale, window_days: window_, cache_ttl: cacheTtl})
    });
    if (result) { statsConfig = Object.assign({}, statsConfig, result); _statsConfigCacheTs = Date.now(); }
    // Save SMTP config
    var smtpHost = document.getElementById('settingSmtpHost').value.trim();
    var smtpPort = parseInt(document.getElementById('settingSmtpPort').value) || 465;
    var fromAddr = document.getElementById('settingFromAddr').value.trim();
    var smtpUser = document.getElementById('settingSmtpUser').value.trim();
    var smtpPass = document.getElementById('settingSmtpPass').value;
    await api('/api/redmine-agent/config/email', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({smtp_host: smtpHost, smtp_port: smtpPort, from_addr: fromAddr, username: smtpUser, password: smtpPass})
    });
    // Redmine 凭据（仅当填写了密码才保存，避免误清空）
    var redmineUser = document.getElementById('settingRedmineUser').value.trim();
    var redminePass = document.getElementById('settingRedminePass').value;
    if (redminePass) {
      await api('/api/redmine-agent/config/credentials', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: redmineUser, password: redminePass})
      });
    }
    _statsConfigCacheTs = 0; // force reload
    hideSettingsModal();
    refreshCurrentTab();
  } catch (e) { notifyUser('保存设置失败', e.message, 'error'); }
}

// ---- Smart search: detect issue ID and fetch from Redmine ----
async function smartSearch() {
  var q = document.getElementById('searchInput').value.trim();
  if (!q) { loadIssues(); return; }
  // Detect issue ID pattern: #634227, 634227, or pure number
  var idMatch = q.match(/^#?(\d{4,})$/);
  if (idMatch) {
    var issueId = parseInt(idMatch[1]);
    // Check local DB first
    try {
      var local = await api('/api/redmine-agent/issues/' + issueId);
      if (local && local.issue_id) {
        loadIssues();
        return;
      }
    } catch (_) {
      // Not found locally — fetch from Redmine
    }
    // Fetch from Redmine
    await fetchIssueFromRedmine(issueId);
  } else {
    loadIssues();
  }
}

async function fetchIssueFromRedmine(issueId) {
  var btn = document.getElementById('scanBtn');
  var origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ 拉取 #' + issueId + '...';
  try {
    var result = await api('/api/redmine-agent/issues/' + issueId + '/fetch', {method: 'POST'});
    if (result.action === 'exists') {
      document.getElementById('searchInput').value = '';
      loadIssues();
      return;
    }
    // Wait for analysis to complete
    btn.textContent = '⏳ 分析 #' + issueId + '...';
    await waitForRun(result.run_id);
    document.getElementById('searchInput').value = '';
    loadIssues();
  } catch (e) {
    notifyUser('拉取工单失败', e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

// ---- Issues list ----
async function loadIssues(page) {
  if (page) currentPage = page;
  const search = document.getElementById('searchInput').value.trim();
  const status = document.getElementById('statusFilter').value;
  const priority = document.getElementById('priorityFilter').value;
  const offset = (currentPage - 1) * pageSize;
  let url = `/api/redmine-agent/issues?limit=${pageSize}&offset=${offset}`;
  if (search) url += `&search=${encodeURIComponent(search)}`;
  if (status) url += `&status=${encodeURIComponent(status)}`;
  if (priority) url += `&priority=${encodeURIComponent(priority)}`;
  try {
    const data = await api(url);
    renderIssuesList(data.items || []);
    renderPagination(data.total || 0, data.limit, data.offset);
  } catch (e) {
    document.getElementById('issuesList').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

function renderIssuesList(issues) {
  const box = document.getElementById('issuesList');
  if (!issues.length) {
    box.innerHTML = '<div class="muted" style="padding:20px">暂无工单数据。点击"全量同步"按钮拉取所有指派给你的 Redmine 工单。</div>';
    return;
  }
  box.innerHTML = issues.map(renderIssueCard).join('');
}

function renderIssueCard(item) {
  const refs = item.references_json || [];
  const failures = item.failures_json || [];
  const ai = item.ai_json || {};

  // Extract seven fields
  const title = esc(item.subject || ai.title || '-');
  const problemDesc = esc(item.problem_description || item.description || '-');
  const errorInfoRaw = item.error_info || _extractErrorHtml(failures) || '-';
  const errorAnalysis = esc(item.error_analysis || ai.root_cause_guess || '-');
  const solutionRaw = item.solution || ai.solution || '-';
  const patchRaw = item.patch_direction || ai.patch_direction || '-';

  const statusClass = ['已关闭','Closed','已解决','Resolved'].includes(item.status_name) ? 'ok' :
                      ['紧急','Urgent'].includes(item.priority_name) ? 'high' :
                      ['高','High'].includes(item.priority_name) ? 'medium' : '';

  // Build combined error info: test module/case + error stack trace in one code block
  var errorInfoCombined = errorInfoRaw;
  if (failures && failures.length) {
    var f0 = failures[0];
    var header = '';
    if (f0.module) header += '测试模块: ' + f0.module + _NL;
    if (f0.name) header += '测试用例: ' + f0.name + _NL;
    if (header) header += _NL;
    // Prepend test info before the error code block
    // If errorInfoRaw starts with ```, insert after the opening fence
    if (errorInfoRaw.startsWith(_F3) || errorInfoRaw.startsWith(_BT+_BT+_BT)) {
      // Find the first newline after ```
      var nlIdx = errorInfoRaw.indexOf(_NL);
      if (nlIdx > 0) {
        errorInfoCombined = errorInfoRaw.substring(0, nlIdx + 1) + header + errorInfoRaw.substring(nlIdx + 1);
      } else {
        errorInfoCombined = header + errorInfoRaw;
      }
    } else {
      errorInfoCombined = header + errorInfoRaw;
    }
  }

  // Build references HTML — full display, no truncation
  let refsHtml = '';
  if (refs.length) {
    refsHtml = refs.map(r => {
      const level = r.similarity_level || 'low';
      const score = (r.score || 0).toFixed(0);
      const levelText = level === 'high' ? '高' : level === 'medium' ? '中' : '低';
      return `<div class="ref-item">
        <a href="${redmineIssueUrl(r.issue_id)}" target="_blank">#${r.issue_id}</a>
        <span class="ref-badge ${level}">${levelText} ${score}</span>
        <span class="ref-title">${esc(r.subject || '')}</span>
      </div>`;
    }).join('');
  } else {
    refsHtml = '<div class="muted">暂无参考单</div>';
  }

  // Detect issue type: GMS certification or SDK platform
  var issueType = 'SDK';
  var comp = (item.component || '').toUpperCase();
  var cat = (item.category || '').toUpperCase();
  var fv = (item.fixed_version || '').toUpperCase();
  if (comp.includes('GMS') || cat.includes('GMS') || fv.includes('GMS')) issueType = 'GMS';

  // Detect status display
  var statusName = item.status_name || '-';
  var statusIcon = '';
  if (['已关闭','Closed'].includes(statusName)) statusIcon = '✅ ';
  else if (['已解决','Resolved'].includes(statusName)) statusIcon = '✓ ';
  else if (['新建','New'].includes(statusName)) statusIcon = '🆕 ';
  var isClosed = ['已关闭','Closed','已解决','Resolved'].includes(statusName);

  return `<div class="issue-card">
    <h3>
      <a href="${redmineIssueUrl(item.issue_id)}" target="_blank">#${item.issue_id}</a>
      <span>${title}</span>
      <span style="margin-left:auto;font-size:12px;color:var(--muted)">${esc(item.priority_name || '-')}</span>
    </h3>

    <div class="field-label">📋 基本信息</div>
    <table class="info-table">
      <tr>
        <th>SoC</th><td><strong>${esc(item.soc_platform || '-')}</strong></td>
        <th>Android</th><td><strong>${esc(item.android_version || '-')}</strong></td>
        <th>类型</th><td>${esc(issueType)}</td>
        <th>分类</th><td>${esc(item.category || '-')}</td>
        <th>状态</th><td>${statusIcon}${esc(statusName)}</td>
        <th>指派</th><td>${esc(item.assigned_to_name || '-')}</td>
        <th>创建</th><td>${esc((item.created_on || '-').slice(0, 10))}</td>
      </tr>
    </table>

    <div class="field">
      <div class="field-label">📝 问题描述</div>
      <div class="field-content">${trunc(problemDesc, 500)}</div>
    </div>

    <div class="field">
      <div class="field-label">🔴 报错信息</div>
      ${renderFormattedContent(trunc(errorInfoCombined, 2000), 'field-content error-section')}
    </div>

    <div class="field">
      <div class="field-label">🔍 报错分析</div>
      <div class="field-content">${trunc(errorAnalysis, 800)}</div>
    </div>

    <div class="field">
      <div class="field-label">✅ 解决方案</div>
      <div class="field-content solution-section">${renderFormattedContent(trunc(solutionRaw, 1500), 'field-content solution-section')}</div>
    </div>

    ${patchRaw && patchRaw !== '-' && patchRaw !== '需要进一步分析具体日志和源码' ? `<div class="field">
      <div class="field-label">🔧 解决补丁</div>
      ${renderFormattedContent(patchRaw, 'field-content')}
    </div>` : ''}

    ${refs.length ? `<div class="field">
      <div class="field-label">📎 参考Redmine</div>
      ${refsHtml}
    </div>` : ''}

    <details><summary>📄 完整文档</summary><div class="formatted-doc">${renderFormattedContent(item.doc_content || '', 'field-content')}</div></details>
  </div>`;
}

function _extractErrorHtml(failures) {
  if (!failures || !failures.length) return '';
  return failures.slice(0, 3).map(f => `[${f.module || '-'}] ${f.name || '-'}: ${trunc(f.reason || '', 200)}`).join(_NL);
}

function renderPagination(total, limit, offset) {
  const box = document.getElementById('issuesPagination');
  const pages = Math.ceil(total / limit);
  const current = Math.floor(offset / limit) + 1;
  if (pages <= 1) { box.innerHTML = `<div class="muted">共 ${total} 条</div>`; return; }
  let html = '';
  if (current > 1) html += `<button onclick="loadIssues(${current-1})">上一页</button>`;
  html += `<span class="muted" style="line-height:32px">第 ${current}/${pages} 页 (共${total}条)</span>`;
  if (current < pages) html += `<button onclick="loadIssues(${current+1})">下一页</button>`;
  box.innerHTML = html;
}

// ---- Runs ----
async function loadRuns() {
  try {
    const data = await api('/api/redmine-agent/runs?limit=30');
    const items = data.items || [];
    const box = document.getElementById('runsList');
    box.innerHTML = items.map(run => `
      <div class="run-item ${run.run_id === currentRunId ? 'active' : ''}" onclick="loadRun('${esc(run.run_id)}')">
        <div class="run-item-title">${esc(run.started_at || run.run_id)}</div>
        <div class="run-item-meta">${esc(run.status)} | mode=${esc(run.mode)} | issues ${run.issue_count || 0} | done ${run.processed_count || 0}</div>
      </div>`).join('') || '<div class="muted" style="padding:12px">暂无扫描记录</div>';
    if (!currentRunId && items.length) loadRun(items[0].run_id);
  } catch (e) {
    document.getElementById('runsList').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

async function loadRun(runId) {
  currentRunId = runId;
  try {
    const data = await api('/api/redmine-agent/runs/' + encodeURIComponent(runId));
    document.getElementById('runDetailTitle').textContent = '日报详情 ' + runId;
    const issues = data.issues || [];
    document.getElementById('runDetail').innerHTML = `
      <div class="muted">状态: ${esc(data.run.status)} | 报告: ${esc(data.run.report_path || '-')}</div>
      <div style="height:10px"></div>
      ${issues.map(renderIssueCard).join('') || '<div class="muted">没有扫描到问题。</div>'}`;
    loadRuns();
  } catch (e) {
    document.getElementById('runDetail').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

// ---- Statistics ----
function trendStartDate(chartKey) {
  return ((trendDateRange(chartKey) || {}).start || '').trim();
}
function trendEndDate(chartKey) {
  return ((trendDateRange(chartKey) || {}).end || '').trim();
}
function trendDateRange(chartKey) {
  var ranges = statsConfig.chart_date_ranges || {};
  return ranges[chartKey] || {};
}
function filterTrendItems(items, keyName, chartKey) {
  var start = trendStartDate(chartKey);
  var end = trendEndDate(chartKey);
  if (!start && !end) return items || [];
  return (items || []).filter(function(item) {
    var label = String(item[keyName] || '');
    var minLabel = start;
    var maxLabel = end;
    if (keyName === 'week') {
      minLabel = start ? start.slice(0, 4) + '-W' + startWeekNumber(start) : '';
      maxLabel = end ? end.slice(0, 4) + '-W' + startWeekNumber(end) : '';
    } else if (keyName === 'month') {
      minLabel = start ? start.slice(0, 7) : '';
      maxLabel = end ? end.slice(0, 7) : '';
    } else if (keyName === 'year') {
      minLabel = start ? start.slice(0, 4) : '';
      maxLabel = end ? end.slice(0, 4) : '';
    }
    return (!minLabel || label >= minLabel) && (!maxLabel || label <= maxLabel);
  });
}
function startWeekNumber(dateText) {
  var d = new Date(dateText + 'T00:00:00');
  if (isNaN(d.getTime())) return '01';
  d.setHours(0,0,0,0);
  d.setDate(d.getDate() + 3 - (d.getDay() + 6) % 7);
  var week1 = new Date(d.getFullYear(), 0, 4);
  var week = 1 + Math.round(((d - week1) / 86400000 - 3 + (week1.getDay() + 6) % 7) / 7);
  return String(week).padStart(2, '0');
}
async function setTrendStartDate(chartKey, title) {
  pendingTrendChartKey = chartKey || '';
  document.getElementById('trendStartModalTitle').textContent = title + ' 日期范围';
  document.getElementById('trendStartDateInput').value = trendStartDate(chartKey);
  document.getElementById('trendEndDateInput').value = trendEndDate(chartKey);
  showModal('trendStartModal');
  setTimeout(function() {
    var input = document.getElementById('trendStartDateInput');
    if (!input) return;
    input.focus();
    if (typeof input.showPicker === 'function') {
      try { input.showPicker(); } catch (_) {}
    }
  }, 50);
}
function hideTrendStartModal() { hideModal('trendStartModal'); }
async function clearTrendStartDate() {
  document.getElementById('trendStartDateInput').value = '';
  document.getElementById('trendEndDateInput').value = '';
  await saveTrendStartDate();
}
async function saveTrendStartDate() {
  var chartKey = pendingTrendChartKey;
  var start = (document.getElementById('trendStartDateInput').value || '').trim();
  var end = (document.getElementById('trendEndDateInput').value || '').trim();
  if (!chartKey) return;
  if (start && end && start > end) {
    var tmp = start; start = end; end = tmp;
  }
  var ranges = Object.assign({}, statsConfig.chart_date_ranges || {});
  if (start || end) ranges[chartKey] = Object.assign({}, start ? {start: start} : {}, end ? {end: end} : {});
  else delete ranges[chartKey];
  try {
    var result = await api('/api/redmine-agent/config/stats', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({chart_date_ranges: ranges})
    });
    statsConfig = Object.assign({}, statsConfig, result);
    _statsConfigCacheTs = Date.now();
    hideTrendStartModal();
    refreshCurrentTab();
  } catch (e) {
    notifyUser('保存起始时间失败', e.message, 'error');
  }
}
function renderTrend(title, items, keyName, chartKey, detailNames, detailProfileId) {
  chartKey = chartKey || title;
  const filtered = filterTrendItems(items || [], keyName, chartKey);
  const reversed = filtered.slice().reverse();
  const max = Math.max(1, ...reversed.map(item => Number(item.count || 0)));
  const rows = reversed.map(item => {
    const label = item[keyName] || '-';
    const count = Number(item.count || 0);
    const pct = Math.max(5, Math.round((count / max) * 100));
    const namesArg = Array.isArray(detailNames) ? detailNames.join(',') : String(detailNames || '');
    const profileArg = String(detailProfileId || '');
    const clickAttr = count > 0 ? ` style="cursor:pointer" onclick="showRedmineTrendDetail('${esc(keyName)}','${esc(String(label))}','${esc(namesArg)}','${esc(profileArg)}')" title="点击查看该时段解决的问题单"` : '';
    return `<div class="bar-row"${clickAttr}>
      <div class="bar-label">${esc(label)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
      <div class="bar-count">${count}</div>
    </div>`;
  }).join('');
  var start = trendStartDate(chartKey);
  var end = trendEndDate(chartKey);
  var tip = (start || end) ? ('范围: ' + (start || '不限') + ' 至 ' + (end || '不限')) : '设置统计日期范围';
  return `<section class="trend-panel">
    <div class="trend-title-row">
      <h3>${esc(title)}</h3>
      <button class="trend-start-btn" onclick="setTrendStartDate('${esc(chartKey)}','${esc(title)}')" title="${esc(tip)}">⚙</button>
    </div>
    <div class="trend-body">${rows || '<div class="muted">暂无已解决数据</div>'}</div>
  </section>`;
}

function renderMiniIssueList(title, items, emptyText, sectionId) {
  const rows = (items || []).map(item => {
    const issueId = item.issue_id || '';
    const reply = item.last_external_reply_by ? `最后回复: ${item.last_external_reply_by}` :
      (item.last_owner_reply_by ? `最后回复: ${item.last_owner_reply_by}` : `附件: ${item.attachment_count || 0}`);
    const note = item.last_external_reply || item.last_owner_reply || '';
    const time = item.last_external_reply_at || item.last_owner_reply_at || item.updated_on || item.created_on || '-';
    return `<div class="issue-mini">
      <div><a href="${redmineIssueUrl(issueId)}" target="_blank">#${issueId}</a><div class="muted">${esc(item.status_name || '-')}</div></div>
      <div class="issue-mini-title">
        <strong title="${esc(item.subject || '')}">${esc(item.subject || '-')}</strong>
        <span>${esc(reply)}${note ? ' | ' + esc(trunc(note, 120)) : ''}</span>
      </div>
      <div class="issue-mini-meta">${esc(item.priority_name || '-')}<br>${esc(String(time).slice(0, 16))}</div>
    </div>`;
  }).join('');
  return `<section class="stats-section" id="${sectionId || ''}"><h2>${esc(title)}</h2><div class="issue-mini-list">${rows || `<div class="muted">${esc(emptyText || '暂无数据')}</div>`}</div></section>`;
}

function renderGroupCards(title, data) {
  const cards = Object.entries(data || {}).map(([k,v]) => `<div class="stat-card"><div class="value">${v}</div><div class="label">${esc(k)}</div></div>`).join('');
  return `<section class="stats-section"><h2>${esc(title)}</h2><div class="stats-grid">${cards || '<div class="muted">无数据</div>'}</div></section>`;
}
function renderSummaryHeader(title, controlsHtml, metaHtml) {
  return `<div class="dashboard-summary-header">
    <h2 class="dashboard-summary-title">${esc(title)}</h2>
    <div class="dashboard-summary-controls">${controlsHtml || ''}</div>
    <div class="muted dashboard-summary-meta">${metaHtml || ''}</div>
  </div>`;
}
function renderStatsCards(cards) {
  return '<div class="stats-grid">' + (cards || []).map(function(card) {
    var cls = card.className ? ' ' + card.className : '';
    var onclick = card.onclick ? ' onclick="' + card.onclick + '"' : '';
    return '<div class="stat-card' + cls + '"' + onclick + '><div class="value">' + esc(card.value == null ? 0 : card.value) + '</div><div class="label">' + esc(card.label || '') + '</div></div>';
  }).join('') + '</div>';
}

function redmineIssueIds(items) {
  return (items || []).map(function(item) { return item.issue_id || ''; }).filter(Boolean);
}

function copyDepartmentIssues(userId, btn) {
  var user = (window._departmentUsers || []).find(function(item) { return String(item.id || '') === String(userId || ''); });
  var urls = redmineIssueUrls((user || {}).overdue_issues || []);
  copyText(urls.join(_NL), btn);
}
function copyProjectIssues(userId, btn) {
  var user = (window._projectUsers || []).find(function(item) { return String(item.id || '') === String(userId || ''); });
  var urls = redmineIssueUrls((user || {}).issues || []);
  copyText(urls.join(_NL), btn);
}

async function sendDepartmentReminder(userId, btn) {
  var user = (window._departmentUsers || []).find(function(item) { return String(item.id || '') === String(userId || ''); });
  var ids = redmineIssueIds((user || {}).overdue_issues || []);
  if (!ids.length) {
    notifyUser('没有可发送的问题', '该人员没有超过阈值未回复的 Redmine 问题。', 'info');
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    var data = await api('/api/redmine-agent/reminders/email', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_id: userId, issue_ids: ids})
    });
    notifyUser('提醒邮件已发送', '已发送到 ' + (data.to || '绑定邮箱'), 'success');
  } catch (e) {
    notifyUser('提醒邮件发送失败', e.message || '发送失败', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '邮箱'; }
  }
}
async function sendProjectReminder(userId, btn) {
  var user = (window._projectUsers || []).find(function(item) { return String(item.id || '') === String(userId || ''); });
  var ids = redmineIssueIds((user || {}).issues || []);
  if (!ids.length) {
    notifyUser('没有可发送的问题', '该人员没有项目未关闭 Redmine 问题。', 'info');
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    var data = await api('/api/redmine-agent/reminders/email', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        user_id: userId,
        issue_ids: ids,
        subject: 'Redmine 项目未关闭问题提醒 - ' + (user.name || userId),
        intro: '以下 Redmine 问题在项目看板中仍未关闭，请及时处理：'
      })
    });
    notifyUser('提醒邮件已发送', '已发送到 ' + (data.to || '绑定邮箱'), 'success');
  } catch (e) {
    notifyUser('提醒邮件发送失败', e.message || '发送失败', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '邮箱'; }
  }
}

function saveRedmineProfileState() {
  try {
    window.sessionStorage.setItem('redmineDepartmentProfileId', departmentProfileId || '');
    window.sessionStorage.setItem('redmineProjectProfileId', projectProfileId || '');
  } catch(_) {}
  var url = new URL(window.location.href);
  if (departmentProfileId) url.searchParams.set('dept_profile', departmentProfileId);
  else url.searchParams.delete('dept_profile');
  if (projectProfileId) url.searchParams.set('project_profile', projectProfileId);
  else url.searchParams.delete('project_profile');
  window.history.replaceState({}, '', url.toString());
}

function restoreRedmineProfileState() {
  var q = new URLSearchParams(window.location.search);
  departmentProfileId = q.get('dept_profile') || '';
  projectProfileId = q.get('project_profile') || '';
  try {
    if (!departmentProfileId) departmentProfileId = window.sessionStorage.getItem('redmineDepartmentProfileId') || '';
    if (!projectProfileId) projectProfileId = window.sessionStorage.getItem('redmineProjectProfileId') || '';
  } catch(_) {}
}

function onDepartmentProfileChange() {
  var select = document.getElementById('departmentProfileSelect');
  departmentProfileId = select ? select.value : '';
  saveRedmineProfileState();
  loadDepartmentOverdue(true);
}

function renderDepartmentIssue(item) {
  const issueId = item.issue_id || '';
  const lastAt = item.last_external_reply_at || item.updated_on || '-';
  const days = Number(item.unreplied_days || 0);
  const replyText = item.last_external_reply ? ' | ' + esc(trunc(item.last_external_reply, 140)) : '';
  return `<div class="issue-mini">
    <div><a href="${redmineIssueUrl(issueId)}" target="_blank">#${issueId}</a><div class="muted">${esc(item.status_name || '-')}</div></div>
    <div class="issue-mini-title">
      <strong title="${esc(item.subject || '')}">${esc(item.subject || '-')}</strong>
      <span>最后回复: ${esc(item.last_external_reply_by || '-')} | 未回复 ${days} 天${replyText}</span>
    </div>
    <div class="issue-mini-meta">${esc(item.priority_name || '-')}<br>${esc(String(lastAt).slice(0, 16))}</div>
  </div>`;
}

function renderProjectIssue(item) {
  const issueId = item.issue_id || '';
  const updated = item.updated_on || item.created_on || '-';
  return `<div class="issue-mini">
    <div><a href="${redmineIssueUrl(issueId)}" target="_blank">#${issueId}</a><div class="muted">${esc(item.status_name || '-')}</div></div>
    <div class="issue-mini-title">
      <strong title="${esc(item.subject || '')}">${esc(item.subject || '-')}</strong>
      <span>指派给: ${esc(item.assigned_to_name || '-')}</span>
    </div>
    <div class="issue-mini-meta">${esc(item.priority_name || '-')}<br>${esc(String(updated).slice(0, 16))}</div>
  </div>`;
}

function renderDepartmentOverdue(data) {
  const summary = data.summary || {};
  window._departmentUsers = data.users || [];
  redmineTrendNames = (data.users || []).map(function(u) { return u.name; }).filter(Boolean);
  const users = (data.users || []).slice().sort(function(a, b) {
    return String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN-u-co-pinyin');
  });
  const generatedAt = String(data.generated_at || '-').replace('T', ' ').replace(/:\d{2}$/, '');
  const profile = data.profile || {};
  const sd = data.stale_days || 20;
  departmentProfileId = profile.id || departmentProfileId || '';
  if (data.available_profiles) {
    statsConfig.dashboard = Object.assign({}, statsConfig.dashboard || {}, {profiles: data.available_profiles});
  }
  const profileSelect = `<div class="select-with-add">
    <select id="departmentProfileSelect" onchange="onDepartmentProfileChange()" style="min-width:160px">
      ${departmentOptionsHtml(departmentProfileId, true)}
    </select>
    <button class="select-add-btn" type="button" onclick="showAddDepartmentModal('departmentProfileSelect')" title="添加部门">＋</button>
  </div>`;
  const cards = renderStatsCards([
    {value: summary.open_count || 0, label: '当前未关闭', className: 'warn'},
    {value: summary.waiting_my_reply || 0, label: '待回复', className: 'bad'},
    {value: summary.no_reply_3_days || 0, label: 'RK ' + sd + '天未回复', className: 'bad'},
    {value: summary.customer_no_reply_3_days || 0, label: '客户 ' + sd + '天未回复', className: 'warn'},
    {value: summary.total_owned || 0, label: '历史总数'},
    {value: summary.user_count || 0, label: '配置用户'},
  ]);
  const trends = data.trends || {};
  const trendNames = users.reduce(function(acc, user) {
    (user.owner_names || [user.name]).forEach(function(name) {
      if (name) acc.push(name);
    });
    return acc;
  }, []);
  const trendPanels = `<div class="trend-grid">
    ${renderTrend('每天解决Redmine问题', trends.resolved_daily || [], 'date', 'department_daily', trendNames, departmentProfileId)}
    ${renderTrend('每周解决Redmine问题', trends.resolved_weekly || [], 'week', 'department_weekly', trendNames, departmentProfileId)}
    ${renderTrend('每月解决Redmine问题', trends.resolved_monthly || [], 'month', 'department_monthly', trendNames, departmentProfileId)}
    ${renderTrend('每年解决Redmine问题', trends.resolved_yearly || [], 'year', 'department_yearly', trendNames, departmentProfileId)}
  </div>`;
  const rows = users.map(function(user) {
    const names = (user.owner_names || []).join(' / ');
    const nameLine = esc(user.name || '-');
    const subLine = names ? '<div class="muted">' + esc(names) + '</div>' : '';
    const ids = redmineIssueIds(user.overdue_issues || []);
    const copyDisabled = ids.length ? '' : ' disabled';
    return `<tr style="cursor:pointer" onclick="scrollToSection('dept-user-${esc(user.id || '')}')">
      <td class="col-person"><strong>${nameLine}</strong>${subLine}</td>
      <td>${user.total_owned || 0}</td>
      <td>${user.open_count || 0}</td>
      <td>${user.scanned_open_count || 0}</td>
      <td>${user.waiting_my_reply || 0}</td>
      <td><strong style="color:var(--bad)">${user.no_reply_3_days || 0}</strong></td>
      <td>${user.customer_no_reply_3_days || 0}</td>
      <td>${user.max_unreplied_days || 0}</td>
      <td onclick="event.stopPropagation()">
        <button class="secondary dept-action-btn"${copyDisabled} onclick="copyDepartmentIssues('${esc(user.id || '')}', this)">复制3天未回复工单</button>
        <button class="secondary dept-action-btn"${copyDisabled} onclick="sendDepartmentReminder('${esc(user.id || '')}', this)">邮箱</button>
      </td>
    </tr>`;
  }).join('');
  const table = `<div class="dept-table-wrap">
    <table class="dept-table">
      <thead><tr><th class="col-person">人员</th><th>历史数量</th><th>未关闭</th><th>本地未关闭</th><th>待回复</th><th>RK ${sd}天未回复</th><th>客户 ${sd}天未回复</th><th>最长未回复天数</th><th>操作</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="9" class="muted">暂无配置用户</td></tr>'}</tbody>
    </table>
  </div>`;
  const detailUsers = users.filter(function(user) { return (user.overdue_issues || []).length > 0; });
  const details = detailUsers.map(function(user) {
    const issues = (user.overdue_issues || []).map(renderDepartmentIssue).join('');
    const names = (user.owner_names || []).join(' / ');
    return `<section class="dept-user-block" id="dept-user-${esc(user.id || '')}">
      <div class="dept-user-title">
        <h2>${esc(user.name || '-')} ${sd}天未回复问题 (${(user.overdue_issues || []).length})</h2>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <div class="muted">${esc(names || '-')} | 最长 ${user.max_unreplied_days || 0} 天 | 窗口 ${data.window_days || 0} 天</div>
        </div>
      </div>
      <div class="issue-mini-list">${issues}</div>
    </section>`;
  }).join('');
  document.getElementById('departmentContent').innerHTML = `
    <section class="stats-section">
      ${renderSummaryHeader((profile.name || '部门') + ' Redmine 未回复汇总', '<div class="filter-bar">' + profileSelect + '</div>', '更新时间: ' + esc(generatedAt) + ' | 阈值: ' + esc(data.stale_days || 3) + ' 天 | 缓存: ' + (data.cache_hit ? '是' : '否'))}
      ${cards}
    </section>
    ${trendPanels}
    ${table}
    ${details || '<div class="muted" style="padding:12px">当前配置用户暂无超过 ' + sd + ' 天未回复的问题。</div>'}
  `;
}

async function loadDepartmentOverdue(force) {
  const box = document.getElementById('departmentContent');
  if (!box) return;
  await loadStatsConfig();
  var sd = statsConfig.stale_days || 20;
  box.innerHTML = '<div class="muted" style="padding:20px;text-align:center">⏳ 正在统计部门看板超阈值未回复问题...</div>';
  try {
    var defaults = (statsConfig.dashboard || {}).defaults || {};
    var url = '/api/redmine-agent/statistics/department-overdue?stale_days=' + sd
      + '&list_limit=' + (defaults.list_limit || 50)
      + '&issue_limit=' + (defaults.issue_limit || 500)
      + '&profile_id=' + encodeURIComponent(departmentProfileId || '');
    if (force) url += '&refresh=true';
    const data = await api(url);
    renderDepartmentOverdue(data);
  } catch (e) {
    box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

async function loadStatistics() {
  var savedName = '';
  try {
    var oldSel = document.getElementById('statsUserSelect');
    if (oldSel) savedName = oldSel.value;
  } catch(_) {}
  try {
    await loadStatsConfig();
    var selectedName = savedName || '';
    var q = new URLSearchParams(window.location.search);
    if (!selectedName) selectedName = q.get('name') || '';
    var sd = statsConfig.stale_days || 3;
    var workloadUrl = '/api/redmine-agent/statistics/workload?stale_days=' + sd + '&list_limit=30';
    if (selectedName) workloadUrl += '&name=' + encodeURIComponent(selectedName);
    const [basic, workload] = await Promise.all([
      api('/api/redmine-agent/statistics'),
      api(workloadUrl)
    ]);
    const lists = workload.lists || {};
    const meta = workload.meta || {};
    updateRedmineTrendNames(selectedName, meta);

    const userSelectHtml = '<div class="select-with-add">'
      + '<select id="statsUserSelect" onchange="onStatsUserChange()" style="width:160px">'
      + '<option value="">当前登录用户</option>'
      + '</select>'
      + '<button class="select-add-btn" onclick="showAddUserModal()" title="添加用户">＋</button>'
      + '</div>';

    document.getElementById('statsContent').innerHTML = `
      <section class="stats-section">
        ${renderSummaryHeader('Redmine概览', '<div class="filter-bar">' + userSelectHtml + '</div>', '统计身份: ' + ((meta.owner_names || []).map(esc).join(' / ') || '未识别') + ' | 更新时间: ' + esc((meta.generated_at || '-').replace('T', ' ').replace(/:\d{2}$/, '')))}
        ${renderStatsCards([
          {value: workload.open_count || 0, label: '当前未关闭', className: 'warn'},
          {value: workload.waiting_my_reply || 0, label: '待回复 ⬇', className: 'bad clickable-stat', onclick: "scrollToSection('sec-waiting-reply')"},
          {value: workload.no_reply_3_days || 0, label: 'RK ' + sd + '天未回复客户 ⬇', className: 'bad clickable-stat', onclick: "scrollToSection('sec-no-reply-3d')"},
          {value: workload.customer_no_reply_3_days || 0, label: '客户 ' + sd + '天未回复RK ⬇', className: 'warn clickable-stat', onclick: "scrollToSection('sec-customer-no-reply')"},
          {value: workload.missing_test_report || 0, label: '缺失测试报告 ⬇', className: 'warn clickable-stat', onclick: "scrollToSection('sec-missing-report')"},
          {value: workload.closed_count || 0, label: '已解决 / 已关闭', className: 'ok'},
          {value: workload.total_owned || 0, label: '名下历史数量'},
        ])}
      </section>

      <div class="trend-grid">
        ${renderTrend('每天解决Redmine问题', workload.resolved_daily || [], 'date', 'personal_daily', redmineTrendNames)}
        ${renderTrend('每周解决Redmine问题', workload.resolved_weekly || [], 'week', 'personal_weekly', redmineTrendNames)}
        ${renderTrend('每月解决Redmine问题', workload.resolved_monthly || [], 'month', 'personal_monthly', redmineTrendNames)}
        ${renderTrend('每年解决Redmine问题', workload.resolved_yearly || [], 'year', 'personal_yearly', redmineTrendNames)}
      </div>

      ${renderMiniIssueList('待回复的问题 (' + (lists.waiting_my_reply || []).length + ')', lists.waiting_my_reply || [], '暂无待回复问题', 'sec-waiting-reply')}
      ${renderMiniIssueList('RK ' + sd + '天未回复客户的问题 (' + (lists.no_reply_3_days || []).length + ')', lists.no_reply_3_days || [], '暂无RK超过阈值未回复客户问题', 'sec-no-reply-3d')}
      ${renderMiniIssueList('客户 ' + sd + '天未回复RK的问题 (' + (lists.customer_no_reply_3_days || []).length + ')', lists.customer_no_reply_3_days || [], '暂无客户超过阈值未回复RK问题', 'sec-customer-no-reply')}
      ${renderMiniIssueList('缺失测试报告的问题 (' + (lists.missing_test_report || []).length + ')', lists.missing_test_report || [], '暂无缺失测试报告问题', 'sec-missing-report')}
    `;
    statsUserInitialized = false;
    await initStatsUserSelect();
    if (selectedName) {
      var sel = document.getElementById('statsUserSelect');
      if (sel) sel.value = selectedName;
    }
  } catch (e) {
    document.getElementById('statsContent').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

function onProjectProfileChange() {
  var select = document.getElementById('projectProfileSelect');
  projectProfileId = select ? select.value : '';
  saveRedmineProfileState();
  loadProjectDashboard(true);
}
function toggleProjectOpenOnly() {
  projectOpenOnly = !projectOpenOnly;
  renderProjectDashboard(window._projectData || {});
}

function renderProjectDashboard(data) {
  window._projectData = data || {};
  const summary = data.summary || {};
  const profile = data.profile || {};
  projectProfileId = profile.id || projectProfileId || '';
  if (data.available_profiles) {
    statsConfig.dashboard = Object.assign({}, statsConfig.dashboard || {}, {project_profiles: data.available_profiles});
  }
  window._projectUsers = data.assignees || [];
  const generatedAt = String(data.generated_at || '-').replace('T', ' ').replace(/:\d{2}$/, '');
  const profileSelect = `<div class="select-with-add">
    <select id="projectProfileSelect" onchange="onProjectProfileChange()" style="min-width:220px">${projectOptionsHtml(projectProfileId)}</select>
    <button class="select-add-btn" type="button" onclick="showAddProjectModal()" title="添加项目">＋</button>
  </div>`;
  const openOnlyBtn = `<button class="secondary toggle-btn ${projectOpenOnly ? 'active' : ''}" onclick="toggleProjectOpenOnly()">${projectOpenOnly ? '显示全员' : '仅未关闭人员'}</button>`;
  const assignees = (data.assignees || []).slice().filter(function(user) {
    return !projectOpenOnly || Number(user.open_count || 0) > 0;
  }).sort(function(a, b) {
    return String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN-u-co-pinyin');
  });
  const cards = renderStatsCards([
    {value: summary.issue_count || 0, label: '项目总数'},
    {value: summary.assignee_count || 0, label: '涉及人员'},
    {value: summary.open_count || 0, label: '当前未关闭', className: 'warn'},
    {value: summary.closed_count || 0, label: '已解决 / 已关闭', className: 'ok'},
  ]);
  const rows = assignees.map(function(user) {
    const ids = redmineIssueIds(user.issues || []);
    const actionDisabled = ids.length ? '' : ' disabled';
    return `<tr style="cursor:pointer" onclick="scrollToSection('project-user-${esc(user.id || '')}')">
      <td class="col-person"><strong>${esc(user.name || '-')}</strong></td>
      <td>${user.total_owned || 0}</td>
      <td>${user.open_count || 0}</td>
      <td>${user.closed_count || 0}</td>
      <td onclick="event.stopPropagation()">
        <button class="secondary dept-action-btn"${actionDisabled} onclick="copyProjectIssues('${esc(user.id || '')}', this)">复制</button>
        <button class="secondary dept-action-btn"${actionDisabled} onclick="sendProjectReminder('${esc(user.id || '')}', this)">邮箱</button>
      </td>
      <td class="project-filter-cell"></td>
    </tr>`;
  }).join('');
  const table = `<div class="dept-table-wrap">
    <table class="dept-table">
      <thead><tr><th class="col-person">人员</th><th>项目内数量</th><th>未关闭</th><th>已关闭</th><th>操作</th><th class="project-filter-th">${openOnlyBtn || ''}</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="muted">暂无项目人员数据</td></tr>'}</tbody>
    </table>
  </div>`;
  const details = assignees.filter(function(user) { return (user.issues || []).length > 0; }).map(function(user) {
    const issues = (user.issues || []).map(renderProjectIssue).join('');
    return `<section class="dept-user-block" id="project-user-${esc(user.id || '')}">
      <div class="dept-user-title"><h2>${esc(user.name || '-')} 未关闭问题 (${(user.issues || []).length})</h2></div>
      <div class="issue-mini-list">${issues}</div>
    </section>`;
  }).join('');
  document.getElementById('projectContent').innerHTML = `
    <section class="stats-section">
      ${renderSummaryHeader((profile.name || profile.project_id || '项目') + ' Redmine 当前情况', '<div class="filter-bar">' + profileSelect + '</div>', '项目: ' + esc(profile.project_id || '-') + ' | 更新时间: ' + esc(generatedAt) + ' | 缓存: ' + (data.cache_hit ? '是' : '否') + ' | ' + (projectOpenOnly ? '仅显示未关闭人员' : '显示全员'))}
      ${cards}
    </section>
    ${table}
    ${details || '<div class="muted" style="padding:12px">当前项目暂无未关闭问题。</div>'}
  `;
}

async function loadProjectDashboard(force) {
  const box = document.getElementById('projectContent');
  if (!box) return;
  await loadStatsConfig();
  if (!projectProfiles().length) {
    box.innerHTML = '<div class="muted" style="padding:20px">暂无项目看板配置。<button style="margin-left:10px" onclick="showAddProjectModal()">＋ 添加项目</button></div>';
    return;
  }
  box.innerHTML = '<div class="muted" style="padding:20px;text-align:center">⏳ 正在统计项目 Redmine 当前情况...</div>';
  try {
    var selected = projectProfileId || (projectProfiles()[0] || {}).id || '';
    var url = '/api/redmine-agent/statistics/project?profile_id=' + encodeURIComponent(selected);
    if (force) url += '&refresh=true';
    const data = await api(url);
    renderProjectDashboard(data);
  } catch (e) {
    box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

async function startScan() {
  const btn = document.getElementById('scanBtn');
  btn.disabled = true; btn.textContent = '⏳ 扫描中...';
  try {
    const started = await api('/api/redmine-agent/runs?hours=48&max_issues=50', {method:'POST'});
    const rid = started.run_id || '';
    btn.textContent = '⏳ 等待结果...';
    await waitForRun(rid, '扫描');
  } catch (e) { notifyUser('扫描失败', e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = '🔍 扫描'; }
}

async function triggerSync() {
  if (!confirm('确认全量同步所有指派给你的 Redmine 工单？这可能需要几分钟。')) return;
  const btn = document.getElementById('scanBtn');
  try {
    const started = await api('/api/redmine-agent/sync?max_analyze=30', {method:'POST'});
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 同步中...'; }
    await waitForRun(started.run_id, '同步');
  } catch (e) { notifyUser('同步失败', e.message, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '🔍 扫描'; } }
}

async function waitForRun(runId, label) {
  for (let i = 0; i < 240; i++) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const status = await api('/api/redmine-agent/status');
      if (!status.running) {
        refreshCurrentTab();
        notifyUser('RedmineAgent ' + label + '完成', '任务 ' + runId + ' 已完成', 'success');
        return;
      }
    } catch (_) {}
  }
  refreshCurrentTab();
  notifyUser('RedmineAgent ' + label + '超时', '任务 ' + runId + ' 等待超时，请检查状态', 'warning');
}

// ---- Init ----
restoreRedmineProfileState();
var initialTab = new URLSearchParams(window.location.search).get('tab') || (window.sessionStorage.getItem('redmineLastTab') || 'stats');
if (!document.getElementById('tab-' + initialTab)) initialTab = 'stats';
switchTab(initialTab);

// Check if a task is already running on page load — reset button state
(async function() {
  try {
    const status = await api('/api/redmine-agent/status');
    if (!status.running) {
      var btn = document.getElementById('scanBtn');
      if (btn) { btn.disabled = false; btn.textContent = '🔍 扫描'; }
    }
  } catch(_) {}
})();

// Auto-refresh status
setInterval(async () => {
  try {
    const status = await api('/api/redmine-agent/status');
    var btn = document.getElementById('scanBtn');
    if (status.running) {
      document.title = '⏳ RedmineAgent (运行中...)';
    } else {
      document.title = '🔧 RedmineAgent';
      if (btn && btn.disabled) { btn.disabled = false; btn.textContent = '🔍 扫描'; }
    }
  } catch (_) {}
}, 10000);
