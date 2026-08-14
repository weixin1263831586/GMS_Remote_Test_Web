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
    throw new Error(r.status === 401 ? '登录状态已失效，请刷新后重新登录' : detail);
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
  const stop = opts.stopPropagation === false ? '' : ' onclick="event.stopPropagation()"';
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
    // Table: a line with | followed by a separator line |---|
    if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s:-|]+/.test(lines[i+1])) {
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
  var header = rows[0].split('|').map(function(c){return c.trim();}).filter(function(_, idx, arr){ return !(idx === 0 && arr[0] === '') && !(idx === arr.length-1 && arr[arr.length-1] === ''); });
  var body = rows.slice(2).map(function(r){
    var cells = r.split('|').map(function(c){return c.trim();});
    // drop leading/trailing empty from split on |
    if (cells.length && cells[0] === '') cells.shift();
    if (cells.length && cells[cells.length-1] === '') cells.pop();
    return '<tr>' + cells.map(function(c){return '<td>' + _inlineMd(esc(c)) + '</td>';}).join('') + '</tr>';
  }).join('');
  return '<table class="md-table"><thead><tr>' + header.map(function(c){return '<th>' + esc(c) + '</th>';}).join('') + '</tr></thead><tbody>' + body + '</tbody></table>';
}

function _inlineMd(text) {
  // `code`, **bold**, [link](url)
  return String(text || '')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>')
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
  if (tab === 'issues') return loadIssues();
  if (tab === 'cases') return loadCases();
  if (tab === 'runs') return loadRuns();
  if (tab === 'department') return loadDepartmentOverdue(false);
  if (tab === 'project') return loadProjectDashboard(false);
  if (tab === 'stats') return loadStatistics();
  return Promise.resolve();
}

async function refreshCurrentTab() {
  var targetBtn = document.getElementById('refreshBtn');
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = '⏳ 刷新中...'; }
  try {
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
    } else {
      await loadStatistics(true);
    }
  } finally {
    if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = '刷新'; }
  }
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
    await loadStatistics();
  } finally {
    if (select) select.disabled = false;
  }
}

// ---- Shared modal helpers ----
const redmineModalStack = [];
function syncRedmineModalState() {
  const active = redmineModalStack.filter(function(id) {
    const modal = document.getElementById(id);
    return modal && modal.classList.contains('show');
  });
  redmineModalStack.length = 0;
  active.forEach(function(id) { redmineModalStack.push(id); });
  const topIndex = redmineModalStack.length - 1;
  redmineModalStack.forEach(function(id, index) {
    const modal = document.getElementById(id);
    modal.style.zIndex = String(10000 + index * 20);
    modal.inert = index !== topIndex;
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-hidden', index === topIndex ? 'false' : 'true');
    if (index === topIndex) modal.setAttribute('aria-modal', 'true');
    else modal.removeAttribute('aria-modal');
  });
  document.documentElement.classList.toggle('modal-open', redmineModalStack.length > 0);
  document.body.classList.toggle('modal-open', redmineModalStack.length > 0);
}
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && redmineModalStack.length) {
    e.preventDefault();
    e.stopPropagation();
    hideModal(redmineModalStack[redmineModalStack.length - 1]);
  }
});
document.addEventListener('click', function(e) {
  if (e.target && e.target.classList && e.target.classList.contains('modal')
      && redmineModalStack[redmineModalStack.length - 1] === e.target.id) {
    hideModal(e.target.id);
  }
});
function showModal(id) {
  const modal = document.getElementById(id);
  if (!modal) return;
  const index = redmineModalStack.indexOf(id);
  if (index >= 0) redmineModalStack.splice(index, 1);
  redmineModalStack.push(id);
  modal.classList.add('show');
  syncRedmineModalState();
}
function hideModal(id) {
  const modal = document.getElementById(id);
  if (!modal) return;
  modal.classList.remove('show');
  modal.inert = false;
  modal.setAttribute('aria-hidden', 'true');
  modal.removeAttribute('aria-modal');
  modal.style.removeProperty('z-index');
  const index = redmineModalStack.indexOf(id);
  if (index >= 0) redmineModalStack.splice(index, 1);
  syncRedmineModalState();
}
function removeDynamicModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.remove();
  const index = redmineModalStack.indexOf(id);
  if (index >= 0) redmineModalStack.splice(index, 1);
  syncRedmineModalState();
}
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
        <span class="modal-close" onclick="removeDynamicModal('${modalId}')">&times;</span>
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
          <input type="file" id="${fileInputId}" data-redmine-files multiple style="display:none" onchange="updateRedmineReplyFileList('${fileInputId}', '${fileListId}')">
          <div id="${fileInputId}-drop" class="redmine-reply-drop" onclick="document.getElementById('${fileInputId}').click()">拖拽文件到此处，或点击选择文件</div>
          <div id="${fileListId}" class="redmine-reply-file-list"></div>
        </div>
        <div class="modal-buttons">
          <button class="secondary" onclick="removeDynamicModal('${modalId}')">取消</button>
          <button onclick="confirmAndSendRedmineReply('${modalId}')">确认并发送</button>
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
    return `<div class="redmine-reply-file"><span>📎 ${esc(file.name)} <span class="muted">(${formatBytes(file.size) || '0 B'})</span></span><button type="button" onclick="removeRedmineReplyFile('${fileInputId}', '${fileListId}', ${idx})">移除</button></div>`;
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
        var issueCell = issueId ? renderRedmineIssueLink(issueId, {stopPropagation: false}) : '-';
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
      document.getElementById('settingFreshnessDays').value = statsConfig.freshness_days || 180;
      document.getElementById('settingRedmineBaseUrl').value = redmineBaseUrl();
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
  var freshnessDays = parseInt(document.getElementById('settingFreshnessDays').value) || 180;
  var redmineBase = document.getElementById('settingRedmineBaseUrl').value.trim();
  try {
    // Save stats config
    var result = await api('/api/redmine-agent/config/stats', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({base_url: redmineBase, stale_days: stale, window_days: window_, cache_ttl: cacheTtl, freshness_days: freshnessDays})
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
  if (!q) return loadIssues();
  // Detect issue ID pattern: #634227, 634227, or pure number
  var idMatch = q.match(/^#?(\d{4,})$/);
  if (idMatch) {
    var issueId = parseInt(idMatch[1]);
    // Check local DB first
    try {
      var local = await api('/api/redmine-agent/issues/' + issueId);
      if (local && local.issue_id) {
        return loadIssues();
      }
    } catch (_) {
      // Not found locally — fetch from Redmine
    }
    await fetchIssueFromRedmine(issueId);
  } else {
    return loadIssues();
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
      return loadIssues();
    }
    // Wait for analysis to complete
    btn.textContent = '⏳ 分析 #' + issueId + '...';
    await waitForRun(result.run_id, '拉取');
    document.getElementById('searchInput').value = '';
    return loadIssues();
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
  if (pendingWorkspaceIssueId) {
    const card = box.querySelector(`.issue-card[data-issue-id="${CSS.escape(pendingWorkspaceIssueId)}"]`);
    if (card) setTimeout(function() { card.scrollIntoView({behavior: 'smooth', block: 'start'}); }, 0);
  }
}

function renderIssueCard(item) {
  const refs = item.references_json || [];
  const failures = item.failures_json || [];
  const ai = item.ai_json || {};
  const attachments = item.attachment_links || [];
  // 缓存原始富数据，供「存为Wiki」按钮按 issue_id 取回（避免序列化复杂对象进 onclick）
  if (item.issue_id) {
    window.__issueCardCache = window.__issueCardCache || {};
    window.__issueCardCache[String(item.issue_id)] = item;
  }

  // Extract seven fields
  const title = esc(item.subject || ai.title || '-');
  const problemDesc = _buildProblemDescription(item, attachments);
  const errorInfoRaw = item.error_info || _extractErrorHtml(failures) || '-';
  const errorAnalysis = item.error_analysis || ai.root_cause_guess || '-';
  const solutionRaw = item.solution || ai.solution || '-';
  const patchRaw = item.patch_direction || ai.patch_direction || '-';
  const attachmentLinks = attachments;
  const hasPatch = patchRaw && patchRaw !== '-' && patchRaw !== '需要进一步分析具体日志和源码'
    && !String(patchRaw).includes('未从现有证据中提取到明确补丁')
    && !String(patchRaw).includes('当前缺少可定位补丁');

  const statusClass = ['已关闭','Closed','已解决','Resolved'].includes(item.status_name) ? 'ok' :
                      ['紧急','Urgent'].includes(item.priority_name) ? 'high' :
                      ['高','High'].includes(item.priority_name) ? 'medium' : '';

  // 将测试模块、用例和错误堆栈合并为代码块。
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
    refsHtml = '<div class="ref-card-list">' + refs.map(renderReferenceCard).join('') + '</div>';
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

  return `<div class="issue-card" data-issue-id="${esc(item.issue_id)}">
    <h3>
      ${renderRedmineIssueLink(item.issue_id, {stopPropagation: false})}
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
      ${renderIssueRichText(problemDesc)}
    </div>

    <div class="field">
      <div class="field-label">🔴 报错信息</div>
      ${renderIssueRichText(errorInfoCombined, 'rich-field error-rich')}
    </div>

    <div class="field">
      <div class="field-label">🔍 报错分析</div>
      ${renderIssueRichText(errorAnalysis)}
    </div>

    <div class="field">
      <div class="field-label">✅ 解决方案</div>
      <div class="solution-section">${renderMarkdownDoc(solutionRaw)}</div>
    </div>

    <div class="field">
      <div class="field-label">📎 Redmine附件 / 补丁</div>
      ${renderAttachmentLinks(item.issue_id, attachmentLinks)}
    </div>

    ${hasPatch ? `<div class="field">
      <div class="field-label">🔧 解决补丁</div>
      ${renderIssueRichText(patchRaw)}
    </div>` : ''}

    ${refs.length ? `<div class="field">
      <div class="field-label">📎 参考Redmine</div>
      ${refsHtml}
    </div>` : ''}

    <details class="issue-doc-details" ontoggle="loadIssueDocOnToggle(this, ${item.issue_id})">
      <summary>📄 完整文档</summary>
      <div class="formatted-doc muted">展开后加载完整文档…</div>
    </details>

    <div class="knowledge-actions">
      <button class="ka-btn primary" onclick="agentReplyDraft(${item.issue_id}, this)" title="复用报告分析风格生成 Redmine 回复草稿、根因和补丁方向">✉️ Redmine回复</button>
      <button class="ka-btn" onclick="refreshIssueMetadata(${item.issue_id})" title="只刷新Redmine历史回复和附件元数据，不下载附件">🔄 刷新附件元数据</button>
      <button class="ka-btn" onclick="toggleIssueWorkbench(${item.issue_id})" title="展开相似工单、历史回复和附件解析摘要">🧩 展开依据</button>
      <button class="ka-btn" onclick="saveIssueToWiki(${item.issue_id})" title="把该工单存入 Wiki「Redmine问题沉淀」分类，并建立外链">📥 存为Wiki</button>
      <button class="ka-btn" onclick="navigateFromRedmineIssue('reports', ${item.issue_id})" title="保留工单上下文并打开测试报告">📊 关联报告</button>
      <button class="ka-btn" onclick="navigateFromRedmineIssue('automation', ${item.issue_id})" title="保留工单上下文并打开 GMS ATS">⚙️ 关联 ATS</button>
    </div>
    <div id="issue-workbench-${item.issue_id}" class="issue-workbench" style="display:none"></div>
  </div>`;
}

function renderReferenceCard(r) {
  const level = r.similarity_level || 'low';
  const score = Number(r.score || 0).toFixed(0);
  const levelText = level === 'high' ? '高' : level === 'medium' ? '中' : '低';
  return `<div class="ref-card">
    <div class="ref-item">
      ${renderRedmineIssueLink(r.issue_id, {stopPropagation: false})}
      <span class="ref-badge ${level}">${levelText} ${score}</span>
      <span class="ref-title">${esc(r.subject || '')}</span>
    </div>
  </div>`;
}

async function saveIssueToWiki(issueId) {
  const item = (window.__issueCardCache || {})[String(issueId)];
  if (!item) {
    notifyUser('数据缺失', '未找到该工单的富化数据，请重新打开工单后再试', 'error');
    return;
  }
  const subject = item.subject || ('Redmine #' + issueId);
  const module = item.module || '';
  const parts = [];
  parts.push('# ' + subject);
  parts.push('');
  parts.push('- **Redmine Issue**: #' + issueId);
  if (module) parts.push('- **模块**: ' + module);
  if (item.priority) parts.push('- **优先级**: ' + item.priority);
  if (item.status) parts.push('- **状态**: ' + item.status);
  parts.push('');
  const problemDesc = _buildProblemDescription(item, item.attachment_links || []);
  if (problemDesc && String(problemDesc).trim() && String(problemDesc).trim() !== '-') {
    parts.push('## 问题描述');
    parts.push('');
    parts.push(String(problemDesc).trim());
    parts.push('');
  }
  const errorAnalysis = item.error_analysis || (item.ai_json || {}).root_cause_guess || '';
  if (errorAnalysis && String(errorAnalysis).trim() && String(errorAnalysis).trim() !== '-') {
    parts.push('## 错误分析 / 根因');
    parts.push('');
    parts.push(String(errorAnalysis).trim());
    parts.push('');
  }
  const solution = item.solution || (item.ai_json || {}).solution || '';
  if (solution && String(solution).trim() && String(solution).trim() !== '-') {
    parts.push('## 解决方案');
    parts.push('');
    parts.push(String(solution).trim());
    parts.push('');
  }
  const content = parts.join('\n');
  const payload = {
    content_md: content,
    space_id: 'issues',
    title: `Redmine #${issueId} ${item.subject || ''}`.trim(),
    source: 'redmine',
    tags: ['Redmine问题沉淀'].concat(module ? [module] : []),
    links: [
      {target_type: 'redmine_issue', target_id: String(issueId), title: '#' + String(issueId)},
      ...(module ? [{target_type: 'test_case', target_id: module, title: module}] : [])
    ]
  };
  try {
    await api('/api/knowledge/docs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    notifyUser('已存为Wiki', '已存入「Redmine问题沉淀」并关联 Redmine #' + issueId, 'success');
  } catch (e) {
    notifyUser('存为Wiki失败', (e && e.message) || String(e), 'error');
  }
}

function _buildProblemDescription(item, attachments) {
  const parts = [];
  const desc = item.problem_description || item.description || '';
  if (desc && String(desc).trim() && String(desc).trim() !== '-') parts.push(String(desc).trim());
  const attachmentNotes = (attachments || []).map(function(a) {
    const analysis = a.analysis_json || {};
    const details = analysis.details || {};
    const excerpt = analysis.text_excerpt || details.ocr_text || '';
    const detected = details.detected_errors || [];
    if (!excerpt && !detected.length) return '';
    var lines = [`**附件**: ${a.filename || '-'}`];
    if (detected.length) lines.push(`检测到: ${detected.join(' / ')}`);
    if (excerpt) lines.push(excerpt.trim());
    return lines.join('\n');
  }).filter(Boolean);
  if (attachmentNotes.length) {
    parts.push('');
    parts.push('**附件分析**: ');
    parts.push(attachmentNotes.join('\n\n'));
  }
  return parts.join('\n') || '-';
}

function _extractErrorHtml(failures) {
  if (!failures || !failures.length) return '';
  return failures.slice(0, 3).map(f => `[${f.module || '-'}] ${f.name || '-'}: ${trunc(f.reason || '', 200)}`).join(_NL);
}

function renderAttachmentLinks(issueId, attachments) {
  const items = attachments || [];
  if (!items.length) {
    return `<div class="muted">本地暂无附件元数据。可点击“刷新附件元数据”从 Redmine 拉取附件名；或直接打开 <a href="${redmineIssueAttachmentsUrl(issueId)}" target="_blank">Redmine 附件区</a>。</div>`;
  }
  return `<div class="attachment-link-list">${items.map(a => {
    const name = String(a.filename || '');
    const lower = name.toLowerCase();
    const kind = lower.endsWith('.diff') || lower.endsWith('.patch') ? '补丁'
      : (/\.(png|jpg|jpeg|webp|bmp)$/i.test(lower) ? '截图' : '报告');
    const patchDir = kind === '补丁' ? '/vendor/rockchip/modules/power_ext' : '';
    // 优先使用同源代理下载，缺少附件 ID 时打开 Redmine 链接。
    const attId = a.attachment_id || a.id || '';
    const safeName = esc(name || '-');
    const linkHtml = attId
      ? `<a href="/api/redmine-agent/issues/${issueId}/attachments/${encodeURIComponent(attId)}/download" download="${esc(name)}" title="直接下载(不跳转)">${safeName}</a>`
      : `<a href="${esc(a.url || redmineIssueAttachmentsUrl(issueId))}" target="_blank">${safeName}</a>`;
    return `<div class="attachment-link-item">
      <span class="attachment-kind">${esc(kind)}</span>
      ${linkHtml}
      ${patchDir ? `<span class="patch-dir">应用目录：${esc(patchDir)}</span>` : ''}
    </div>`;
  }).join('')}</div>`;
}

async function loadIssueDocOnToggle(details, issueId) {
  if (!details || !details.open || details.dataset.loaded === '1') return;
  const box = details.querySelector('.formatted-doc');
  if (!box) return;
  box.innerHTML = '<div class="muted">正在加载完整文档…</div>';
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/document`);
    const text = data && data.doc_content ? data.doc_content : (data || '');
    box.classList.remove('muted');
    box.innerHTML = renderMarkdownDoc(String(text || '-'));
    details.dataset.loaded = '1';
  } catch (e) {
    box.innerHTML = `<div class="muted">完整文档加载失败: ${esc(e.message)}</div>`;
  }
}

async function refreshIssueMetadata(issueId) {
  try {
    notifyUser('正在刷新', `#${issueId} 附件和历史回复元数据`);
    await api(`/api/redmine-agent/issues/${issueId}/metadata`, {method:'POST'});
    await loadIssues(currentPage);
    notifyUser('已刷新', `#${issueId} 元数据已更新`);
  } catch (e) {
    notifyUser('刷新失败', e.message, 'error');
  }
}

function renderPagination(total, limit, offset) {
  const box = document.getElementById('issuesPagination');
  const pages = Math.ceil(total / limit);
  const current = Math.floor(offset / limit) + 1;
  if (pages <= 1) { box.innerHTML = `<div class="muted">共 ${total} 条</div>`; return; }

  // 页码窗口包含首页、末页和当前页前后两页。
  function pageWindow() {
    const span = 2;            // pages either side of current
    const win = new Set([1, pages, current]);
    for (let p = current - span; p <= current + span; p++) {
      if (p > 1 && p < pages) win.add(p);
    }
    const sorted = Array.from(win).filter(p => p >= 1 && p <= pages).sort((a, b) => a - b);
    const out = [];
    for (let i = 0; i < sorted.length; i++) {
      if (i > 0 && sorted[i] - sorted[i - 1] > 1) out.push('…');
      out.push(sorted[i]);
    }
    return out;
  }

  const numBtn = (p, label) => {
    const active = p === current;
    return `<button class="page-num${active ? ' active' : ''}"${active ? ' disabled' : ''} onclick="loadIssues(${p})">${label}</button>`;
  };

  let html = `<button onclick="loadIssues(1)"${current === 1 ? ' disabled' : ''}>首页</button>`;
  html += `<button onclick="loadIssues(${current-1})"${current === 1 ? ' disabled' : ''}>上一页</button>`;
  for (const p of pageWindow()) {
    if (p === '…') html += `<span class="muted" style="line-height:32px">…</span>`;
    else html += numBtn(p, p);
  }
  html += `<button onclick="loadIssues(${current+1})"${current === pages ? ' disabled' : ''}>下一页</button>`;
  html += `<button onclick="loadIssues(${pages})"${current === pages ? ' disabled' : ''}>末页</button>`;
  html += `<span class="muted" style="line-height:32px">第 ${current}/${pages} 页 (共${total}条)</span>`;
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
  const hasReplyBtn = ['sec-waiting-reply', 'sec-no-reply-3d', 'sec-missing-report'].includes(sectionId);
  const rows = (items || []).map(item => {
    const issueId = item.issue_id || '';
    const reply = item.last_external_reply_by ? `最后回复: ${item.last_external_reply_by}` :
      (item.last_owner_reply_by ? `最后回复: ${item.last_owner_reply_by}` : `附件: ${item.attachment_count || 0}`);
    const note = item.last_external_reply || item.last_owner_reply || '';
    const time = item.last_external_reply_at || item.last_owner_reply_at || item.updated_on || item.created_on || '-';
    const replyBtn = hasReplyBtn && issueId
      ? `<button class="ka-btn" onclick="event.stopPropagation();agentReplyDraft(${issueId}, this)" title="AI 生成回复草稿+补丁方向(联网拉取工单详情与历史回复)">✉️ 回复草稿</button>`
      : '';
    return `<div class="issue-mini">
      <div class="issue-mini-id">${renderRedmineIssueLink(issueId, {stopPropagation: false})}<div class="muted">${esc(item.status_name || '-')}</div></div>
      <div class="issue-mini-title">
        <strong title="${esc(item.subject || '')}">${esc(item.subject || '-')}</strong>
        <span>${esc(reply)}${note ? ' | ' + esc(trunc(note, 120)) : ''}</span>
      </div>
      <div class="issue-mini-right">
        <div class="issue-mini-right-meta">${esc(item.priority_name || '-')}<br>${esc(String(time).slice(0, 16))}</div>
        ${replyBtn}
      </div>
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
    <div>${renderRedmineIssueLink(issueId, {stopPropagation: false})}<div class="muted">${esc(item.status_name || '-')}</div></div>
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
    <div>${renderRedmineIssueLink(issueId, {stopPropagation: false})}<div class="muted">${esc(item.status_name || '-')}</div></div>
    <div class="issue-mini-title">
      <strong title="${esc(item.subject || '')}">${esc(item.subject || '-')}</strong>
      <span>指派给: ${esc(item.assigned_to_name || '-')}</span>
    </div>
    <div class="issue-mini-meta">${esc(item.priority_name || '-')}<br>${esc(String(updated).slice(0, 16))}</div>
  </div>`;
}

function renderRedmineNotConfigured() {
  return `<div class="muted" style="padding:20px;text-align:center">
    <strong>Redmine尚未配置</strong><br>
    请先在 Redmine 看板设置中保存 Redmine 地址和账号密码/API 密码。
    <br><button class="secondary" style="margin-top:12px" onclick="showSettingsModal()">打开设置</button>
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
  document.getElementById('departmentContent').dataset.loaded = 'true';
}

async function loadDepartmentOverdue(force) {
  const box = document.getElementById('departmentContent');
  if (!box) return;
  const requestGeneration = ++redmineDashboardRequestGeneration.department;
  const hadRenderedDashboard = box.dataset.loaded === 'true';
  box.setAttribute('aria-busy', 'true');
  if (!hadRenderedDashboard) box.innerHTML = '<div class="muted" style="padding:20px;text-align:center">⏳ 正在统计部门Redmine数据...</div>';
  try {
    await loadStatsConfig();
    var sd = statsConfig.stale_days || 20;
    var defaults = (statsConfig.dashboard || {}).defaults || {};
    var url = '/api/redmine-agent/statistics/department-overdue?stale_days=' + sd
      + '&list_limit=' + (defaults.list_limit || 50)
      + '&issue_limit=' + (defaults.issue_limit || 500)
      + '&profile_id=' + encodeURIComponent(departmentProfileId || '');
    if (force) url += '&refresh=true';
    const data = await api(url);
    if (requestGeneration !== redmineDashboardRequestGeneration.department) return;
    if (data && data.configured === false) {
      box.innerHTML = renderRedmineNotConfigured();
      box.dataset.loaded = 'true';
      return;
    }
    renderDepartmentOverdue(data);
  } catch (e) {
    if (requestGeneration !== redmineDashboardRequestGeneration.department) return;
    if (hadRenderedDashboard) notifyUser('部门看板刷新失败', e.message, 'error');
    else box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  } finally {
    if (requestGeneration === redmineDashboardRequestGeneration.department) box.setAttribute('aria-busy', 'false');
  }
}

async function loadStatistics(force) {
  const box = document.getElementById('statsContent');
  if (!box) return;
  const requestGeneration = ++redmineDashboardRequestGeneration.stats;
  const hadRenderedDashboard = box.dataset.loaded === 'true';
  box.setAttribute('aria-busy', 'true');
  if (!hadRenderedDashboard) box.innerHTML = '<div class="muted" style="padding:20px;text-align:center">⏳ 正在加载个人看板数据...</div>';
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
    if (force) workloadUrl += '&refresh=true';
    const [basic, workload] = await Promise.all([
      api('/api/redmine-agent/statistics'),
      api(workloadUrl)
    ]);
    if (requestGeneration !== redmineDashboardRequestGeneration.stats) return;
    if (workload && workload.configured === false) {
      box.innerHTML = renderRedmineNotConfigured();
      box.dataset.loaded = 'true';
      return;
    }
    if (force && workload.refresh_warning) {
      notifyUser('Redmine刷新未完全成功', workload.refresh_warning, 'warning');
    }
    const lists = workload.lists || {};
    const meta = workload.meta || {};
    updateRedmineTrendNames(selectedName, meta);

    const userSelectHtml = '<div class="select-with-add">'
      + '<select id="statsUserSelect" onchange="onStatsUserChange()" style="width:160px">'
      + '<option value="' + esc(selectedName || '加载中...') + '">' + esc(selectedName || '加载中...') + '</option>'
      + '</select>'
      + '<button class="select-add-btn" onclick="showAddUserModal()" title="添加用户">＋</button>'
      + '</div>';

    box.innerHTML = `
      <section class="stats-section">
        ${renderSummaryHeader('Redmine概览', '<div class="filter-bar">' + userSelectHtml + '</div>', '统计身份: ' + ((meta.owner_names || []).map(esc).join(' / ') || '未识别') + ' | 统计口径: ' + (meta.count_source === 'redmine_live' ? 'Redmine实时全历史' : '本地同步快照') + ' | 更新时间: ' + esc((meta.generated_at || '-').replace('T', ' ').replace(/:\d{2}$/, '')))}
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
    box.dataset.loaded = 'true';
    statsUserInitialized = false;
    await initStatsUserSelect();
    if (selectedName) {
      var sel = document.getElementById('statsUserSelect');
      if (sel) sel.value = selectedName;
    }
  } catch (e) {
    if (requestGeneration !== redmineDashboardRequestGeneration.stats) return;
    if (hadRenderedDashboard) notifyUser('个人看板刷新失败', e.message, 'error');
    else box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  } finally {
    if (requestGeneration === redmineDashboardRequestGeneration.stats) box.setAttribute('aria-busy', 'false');
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
  document.getElementById('projectContent').dataset.loaded = 'true';
}

async function loadProjectDashboard(force) {
  const box = document.getElementById('projectContent');
  if (!box) return;
  const requestGeneration = ++redmineDashboardRequestGeneration.project;
  const hadRenderedDashboard = box.dataset.loaded === 'true';
  box.setAttribute('aria-busy', 'true');
  if (!hadRenderedDashboard) box.innerHTML = '<div class="muted" style="padding:20px;text-align:center">⏳ 正在统计项目 Redmine 当前情况...</div>';
  try {
    await loadStatsConfig();
    if (!projectProfiles().length) {
      box.innerHTML = '<div class="muted" style="padding:20px">暂无项目看板配置。<button style="margin-left:10px" onclick="showAddProjectModal()">＋ 添加项目</button></div>';
      box.dataset.loaded = 'true';
      return;
    }
    var selected = projectProfileId || (projectProfiles()[0] || {}).id || '';
    var url = '/api/redmine-agent/statistics/project?profile_id=' + encodeURIComponent(selected);
    if (force) url += '&refresh=true';
    const data = await api(url);
    if (requestGeneration !== redmineDashboardRequestGeneration.project) return;
    renderProjectDashboard(data);
  } catch (e) {
    if (requestGeneration !== redmineDashboardRequestGeneration.project) return;
    if (hadRenderedDashboard) notifyUser('项目看板刷新失败', e.message, 'error');
    else box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  } finally {
    if (requestGeneration === redmineDashboardRequestGeneration.project) box.setAttribute('aria-busy', 'false');
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
  const assignee = getSelectedStatsAssignee();
  const target = assignee ? `「${assignee}」名下的` : '指派给你的';
  if (!await confirmUserAction(
    '全量同步 Redmine',
    `确认全量同步 ${target} Redmine 工单？\n这可能需要几分钟。`
  )) return;
  const params = new URLSearchParams({max_analyze: '30'});
  if (assignee) params.set('assignee_name', assignee);
  const btn = document.getElementById('syncBtn');
  const oldText = btn ? btn.textContent : '';
  try {
    const started = await api(`/api/redmine-agent/sync?${params}`, {method:'POST'});
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 同步中...'; }
    await waitForRun(started.run_id, '同步');
  } catch (e) { notifyUser('同步失败', e.message, 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = oldText || '🔄 全量同步'; } }
}

function showResetModal() {
  const cb = document.getElementById('resetConfirm');
  if (cb) cb.checked = false;
  const m = document.getElementById('resetModal');
  if (m) m.style.display = 'flex';
}

function hideResetModal() {
  const m = document.getElementById('resetModal');
  if (m) m.style.display = 'none';
}

async function confirmReset() {
  const cb = document.getElementById('resetConfirm');
  if (!cb || !cb.checked) {
    notifyUser('请确认', '需要勾选「我确认删除所有数据」才能继续', 'warning');
    return;
  }
  hideResetModal();
  try {
    await api('/api/redmine-agent/reset', {method:'POST'});
    notifyUser('重置完成', 'Redmine 数据已清空', 'success');
    refreshCurrentTab();
  } catch (e) { notifyUser('重置失败', e.message, 'error'); }
}

async function waitForRun(runId, label, options) {
  const opts = options || {};
  const reload = opts.reload !== false;
  for (let i = 0; i < 240; i++) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const status = await api('/api/redmine-agent/status');
      if (!status.running) {
        const last = status.last_result || {};
        if (last.status === 'failed' || last.error) {
          notifyUser('RedmineAgent ' + label + '失败', last.error || ('任务 ' + runId + ' 执行失败'), 'error');
          return;
        }
        if (reload) refreshCurrentTab();
        notifyUser('RedmineAgent ' + label + '完成', '任务 ' + runId + ' 已完成', 'success');
        return;
      }
    } catch (_) {}
  }
  if (reload) refreshCurrentTab();
  notifyUser('RedmineAgent ' + label + '超时', '任务 ' + runId + ' 等待超时，请检查状态', 'warning');
}

// 知识库：成熟案例、批量导入、案例分析和回复草稿。
let currentCaseOffset = 0;
const casePageSize = 30;
let pendingReferenceIssueId = 0;
let pendingInternalSource = null; // {type:'issue'|'case', id}
let caseView = 'facts'; // 'facts' (imported issue facts) | 'cases' (mature cases)

function switchCaseView(view) {
  caseView = view;
  document.getElementById('viewBtnFacts').classList.toggle('active', view === 'facts');
  document.getElementById('viewBtnCases').classList.toggle('active', view === 'cases');
  currentCaseOffset = 0;
  loadCases();
}

async function loadCases() {
  const search = (document.getElementById('caseSearchInput') || {}).value || '';
  try {
    if (caseView === 'facts') {
      const data = await api(`/api/redmine-agent/cases?limit=${casePageSize}&offset=${currentCaseOffset}&search=${encodeURIComponent(search)}`);
      renderFactsList(data.items || [], data.total || 0);
    } else {
      const data = await api(`/api/redmine-agent/mature-cases?limit=${casePageSize}&offset=${currentCaseOffset}&search=${encodeURIComponent(search)}`);
      renderCasesList(data.items || [], data.total || 0);
    }
  } catch (e) {
    const box = document.getElementById('casesList');
    if (box) box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

function renderFactsList(items, total) {
  const box = document.getElementById('casesList');
  if (!items.length) {
    box.innerHTML = '<div class="muted" style="padding:14px">暂无已导入的工单。点击右上「📥 批量导入工单」粘贴工单号,或「导入最近20个我的指派」。</div>';
    return;
  }
  box.innerHTML = items.map(f => {
    const sig = f.error_signature ? `<span class="case-sig">${esc(f.error_signature)}</span>` : '';
    const scope = [f.chip_platform, f.android_version, f.certification_type, f.module].filter(Boolean).map(esc).join(' / ');
    const conf = f.confidence ? `<span class="muted" style="float:right">置信度 ${f.confidence}</span>` : '';
    return `<div class="case-card" onclick="showCaseFact(${f.issue_id})">
      <div class="case-head"><span class="case-status draft">${renderRedmineIssueLink(f.issue_id)}</span>${sig}${conf}</div>
      <div class="case-title">${esc(f.subject || '-')}</div>
      <div class="case-scope muted">${scope || '-'}</div>
      <div class="case-sources muted">${esc(f.problem_summary || '').slice(0,80)}</div>
    </div>`;
  }).join('') + `<div class="muted" style="padding:8px">共 ${total} 条工单事实</div>`;
}

function renderCasesList(items, total) {
  const box = document.getElementById('casesList');
  if (!items.length) {
    box.innerHTML = '<div class="muted" style="padding:14px">暂无成熟案例。请先「批量导入工单」结构化历史单,再选中若干案例用「📚分析案例」→ 构建成熟案例。</div>';
    return;
  }
  box.innerHTML = items.map(c => {
    const status = c.status || 'draft';
    const badge = status === 'approved' ? '✅已审核' : status === 'draft' ? '📝草稿' : esc(status);
    const sig = c.canonical_error_signature ? `<span class="case-sig">${esc(c.canonical_error_signature)}</span>` : '';
    const scope = [c.chip_platform, c.android_version, c.certification_type, c.module].filter(Boolean).map(esc).join(' / ');
    return `<div class="case-card" onclick="showCaseDetail(${c.case_id})">
      <div class="case-head"><span class="case-status ${status}">${badge}</span>${sig}</div>
      <div class="case-title">${esc(c.title || '-')}</div>
      <div class="case-scope muted">${scope || '-'}</div>
      <div class="case-sources muted">来源: ${renderRedmineIssueLinks(c.source_issue_ids_json || [])}</div>
    </div>`;
  }).join('') + `<div class="muted" style="padding:8px">共 ${total} 个案例</div>`;
}

async function showCaseDetail(caseId) {
  document.getElementById('caseDetailTitle').textContent = '案例 #' + caseId;
  const box = document.getElementById('caseDetail');
  box.innerHTML = '<div class="muted">加载中…</div>';
  try {
    const c = await api(`/api/redmine-agent/mature-cases/${caseId}`);
    const sol = c.solution_json || {};
    const rules = c.rules_json || [];
    const sources = c.source_issue_ids_json || [];
    box.innerHTML = `
      <div class="field"><div class="field-label">标题</div><div class="field-content">${esc(c.title||'-')}</div></div>
      <div class="field"><div class="field-label">适用范围</div><div class="field-content">${esc([c.chip_platform,c.android_version,c.certification_type,c.module].filter(Boolean).join(' / ')||'-')}</div></div>
      <div class="field"><div class="field-label">问题摘要</div><div class="field-content">${esc(c.problem_summary||'-')}</div></div>
      <div class="field"><div class="field-label">根因</div><div class="field-content">${esc(c.root_cause||'-')}</div></div>
      <div class="field"><div class="field-label">解决方案</div><div class="field-content">${renderFormattedContent(sol.overview||'-','field-content')}</div></div>
      ${rules.length?`<div class="field"><div class="field-label">经验规则</div><div class="field-content">${rules.map(r=>esc((r.title||'')+(r.content?': '+r.content:''))).join('<br>')}</div></div>`:''}
      <div class="field"><div class="field-label">来源工单</div><div class="field-content">${renderRedmineIssueLinks(sources)}</div></div>
      <div class="case-actions">
        ${c.status!=='approved'?`<button onclick="approveCase(${caseId})">✅ 审核通过</button>`:''}
        <button class="secondary" onclick="draftReply(${sources[0]||0}, ${caseId})">✉️ 生成回复</button>
        <button class="secondary" onclick="startCreateInternalCase(${caseId})">📝 创建内部单</button>
      </div>`;
  } catch (e) { box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`; }
}

async function approveCase(caseId) {
  try { await api(`/api/redmine-agent/mature-cases/${caseId}/approve`, {method:'POST'}); notifyUser('已审核', '案例 #'+caseId+' 已标记为 approved'); loadCases(); showCaseDetail(caseId); }
  catch(e){ notifyUser('审核失败', e.message, 'error'); }
}

// ---- Batch import ----
function showBatchImportModal() {
  document.getElementById('batchImportIds').value = '';
  document.getElementById('batchImportResult').innerHTML = '';
  showModal('batchImportModal');
}

function _renderImportResult(data, resultBox) {
  const items = data.items || [];
  const done = items.filter(i => i.status === 'done' || i.status === 'exists').length;
  resultBox.innerHTML = `✅ 完成 ${done}/${items.length}${data.failed ? ` (失败 ${data.failed})` : ''}<br><span class="muted">${items.slice(0, 12).map(i => `${renderRedmineIssueLink(i.issue_id)}:${esc(i.status || '')}`).join('  ')}</span>`;
  notifyUser('批量导入完成', `成功 ${done}/${items.length}，已自动跳转「工单事实」查看`);
  hideModal('batchImportModal');
  switchCaseView('facts');
}

async function submitBatchImport() {
  const raw = document.getElementById('batchImportIds').value.trim();
  const reanalyze = document.getElementById('batchReanalyze').checked;
  const resultBox = document.getElementById('batchImportResult');
  if (!raw) { resultBox.innerHTML = '❌ 请先粘贴工单号,或点「导入最近20个」'; return; }
  resultBox.innerHTML = '⏳ 导入中...';
  try {
    const data = await api('/api/redmine-agent/issues/batch-import', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({issue_ids: raw, reanalyze})});
    _renderImportResult(data, resultBox);
  } catch(e){ resultBox.innerHTML = `❌ ${esc(e.message)}`; }
}

async function submitImportRecent(n) {
  const reanalyze = document.getElementById('batchReanalyze').checked;
  const resultBox = document.getElementById('batchImportResult');
  resultBox.innerHTML = `⏳ 导入最近 ${n} 个我的指派工单...`;
  try {
    const data = await api(`/api/redmine-agent/issues/import-recent?limit=${n}`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({assigned_like: '', reanalyze})});
    _renderImportResult(data, resultBox);
  } catch(e){ resultBox.innerHTML = `❌ ${esc(e.message)}`; }
}

// ---- Per-issue knowledge actions ----
async function analyzeIssueCase(issueId) {
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/analyze-case`, {method:'POST'});
    if (data.status==='done'||data.status==='exists') { notifyUser('已入库', `#${issueId} → 模块 ${data.module||'-'}`); showCaseFact(issueId); }
    else notifyUser('分析失败', data.error||data.status, 'error');
  } catch(e){ notifyUser('分析失败', e.message, 'error'); }
}

async function findSimilarCase(issueId) {
  openKnowledgeModal(`#${issueId} 相似工单`, '<div class="muted">检索中…</div>');
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/similar?limit=10`);
    const items = data.similar || [];
    if (!items.length) { setKnowledgeBody('<div class="muted">知识库暂无相似工单。请先批量导入历史单。</div>'); return; }
    setKnowledgeBody(items.map(s=>`<div class="ref-item">
      ${renderRedmineIssueLink(s.issue_id, {stopPropagation: false})}
      <span class="ref-badge ${s.similarity_level}">${s.similarity_level==='high'?'高':s.similarity_level==='medium'?'中':'低'} ${s.score}</span>
      <span class="ref-title">${esc(s.subject||'')}</span>
      <span class="muted">[${esc(s.module||'-')}${s.error_signature?'/'+esc(s.error_signature):''}]</span>
    </div>`).join(''));
  } catch(e){ setKnowledgeBody(`<div class="muted">失败: ${esc(e.message)}</div>`); }
}

async function toggleIssueWorkbench(issueId) {
  const panel = document.getElementById('issue-workbench-' + issueId);
  if (!panel) return;
  if (panel.style.display !== 'none') {
    panel.style.display = 'none';
    return;
  }
  panel.style.display = 'block';
  panel.innerHTML = '<div class="muted" style="padding:10px">正在汇总结构化事实、历史回复、附件证据和相似工单…</div>';
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/workbench?similar_limit=8`);
    panel.innerHTML = renderIssueWorkbench(data);
  } catch (e) {
    panel.innerHTML = `<div class="muted" style="padding:10px">知识面板加载失败: ${esc(e.message)}</div>`;
  }
}

function renderIssueWorkbench(data) {
  const fact = data.fact || {};
  const sections = data.gms_like_sections || {};
  const scope = sections.scope || {};
  const evidence = data.evidence || {};
  const mature = data.mature_case || null;
  const similar = data.similar || [];
  const attachments = evidence.attachment_summary || [];
  const replies = evidence.reply_summary || [];
  const failures = evidence.failure_summary || [];
  const symptoms = sections.symptoms || [];
  const rules = sections.rules || [];
  const sourceIds = sections.source_issue_ids || [];
  return `
    <div class="workbench-grid">
      <section class="workbench-block">
        <div class="workbench-title">GMS风格结构化结论</div>
        <div class="kv-line"><b>范围</b><span>${esc([scope.chip_platform, scope.android_version, scope.test_version, scope.module].filter(Boolean).join(' / ') || '-')}</span></div>
        <div class="kv-line"><b>错误签名</b><span>${esc(fact.error_signature || (mature ? mature.canonical_error_signature : '') || '-')}</span></div>
        <div class="kv-line"><b>问题现象</b><span>${renderListText(symptoms, '暂无结构化现象')}</span></div>
        <div class="kv-line"><b>根因</b><span>${renderFormattedContent(sections.root_cause || '-', 'field-content')}</span></div>
        <div class="kv-line"><b>解决方案</b><span>${renderFormattedContent(sections.solution || '-', 'field-content')}</span></div>
        <div class="kv-line"><b>验证方式</b><span>${esc(sections.verification || '-')}</span></div>
        ${rules.length ? `<div class="kv-line"><b>经验规则</b><span>${rules.map(r => esc((r.title || '') + (r.content ? ': ' + r.content : ''))).join('<br>')}</span></div>` : ''}
      </section>
      <section class="workbench-block">
        <div class="workbench-title">历史依据</div>
        ${mature ? `<div class="mature-hit">命中成熟案例 #${mature.case_id}: ${esc(mature.title || '')}</div>` : '<div class="muted">暂无成熟案例命中，可先从相似工单构建。</div>'}
        <div class="similar-list">${similar.length ? similar.map(s => `
          <div class="ref-item">
            ${renderRedmineIssueLink(s.issue_id, {stopPropagation: false})}
            <span class="ref-badge ${s.similarity_level || 'low'}">${esc(s.similarity_level || 'low')} ${s.score || 0}</span>
            <span class="ref-title">${esc(s.subject || '')}</span>
          </div>`).join('') : '<div class="muted">暂无相似历史工单。</div>'}</div>
        <div class="source-links">${sourceIds.length ? '来源: ' + renderRedmineIssueLinks(sourceIds, {stopPropagation: false}) : ''}</div>
      </section>
    </div>
    <div class="workbench-grid">
      <section class="workbench-block">
        <div class="workbench-title">附件 / 截图 / 日志证据</div>
        ${failures.length ? `<div class="mini-evidence-title">失败项</div>${failures.map(f => `<div class="evidence-line"><b>${esc(f.module || '-')}</b> ${esc(f.name || '')}<br><span>${esc(f.reason || '')}</span></div>`).join('')}` : ''}
        ${attachments.length ? attachments.map(a => renderAttachmentEvidence(a)).join('') : `<div class="muted">暂无本地解析结果；附件文件不存入内部知识库，请通过上方“Redmine附件 / 补丁”或 <a href="${redmineIssueAttachmentsUrl(data.issue_id)}" target="_blank">Redmine 附件区</a> 查看源文件。</div>`}
      </section>
      <section class="workbench-block">
        <div class="workbench-title">历史回复摘要</div>
        ${replies.length ? replies.map(r => `
          <div class="reply-line">
            <div><b>${esc(r.user || '-')}</b> <span class="muted">${esc(String(r.created_on || '').slice(0, 19))}</span></div>
            ${r.notes ? `<div>${esc(r.notes)}</div>` : ''}
            ${r.details && r.details.length ? `<div class="muted">${r.details.map(d => esc([d.name, d.old_value, d.new_value].filter(Boolean).join(' -> '))).join('<br>')}</div>` : ''}
          </div>`).join('') : '<div class="muted">暂无可汇总的历史回复。</div>'}
      </section>
    </div>`;
}

function renderListText(items, emptyText) {
  if (!items || !items.length) return esc(emptyText || '-');
  return '<ul class="compact-list">' + items.slice(0, 8).map(item => `<li>${esc(item)}</li>`).join('') + '</ul>';
}

function renderAttachmentEvidence(a) {
  const detected = a.detected_errors || [];
  const failures = a.failures || [];
  const type = a.type || a.content_type || '';
  return `<div class="attachment-evidence">
    <div><b>${esc(a.filename || '-')}</b> <span class="muted">${esc(type || a.status || '')}</span></div>
    ${detected.length ? `<div class="detected-errors">${detected.map(esc).join(' / ')} ${a.certification_type ? '(' + esc(a.certification_type) + ')' : ''}</div>` : ''}
    ${failures.length ? failures.map(f => `<div class="evidence-line"><b>${esc(f.module || '-')}</b> ${esc(f.name || '')}<br><span>${esc(f.reason || '')}</span></div>`).join('') : ''}
    ${a.text_excerpt ? `<details><summary>OCR/文本摘录</summary><pre>${esc(a.text_excerpt)}</pre></details>` : ''}
  </div>`;
}

async function draftReply(issueId, matureCaseId) {
  openKnowledgeModal(`✉️ 回复草稿 #${issueId}`, '<div class="muted">生成中…</div>');
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/draft-reply`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(matureCaseId ? {mature_case_id: matureCaseId} : {})});
    const body = `<div class="muted" style="margin-bottom:6px">Redmine ${renderRedmineIssueLink(issueId, {stopPropagation: false})} · 来源: ${data.source==='mature_case'?'成熟案例 #'+(data.mature_case_id||''):'相似工单'} · 模块 ${esc(data.module||'-')} ${data.error_signature?'/ '+esc(data.error_signature):''}</div>
      <textarea id="replyDraftArea" rows="14" style="width:100%;font-family:monospace">${esc(data.reply_draft||'')}</textarea>
      <div style="margin-top:8px"><button onclick="copyReplyDraft(this)">📋 复制</button></div>`;
    setKnowledgeBody(body);
  } catch(e){ setKnowledgeBody(`<div class="muted">失败: ${esc(e.message)}</div>`); }
}

async function draftAgentReply(issueId, matureCaseId) {
  openKnowledgeModal(`🤖 AI+知识库回复 #${issueId}`, '<div class="muted">正在拉取/分析工单并生成回复，可能需要几十秒…</div>');
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/agent-reply`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(matureCaseId ? {mature_case_id: matureCaseId} : {})
    });
    const body = `<div class="muted" style="margin-bottom:6px">Redmine ${renderRedmineIssueLink(issueId, {stopPropagation: false})} · 来源: ${esc(data.source || '-')} · 模块 ${esc(data.module||'-')} ${data.error_signature?'/ '+esc(data.error_signature):''}</div>
      ${data.patch_direction ? `<div class="field"><div class="field-label">补丁方向</div>${renderFormattedContent(data.patch_direction, 'field-content')}</div>` : ''}
      <textarea id="replyDraftArea" rows="16" style="width:100%;font-family:monospace">${esc(data.reply_draft||'')}</textarea>
      <div style="margin-top:8px"><button onclick="copyReplyDraft(this)">📋 复制</button></div>`;
    setKnowledgeBody(body);
  } catch(e){ setKnowledgeBody(`<div class="muted">失败: ${esc(e.message)}</div>`); }
}

function copyReplyDraft(btn) {
  const area = document.getElementById('replyDraftArea');
  if (!area) return;
  navigator.clipboard.writeText(area.value).then(function(){ notifyUser('已复制', '回复草稿已复制到剪贴板'); });
  if (btn) { var old = btn.textContent; btn.textContent = '✓'; setTimeout(function(){ btn.textContent = old; }, 1500); }
}

async function sendReplyToRedmine(issueId, btn) {
  const area = document.getElementById('replyDraftArea');
  if (!area) { notifyUser('无回复内容', '请先生成回复草稿', 'error'); return; }
  const text = (area.value || '').trim();
  if (!text) { notifyUser('回复为空', '请先生成或编辑回复草稿', 'error'); return; }
  if (!await confirmUserAction(
    '发送 Redmine 回复',
    '确认将此回复发送到 Redmine #' + issueId + '？'
  )) return;
  if (btn) { btn.disabled = true; var old = btn.textContent; btn.textContent = '⏳ 发送中…'; }
  try {
    const data = await api('/api/redmine/reply', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({issue_id: String(issueId), reply_text: text}),
    });
    notifyUser('已发送', data && data.message ? data.message : '回复已发送到 Redmine #' + issueId);
    if (btn) { btn.textContent = '✓ 已发送'; }
  } catch (e) {
    notifyUser('发送失败', e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; setTimeout(function(){ btn.textContent = old; }, 2000); }
  }
}

async function agentReplyDraft(issueId, btn) {
  if (!issueId) return;
  if (btn) { btn.disabled = true; var oldBtn = btn.textContent; btn.textContent = '⏳ 生成中…'; }
  notifyUser('正在生成回复', '联网拉取工单详情、附件与历史回复，约 10-30s');
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/agent-reply`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({force: false}),
    });
    const srcMap = {mature_case: '成熟案例 #' + (data.mature_case_id || ''), ai_analysis: 'AI 分析（联网）', similar_issues: '相似历史工单'};
    const srcLabel = srcMap[data.source] || data.source;
    const sigLine = data.error_signature ? ' / ' + data.error_signature : '';
    const similarText = (data.similar_issues || []).length
      ? ' · 相似历史 ' + (data.similar_issues || []).map(function(s) { return renderRedmineIssueLink(s.issue_id, {stopPropagation: false}); }).join(' ')
      : '';
    const summaryHtml = '来源: ' + esc(srcLabel || '-') + ' · 模块 ' + esc(data.module || '-') + esc(sigLine) + similarText;
    openRedmineReplyModal(issueId, data.reply_draft || '', {summaryHtml: summaryHtml});
  } catch (e) {
    notifyUser('生成失败', e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = oldBtn; }
  }
}

function copyPatchDirection(btn) {
  const code = document.querySelector('#patchDirectionBlock code');
  if (!code) return;
  navigator.clipboard.writeText(code.textContent).then(function(){ notifyUser('已复制', '补丁方向已复制到剪贴板'); });
  if (btn) { var old = btn.textContent; btn.textContent = '✓'; setTimeout(function(){ btn.textContent = old; }, 1500); }
}

async function showCaseFact(issueId) {
  openKnowledgeModal(`📚 案例结构化 #${issueId}`, '<div class="muted">加载中…</div>');
  try {
    const f = await api(`/api/redmine-agent/cases/${issueId}`);
    setKnowledgeBody(`
      <div class="field"><div class="field-label">Redmine工单</div><div class="field-content">${renderRedmineIssueLink(issueId, {stopPropagation: false})}</div></div>
      <div class="field"><div class="field-label">平台/版本</div><div class="field-content">${esc(f.chip_platform||'-')} / ${esc(f.android_version||'-')} / ${esc(f.certification_type||'-')}</div></div>
      <div class="field"><div class="field-label">模块/错误签名</div><div class="field-content">${esc(f.module||'-')} / ${esc(f.error_signature||'-')} <span class="muted">(置信度 ${f.confidence||0})</span></div></div>
      <div class="field"><div class="field-label">问题摘要</div><div class="field-content">${esc(f.problem_summary||'-')}</div></div>
      <div class="field"><div class="field-label">根因</div><div class="field-content">${esc(f.root_cause||'-')}</div></div>
      <div class="field"><div class="field-label">解决方案</div><div class="field-content">${renderFormattedContent(f.solution||'-','field-content')}</div></div>
      <div class="case-actions">
        <button onclick="buildMatureFromIssue(${issueId})">🏗️ 构建成熟案例</button>
        <button class="secondary" onclick="draftReply(${issueId})">✉️ 生成回复</button>
      </div>`);
  } catch(e){ setKnowledgeBody(`<div class="muted">失败: ${esc(e.message)}</div>`); }
}

async function buildMatureFromIssue(issueId) {
  try {
    const data = await api('/api/redmine-agent/mature-cases/build', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({issue_ids: String(issueId)})});
    notifyUser('已构建', '成熟案例 #'+data.case_id); hideModal('knowledgeModal'); loadCases();
  } catch(e){ notifyUser('构建失败', e.message, 'error'); }
}

// ---- Reference output + evaluation ----
function openReferenceModal(issueId) { pendingReferenceIssueId = issueId; showModal('referenceModal'); }

async function submitReference() {
  const issueId = pendingReferenceIssueId;
  const payload = {
    source: document.getElementById('referenceSource').value,
    title: document.getElementById('referenceTitle').value,
    markdown: document.getElementById('referenceMarkdown').value,
  };
  try {
    await api(`/api/redmine-agent/issues/${issueId}/reference-output`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    hideModal('referenceModal');
    compareCase(issueId);
  } catch(e){ notifyUser('导入失败', e.message, 'error'); }
}

async function compareCase(issueId) {
  openKnowledgeModal(`⚖️ 质量对比 #${issueId}`, '<div class="muted">评测中…</div>');
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/evaluate-case`, {method:'POST'});
    setKnowledgeBody(`
      <div class="field"><div class="field-label">Redmine工单</div><div class="field-content">${renderRedmineIssueLink(issueId, {stopPropagation: false})}</div></div>
      <div class="field"><div class="field-label">评分</div><div class="field-content" style="font-size:20px;font-weight:600">${data.score||0}/100</div></div>
      ${(data.missing_fields||[]).length?`<div class="field"><div class="field-label">缺失字段</div><div class="field-content">${data.missing_fields.map(esc).join(', ')}</div></div>`:''}
      ${(data.mismatch_fields||[]).length?`<div class="field"><div class="field-label">不一致字段</div><div class="field-content">${data.mismatch_fields.map(m=>`${esc(m.field)}: 内部=${esc(m.internal)} / 参考=${esc(m.reference)}`).join('<br>')}</div></div>`:''}
      ${(data.suggestions||[]).length?`<div class="field"><div class="field-label">优化建议</div><div class="field-content">${data.suggestions.map(esc).join('<br>')}</div></div>`:''}
      <div class="muted" style="margin-top:8px">注:参考输出仅用于评测,不参与自动回复。</div>`);
  } catch(e){ setKnowledgeBody(`<div class="muted">失败: ${esc(e.message)}</div>`); }
}

// ---- Internal issue creation ----
function startCreateInternal(issueId) { pendingInternalSource = {type:'issue', id: issueId}; showModal('internalCreateModal'); }
function startCreateInternalCase(caseId) { pendingInternalSource = {type:'case', id: caseId}; showModal('internalCreateModal'); }

async function confirmInternalCreate() {
  if (!pendingInternalSource) return;
  const payload = {
    project_id: document.getElementById('internalProjectId').value,
    tracker_id: parseInt(document.getElementById('internalTrackerId').value)||1,
    priority_id: parseInt(document.getElementById('internalPriorityId').value)||2,
    assigned_to_id: document.getElementById('internalAssignedId').value || null,
    confirmed: true,
  };
  const src = pendingInternalSource; pendingInternalSource = null;
  try {
    const url = src.type==='case' ? `/api/redmine-agent/mature-cases/${src.id}/create-internal` : `/api/redmine-agent/issues/${src.id}/create-internal`;
    const data = await api(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    hideModal('internalCreateModal');
    if (data.success) notifyUser('已创建', '内部工单 #'+data.internal_issue_id);
    else notifyUser('创建未完成', data.error||'请检查 Redmine 凭据/配置', 'warning');
  } catch(e){ notifyUser('创建失败', e.message, 'error'); }
}

// ---- Knowledge modal helpers ----
function openKnowledgeModal(title, body) {
  document.getElementById('knowledgeTitle').textContent = title;
  document.getElementById('knowledgeFooter').style.display = '';
  setKnowledgeBody(body);
  showModal('knowledgeModal');
}
function setKnowledgeBody(html) { document.getElementById('knowledgeBody').innerHTML = html; }

// ---- Init ----
document.addEventListener('click', function(event) {
  const link = event.target.closest('[data-redmine-issue-id]');
  if (link) selectRedmineWorkspaceIssue(link.dataset.redmineIssueId);
});
window.addEventListener('gms:embedded-workspace', function(event) {
  applyRedmineWorkspaceContext(
    event.detail && event.detail.context || {},
    event.detail && event.detail.type === 'workspace-context-navigate'
  ).catch(function(error) { notifyUser('打开工单失败', error.message, 'error'); });
});
restoreRedmineProfileState();
var initialTab = new URLSearchParams(window.location.search).get('tab') || (window.sessionStorage.getItem('redmineLastTab') || 'stats');
if (!document.getElementById('tab-' + initialTab)) initialTab = 'stats';
var redmineInitialLoad = Promise.resolve(switchTab(initialTab));
try {
  var initialQuery = new URLSearchParams(window.location.search).get('issue') || new URLSearchParams(window.location.search).get('q') || '';
  if (initialQuery) {
    var searchInput = document.getElementById('searchInput');
    if (searchInput) searchInput.value = String(initialQuery).replace(/^#/, '');
    redmineInitialLoad = Promise.resolve(switchTab('issues')).then(function() {
      return smartSearch();
    });
  }
} catch (_) {}
redmineInitialLoad.catch(function() {}).finally(function() {
  window.GmsEmbeddedWorkspace && window.GmsEmbeddedWorkspace.markReady();
});

// Auto-refresh status. Hidden iframe pages do not need to keep polling.
var redmineStatusRefreshInterval = null;
var redmineStatusRefreshPromise = null;
function refreshRedmineAgentStatus() {
  if (redmineStatusRefreshPromise) return redmineStatusRefreshPromise;
  redmineStatusRefreshPromise = (async function() {
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
  })().finally(function() { redmineStatusRefreshPromise = null; });
  return redmineStatusRefreshPromise;
}
function syncRedmineStatusRefresh(event) {
  var visible = event && event.detail && event.detail.visible;
  if (visible === undefined) {
    visible = window.GmsEmbeddedWorkspace && window.GmsEmbeddedWorkspace.isVisible
      ? window.GmsEmbeddedWorkspace.isVisible()
      : true;
  }
  if (redmineStatusRefreshInterval) {
    clearInterval(redmineStatusRefreshInterval);
    redmineStatusRefreshInterval = null;
  }
  if (!visible) return;
  refreshRedmineAgentStatus();
  redmineStatusRefreshInterval = setInterval(refreshRedmineAgentStatus, 10000);
}
window.addEventListener('gms:embedded-visibility', syncRedmineStatusRefresh);
syncRedmineStatusRefresh();
