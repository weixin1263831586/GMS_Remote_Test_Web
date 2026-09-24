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
    // 方案 2：成员写入共享组织架构（管理员权限；原 per-owner POST /users 已收编）。
    await api('/api/redmine-agent/org-chart/members', {
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
  } catch (e) {
    if (e && /403|权限|admin/i.test(e.message)) {
      notifyUser('添加用户失败', '需要管理员权限（组织架构为全组共享，由管理员统一维护）', 'error');
    } else {
      notifyUser('添加用户失败', e.message, 'error');
    }
  }
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
  // 每次打开都独立刷新晨报模型；统计/凭据设置失败不能让旧下拉选项残留。
  void refreshDailyBriefSettings();
  void initSelfBindingSelect();
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
        renderRedmineCredentialStatus(creds);
      } catch (_) { renderRedmineCredentialStatus(null); }
      document.getElementById('settingRedminePass').value = '';
      document.getElementById('settingRedmineApiKey').value = '';
    } catch (_) {}
  })();
}
function hideSettingsModal() { hideModal('settingsModal'); }

// ---- 个人身份绑定（方案 2：全局组织架构共享，个人绑定/别名 per-owner）----
async function initSelfBindingSelect() {
  var select = document.getElementById('settingSelfBinding');
  if (!select) return;
  try {
    var users = await api('/api/redmine-agent/users');
    var binding = await api('/api/redmine-agent/me/binding').catch(function() { return {member: null}; });
    var boundId = binding && binding.member ? String(binding.member.id || '') : '';
    var items = (users.items || []).slice().sort(function(a, b) {
      return (a.name || '').localeCompare(b.name || '', 'zh-Hans-CN-u-co-pinyin');
    });
    select.innerHTML = '<option value="">未绑定（按姓名/邮箱自动识别）</option>'
      + items.map(function(item) {
        var dept = item.department ? ' · ' + item.department : '';
        return '<option value="' + esc(item.id) + '">#' + esc(item.id) + ' ' + esc(item.name) + dept + '</option>';
      }).join('');
    select.value = boundId;
    if (select.value !== boundId) select.value = '';
  } catch (_) { select.innerHTML = '<option value="">（组织架构不可用）</option>'; }
}

async function submitSelfBinding() {
  var select = document.getElementById('settingSelfBinding');
  if (!select) return;
  var value = select.value || null;
  try {
    await api('/api/redmine-agent/me/binding', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({member_id: value}),
    });
    notifyUser(value ? '已绑定身份' : '已解除绑定', value ? '个人看板与每日晨报将按绑定的组织成员识别' : '恢复按姓名/邮箱自动识别', 'success');
  } catch (e) { notifyUser('绑定失败', e.message, 'error'); }
}

async function refreshDailyBriefSettings() {
  try {
    dailyBriefConfigCache = await api('/api/redmine-agent/daily-brief/config') || {};
    dailyBriefSetting('enabled').checked = dailyBriefConfigCache.enabled === true;
    dailyBriefSetting('trigger_time').value = dailyBriefConfigCache.trigger_time || '00:00';
    dailyBriefSetting('delta_enabled').checked = dailyBriefConfigCache.delta_enabled !== false;
    dailyBriefSetting('delta_trigger_time').value = dailyBriefConfigCache.delta_trigger_time || '06:00';
    await Promise.all([
      loadDailyBriefAgentProfiles(dailyBriefConfigCache.agent_profile || ''),
      loadDailyBriefModelOptions(dailyBriefConfigCache.model || ''),
    ]);
    dailyBriefSetting('max_parallel_issues').value = dailyBriefConfigCache.max_parallel_issues || 1;
  } catch (_) {
    // The individual selectors render a visible fallback option on failure.
  }
}

function renderRedmineCredentialStatus(status) {
  var el = document.getElementById('settingRedmineCredentialStatus');
  if (!el) return;
  if (!status) {
    el.textContent = '无法读取当前账号的 Redmine 凭据状态。';
    return;
  }
  if (status.configured) {
    el.textContent = status.api_key_configured
      ? '当前账号：Redmine 地址与 API Key 已配置。'
      : '当前账号：Redmine 地址与账号密码已配置。';
    return;
  }
  if (status.credential_error === 'stored_secret_unreadable') {
    el.textContent = '已保存 Redmine 凭据，但当前部署主密钥无法解密；请恢复原 master.key，或重新保存凭据。';
    return;
  }
  var missing = [];
  if (!status.base_url_configured) missing.push('地址');
  if (!status.password_configured && !status.api_key_configured) missing.push('账号密码或 API Key');
  el.textContent = '当前账号尚未配置：' + (missing.join('、') || 'Redmine 凭据') + '。';
}

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
    var redmineApiKey = document.getElementById('settingRedmineApiKey').value;
    if (redminePass) {
      await api('/api/redmine-agent/config/credentials', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: redmineUser, password: redminePass})
      });
    }
    if (redmineApiKey) {
      await api('/api/redmine-agent/config/credentials', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({api_key: redmineApiKey})
      });
    }
    try { renderRedmineCredentialStatus(await api('/api/redmine-agent/config/credentials')); } catch (_) {}
    var briefConfig = Object.assign({}, dailyBriefConfigCache || {});
    briefConfig.enabled = dailyBriefSetting('enabled').checked;
    briefConfig.trigger_time = dailyBriefSetting('trigger_time').value || '00:00';
    briefConfig.delta_enabled = dailyBriefSetting('delta_enabled').checked;
    briefConfig.delta_trigger_time = dailyBriefSetting('delta_trigger_time').value || '06:00';
    briefConfig.agent_profile = dailyBriefSetting('agent_profile').value.trim();
    briefConfig.model = dailyBriefSetting('model').value.trim();
    briefConfig.max_parallel_issues = parseInt(dailyBriefSetting('max_parallel_issues').value) || 1;
    briefConfig.max_turns = 0;
    briefConfig.issue_timeout_seconds = 0;
    dailyBriefConfigCache = await api('/api/redmine-agent/daily-brief/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(briefConfig)
    });
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
    box.innerHTML = '<div class="muted" style="padding:20px">暂无工单数据。请展开上方「高级同步」，使用「全量同步」拉取所有指派给你的 Redmine 工单。</div>';
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

    <details class="issue-doc-details" data-toggle="loadIssueDocOnToggle" data-r0="el" data-a1="${item.issue_id}">
      <summary>📄 完整文档</summary>
      <div class="formatted-doc muted">展开后加载完整文档…</div>
    </details>

    <div class="knowledge-actions">
      <button class="ka-btn primary" data-click="agentReplyDraft" data-a0="${item.issue_id}" data-r1="el" title="复用报告分析风格生成 Redmine 回复草稿、根因和补丁方向">✉️ Redmine回复</button>
      <button class="ka-btn" data-click="refreshIssueMetadata" data-a0="${item.issue_id}" title="只刷新Redmine历史回复和附件元数据，不下载附件">🔄 刷新附件元数据</button>
      <button class="ka-btn" data-click="toggleIssueWorkbench" data-a0="${item.issue_id}" title="展开相似工单、历史回复和附件解析摘要">🧩 展开依据</button>
      <button class="ka-btn" data-click="saveIssueToWiki" data-a0="${item.issue_id}" title="把该工单存入 Wiki「Redmine问题沉淀」分类，并建立外链">📥 存为Wiki</button>
      <button class="ka-btn" data-click="navigateFromRedmineIssue" data-a0="reports" data-a1="${item.issue_id}" title="保留工单上下文并打开测试报告">📊 关联报告</button>
      <button class="ka-btn" data-click="navigateFromRedmineIssue" data-a0="automation" data-a1="${item.issue_id}" title="保留工单上下文并打开 GMS ATS">⚙️ 关联 ATS</button>
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
    return `<button class="page-num${active ? ' active' : ''}"${active ? ' disabled' : ''} data-click="loadIssues" data-a0="${p}">${label}</button>`;
  };

  let html = `<button data-click="loadIssues" data-a0="1"${current === 1 ? ' disabled' : ''}>首页</button>`;
  html += `<button data-click="loadIssues" data-a0="${current-1}"${current === 1 ? ' disabled' : ''}>上一页</button>`;
  for (const p of pageWindow()) {
    if (p === '…') html += `<span class="muted" style="line-height:32px">…</span>`;
    else html += numBtn(p, p);
  }
  html += `<button data-click="loadIssues" data-a0="${current+1}"${current === pages ? ' disabled' : ''}>下一页</button>`;
  html += `<button data-click="loadIssues" data-a0="${pages}"${current === pages ? ' disabled' : ''}>末页</button>`;
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
      <div class="run-item ${run.run_id === currentRunId ? 'active' : ''}" data-click="loadRun" data-a0="${esc(run.run_id)}">
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
    const clickAttr = count > 0 ? ` style="cursor:pointer" data-click="showRedmineTrendDetail" data-a0="${esc(keyName)}" data-a1="${esc(String(label))}" data-a2="${esc(namesArg)}" data-a3="${esc(profileArg)}" title="点击查看该时段解决的问题单"` : '';
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
      <button class="trend-start-btn" data-click="setTrendStartDate" data-a0="${esc(chartKey)}" data-a1="${esc(title)}" title="${esc(tip)}">⚙</button>
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
      ? `<button class="ka-btn" data-click="agentReplyDraft" data-a0="${issueId}" data-r1="el" data-stop title="AI 生成回复草稿+补丁方向(联网拉取工单详情与历史回复)">✉️ 回复草稿</button>`
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
    // 跳转锚点走 act-bridge 委托：data-click 只放 handler 名 + data-a0
    // 放 section id（此前把整条调用表达式塞进 data-click，按名查表
    // 查不到函数，点击静默失效）。
    var click = card.clickSection ? ' data-click="scrollToSection" data-a0="' + esc(card.clickSection) + '"' : '';
    return '<div class="stat-card' + cls + '"' + click + '><div class="value">' + esc(card.value == null ? 0 : card.value) + '</div><div class="label">' + esc(card.label || '') + '</div></div>';
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
  // 切换部门本就强制刷新（refresh=true）；补上刷新按钮忙碌态，
  // 让「正在拉取新部门数据」有可见反馈。
  withRefreshButtonBusy(function() { return loadDepartmentOverdue(true); });
}

async function openMemberDashboard(name) {
  // 部门看板只承载统计数据；点击成员行跳转到该成员的个人看板
  // （tab=stats + ?name=），不在部门看板内罗列个人问题明细。
  var memberName = String(name || '').trim();
  if (!memberName) return;
  var url = new URL(window.location.href);
  url.searchParams.set('name', memberName);
  window.history.replaceState({}, '', url.toString());
  var existingSelect = document.getElementById('statsUserSelect');
  if (existingSelect) existingSelect.value = memberName;
  // force=true：跳转即按新成员强制重拉，不展示上一位成员的缓存渲染；
  // 与手动切换统计身份一致，刷新按钮进入刷新中状态。
  await withRefreshButtonBusy(function() { return switchTab('stats', true); });
  var select = document.getElementById('statsUserSelect');
  if (select && select.value !== memberName) {
    // 下拉选项尚未包含该成员（users 接口未就绪）时兜底对齐一次。
    select.value = memberName;
    await onStatsUserChange();
  }
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
    <br><button class="secondary" style="margin-top:12px" data-click="showSettingsModal">打开设置</button>
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
    <select id="departmentProfileSelect" data-change="onDepartmentProfileChange" style="min-width:160px">
      ${departmentOptionsHtml(departmentProfileId, true)}
    </select>
    <button class="select-add-btn" type="button" data-click="showAddDepartmentModal" data-a0="departmentProfileSelect" title="添加部门">＋</button>
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
    return `<tr style="cursor:pointer" title="查看个人看板" data-click="openMemberDashboard" data-a0="${esc(user.name || '')}">
      <td class="col-person"><strong>${nameLine}</strong>${subLine}</td>
      <td>${user.total_owned || 0}</td>
      <td>${user.open_count || 0}</td>
      <td>${user.scanned_open_count || 0}</td>
      <td>${user.waiting_my_reply || 0}</td>
      <td><strong style="color:var(--bad)">${user.no_reply_3_days || 0}</strong></td>
      <td>${user.customer_no_reply_3_days || 0}</td>
      <td>${user.max_unreplied_days || 0}</td>
      <td data-click="_actStopPropagation" data-r0="event">
        <button class="secondary dept-action-btn"${copyDisabled} data-click="copyDepartmentIssues" data-a0="${esc(user.id || '')}" data-r1="el">复制3天未回复工单</button>
        <button class="secondary dept-action-btn"${copyDisabled} data-click="sendDepartmentReminder" data-a0="${esc(user.id || '')}" data-r1="el">邮箱</button>
      </td>
    </tr>`;
  }).join('');
  const table = `<div class="dept-table-wrap">
    <table class="dept-table">
      <thead><tr><th class="col-person">人员</th><th>历史数量</th><th>未关闭</th><th>本地未关闭</th><th>待回复</th><th>RK ${sd}天未回复</th><th>客户 ${sd}天未回复</th><th>最长未回复天数</th><th>操作</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="9" class="muted">暂无配置用户</td></tr>'}</tbody>
    </table>
  </div>`;
  document.getElementById('departmentContent').innerHTML = `
    <section class="stats-section">
      ${renderSummaryHeader((profile.name || '部门') + ' Redmine 未回复汇总', '<div class="filter-bar">' + profileSelect + '</div>', '更新时间: ' + esc(generatedAt) + ' | 阈值: ' + esc(data.stale_days || 3) + ' 天 | 缓存: ' + (data.cache_hit ? '是' : '否'))}
      ${cards}
    </section>
    ${trendPanels}
    ${table}
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
      + '<select id="statsUserSelect" data-change="onStatsUserChange" style="width:160px">'
      + '<option value="' + esc(selectedName || '加载中...') + '">' + esc(selectedName || '加载中...') + '</option>'
      + '</select>'
      + '<button class="select-add-btn" data-click="showAddUserModal" title="添加用户">＋</button>'
      + '</div>';

    box.innerHTML = `
      <section class="stats-section">
        ${renderSummaryHeader('Redmine概览', '<div class="filter-bar">' + userSelectHtml + '</div>', '统计身份: ' + ((meta.owner_names || []).map(esc).join(' / ') || '未识别') + ' | 统计口径: ' + (meta.count_source === 'redmine_live' ? 'Redmine实时全历史' : '本地同步快照') + ' | 更新时间: ' + esc((meta.generated_at || '-').replace('T', ' ').replace(/:\d{2}$/, '')))}
        ${renderStatsCards([
          {value: workload.open_count || 0, label: '当前未关闭', className: 'warn'},
          {value: workload.waiting_my_reply || 0, label: '待回复 ⬇', className: 'bad clickable-stat', clickSection: 'sec-waiting-reply'},
          {value: workload.no_reply_3_days || 0, label: 'RK ' + sd + '天未回复客户 ⬇', className: 'bad clickable-stat', clickSection: 'sec-no-reply-3d'},
          {value: workload.customer_no_reply_3_days || 0, label: '客户 ' + sd + '天未回复RK ⬇', className: 'warn clickable-stat', clickSection: 'sec-customer-no-reply'},
          {value: workload.missing_test_report || 0, label: '缺失测试报告 ⬇', className: 'warn clickable-stat', clickSection: 'sec-missing-report'},
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
    <select id="projectProfileSelect" data-change="onProjectProfileChange" style="min-width:220px">${projectOptionsHtml(projectProfileId)}</select>
    <button class="select-add-btn" type="button" data-click="showAddProjectModal" title="添加项目">＋</button>
  </div>`;
  const openOnlyBtn = `<button class="secondary toggle-btn ${projectOpenOnly ? 'active' : ''}" data-click="toggleProjectOpenOnly">${projectOpenOnly ? '显示全员' : '仅未关闭人员'}</button>`;
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
    return `<tr style="cursor:pointer" data-click="scrollToSection" data-a0="project-user-${esc(user.id || '')}">
      <td class="col-person"><strong>${esc(user.name || '-')}</strong></td>
      <td>${user.total_owned || 0}</td>
      <td>${user.open_count || 0}</td>
      <td>${user.closed_count || 0}</td>
      <td data-click="_actStopPropagation" data-r0="event">
        <button class="secondary dept-action-btn"${actionDisabled} data-click="copyProjectIssues" data-a0="${esc(user.id || '')}" data-r1="el">复制</button>
        <button class="secondary dept-action-btn"${actionDisabled} data-click="sendProjectReminder" data-a0="${esc(user.id || '')}" data-r1="el">邮箱</button>
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
      box.innerHTML = '<div class="muted" style="padding:20px">暂无项目看板配置。<button style="margin-left:10px" data-click="showAddProjectModal">＋ 添加项目</button></div>';
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
  window.EmbeddedModalController.open('resetModal');
}

function hideResetModal() {
  window.EmbeddedModalController.close('resetModal');
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
const REDMINE_CASE_VIEW_STORAGE_KEY = 'redmineCaseView';
const REDMINE_CASE_VIEWS = new Set(['facts', 'cases']);
let caseView = new URLSearchParams(window.location.search).get('case_view') || '';
if (!REDMINE_CASE_VIEWS.has(caseView)) {
  try { caseView = window.sessionStorage.getItem(REDMINE_CASE_VIEW_STORAGE_KEY) || ''; } catch(_) { caseView = ''; }
}
if (!REDMINE_CASE_VIEWS.has(caseView)) caseView = 'facts';

function switchCaseView(view, {persist = true, load = true} = {}) {
  caseView = REDMINE_CASE_VIEWS.has(view) ? view : 'facts';
  document.getElementById('viewBtnFacts').classList.toggle('active', caseView === 'facts');
  document.getElementById('viewBtnCases').classList.toggle('active', caseView === 'cases');
  if (persist) {
    try { window.sessionStorage.setItem(REDMINE_CASE_VIEW_STORAGE_KEY, caseView); } catch(_) {}
    var url = new URL(window.location.href);
    if (caseView === 'facts') url.searchParams.delete('case_view');
    else url.searchParams.set('case_view', caseView);
    window.history.replaceState({}, '', url.toString());
  }
  currentCaseOffset = 0;
  if (load) loadCases();
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
    box.innerHTML = '<div class="muted" style="padding:14px">暂无已导入的工单。点击顶部「📚 导入」粘贴工单号,或导入最近20个我的指派。</div>';
    return;
  }
  box.innerHTML = items.map(f => {
    const sig = f.error_signature ? `<span class="case-sig">${esc(f.error_signature)}</span>` : '';
    const scope = [f.chip_platform, f.android_version, f.certification_type, f.module].filter(Boolean).map(esc).join(' / ');
    const conf = f.confidence ? `<span class="muted" style="float:right">置信度 ${f.confidence}</span>` : '';
    return `<div class="case-card" data-click="showCaseFact" data-a0="${f.issue_id}">
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
    box.innerHTML = '<div class="muted" style="padding:14px">暂无成熟案例。请先点击顶部「📚 导入」结构化历史工单,再选中若干案例构建成熟案例。</div>';
    return;
  }
  box.innerHTML = items.map(c => {
    const status = c.status || 'draft';
    const badge = status === 'approved' ? '✅已审核' : status === 'draft' ? '📝草稿' : esc(status);
    const sig = c.canonical_error_signature ? `<span class="case-sig">${esc(c.canonical_error_signature)}</span>` : '';
    const scope = [c.chip_platform, c.android_version, c.certification_type, c.module].filter(Boolean).map(esc).join(' / ');
    return `<div class="case-card" data-click="showCaseDetail" data-a0="${c.case_id}">
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
        ${c.status!=='approved'?`<button data-click="approveCase" data-a0="${caseId}">✅ 审核通过</button>`:''}
        <button class="secondary" data-click="draftReply" data-a0="${sources[0]||0}" data-a1="${caseId}">✉️ 生成回复</button>
        <button class="secondary" data-click="startCreateInternalCase" data-a0="${caseId}">📝 创建内部单</button>
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
      <div style="margin-top:8px"><button data-click="copyReplyDraft" data-r0="el">📋 复制</button></div>`;
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
      <div style="margin-top:8px"><button data-click="copyReplyDraft" data-r0="el">📋 复制</button></div>`;
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
        <button data-click="buildMatureFromIssue" data-a0="${issueId}">🏗️ 构建成熟案例</button>
        <button class="secondary" data-click="draftReply" data-a0="${issueId}">✉️ 生成回复</button>
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
switchCaseView(caseView, {persist: false, load: false});
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
      if (btn && btn.disabled) btn.disabled = false;
      updateRedmineToolbar();
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
// 窄屏页头（tabs+筛选行）换行变高后，晨报工具栏吸附点需要重算。
window.addEventListener('resize', function () {
  if (currentTab === 'daily-brief') syncDailyBriefStickyTop();
});

// ==================== 每日晨报（Redmine Daily Brief） ====================
// 数据源：GET /api/redmine-agent/daily-brief/latest（只读展示；生成/重分析走 POST）。

var dailyBriefCache = null;
var dailyBriefConfigCache = null;
var singleIssueAnalysisRunId = '';
var singleIssueAnalysisTimer = null;
var singleIssueAnalysisHistory = [];
var singleIssueAnalysisPage = 1;
var singleIssueHistoryLookupTimer = null;
var singleIssueHistoryLookupValue = '';
var singleIssueDevicesLoading = null;
var singleIssueIndexedHistoryId = '';
var singleIssueIndexedHistoryTimer = null;
var singleIssueHistoryRunTimers = {};
var singleIssueStopRequested = {};
var SINGLE_ISSUE_ANALYSIS_PAGE_SIZE = 8;
var singleIssueAnalysisHint = '';

function updateSingleIssueAnalysisHintButton() {
  var button = document.getElementById('singleIssueAnalysisHintButton');
  if (!button) return;
  var saved = Boolean(singleIssueAnalysisHint.trim());
  button.textContent = saved ? '辅助说明 · 已保存' : '辅助说明';
  button.title = saved ? '已保存辅助分析说明，点击编辑' : '输入辅助分析说明';
}

function openSingleIssueAnalysisHint() {
  var modalId = 'singleIssueAnalysisHintModal-' + Date.now();
  var modal = document.createElement('div');
  modal.id = modalId;
  modal.className = 'modal';
  modal.innerHTML = '<div class="modal-content single-issue-analysis-hint-modal">'
    + '<div class="modal-header"><span class="modal-title">🤖 辅助分析说明</span>'
    + '<button type="button" class="modal-close" aria-label="关闭" data-click="removeDynamicModal" data-a0="' + modalId + '">&times;</button></div>'
    + '<div class="modal-body"><label for="' + modalId + '-text">提供已观察的现象、复现结果或希望重点核查的实际值；kkagent 会将其作为待验证线索。</label>'
    + '<textarea id="' + modalId + '-text" maxlength="4000" placeholder="例如：经验证补丁无效，需要查看设备上的实际值 notification_custom_view_max_image_width。">' + esc(singleIssueAnalysisHint) + '</textarea>'
    + '<div class="muted">最多 4000 个字符。</div></div>'
    + '<div class="modal-footer"><button type="button" class="secondary" data-click="removeDynamicModal" data-a0="' + modalId + '">取消</button>'
    + '<button type="button" class="ka-btn primary" data-single-issue-save-hint>保存</button></div></div>';
  document.body.appendChild(modal);
  modal.querySelector('[data-single-issue-save-hint]').addEventListener('click', function () {
    singleIssueAnalysisHint = String(modal.querySelector('textarea').value || '').trim();
    updateSingleIssueAnalysisHintButton();
    removeDynamicModal(modalId);
  });
  showModal(modalId);
  requestAnimationFrame(function () { modal.querySelector('textarea').focus(); });
}
window.openSingleIssueAnalysisHint = openSingleIssueAnalysisHint;

function rememberSingleIssueAnalysisRun(runId) {
  singleIssueAnalysisRunId = String(runId || '');
}

function setSingleIssueAnalysisBusy(busy) {
  // 已提交的任务在下方历史卡片中管理；表单始终可用于发起下一单分析。
  document.getElementById('singleIssueAnalysisStart').disabled = false;
  document.getElementById('singleIssueAnalysisId').disabled = false;
}

function resetSingleIssueAnalysisForm() {
  var input = document.getElementById('singleIssueAnalysisId');
  var mode = document.getElementById('singleIssueAnalysisMode');
  var noDevice = document.querySelector('#singleIssueAnalysisDevice [data-device-none]');
  if (input) input.value = '';
  if (mode) mode.value = 'incremental';
  singleIssueAnalysisHint = '';
  updateSingleIssueAnalysisHintButton();
  if (noDevice) noDevice.checked = true;
  document.querySelectorAll('#singleIssueAnalysisDevice input[data-device-serial]').forEach(function (device) {
    device.checked = false;
  });
  syncSingleIssueDevicePickerLabel();
  setSingleIssueDevicePickerOpen(false);
  if (input) input.focus();
}

function singleIssueAnalysisStatus(status) {
  return ({pending: '⏳ 排队中', snapshotting: '⏳ 准备中', analyzing: '⏳ 分析中', running: '⏳ 分析中',
    completed: '✓ 已完成', partial: '⚠ 部分完成', failed: '❌ 分析失败',
    cancelled: '■ 已停止'})[status] || status || '—';
}

function singleIssueAnalysisEffectiveStatus(run, issue) {
  // A cancelled run is terminal even if an older record still carries a
  // pending issue state. The run-level cancellation is authoritative.
  if (String((run || {}).status || '') === 'cancelled') return 'cancelled';
  return String((issue || {}).status || (run || {}).status || '');
}

function singleIssueAnalysisIsRunning(run, issue) {
  var states = ['pending', 'snapshotting', 'analyzing', 'running'];
  return states.indexOf(singleIssueAnalysisEffectiveStatus(run, issue)) >= 0
    || states.indexOf(String((run || {}).status || '')) >= 0;
}

function isAnalysisTimelineWithinRetention(finishedAt) {
  // 终态过程事件只保留 ANALYSIS_EVENT_RETENTION_DAYS 天（Worker 启动时
  // 清理超期部分）。无时间戳的旧记录按仍在保留期处理，交给服务端兜底。
  var stamp = Date.parse(String(finishedAt || ''));
  if (!isFinite(stamp)) return true;
  return Date.now() - stamp < ANALYSIS_EVENT_RETENTION_DAYS * 24 * 60 * 60 * 1000;
}

function singleIssueAnalysisHasHistory(run, issue) {
  // 终态且未超期的 run 才有可回看的执行过程；运行中走实时时间线。
  if (!run || !run.run_id) return false;
  if (singleIssueAnalysisIsRunning(run, issue)) return false;
  return isAnalysisTimelineWithinRetention(run.finished_at || run.started_at);
}

function singleIssueAnalysisHistoryDisabledHint(run, issue) {
  // 「执行过程」置灰提示须与真实原因一致：运行中尚未形成可回看过程、
  // 无 run 记录、终态超 30 天被清理，三者文案不同，不能一律说「已清理」。
  if (run && run.run_id && singleIssueAnalysisIsRunning(run, issue)) {
    return '分析仍在进行中，结束后可回看执行过程';
  }
  if (!run || !run.run_id) return '暂无可回看的执行过程';
  return '过程事件已清理（终态过程仅保留 ' + ANALYSIS_EVENT_RETENTION_DAYS + ' 天）';
}

function latestSingleIssueRunForIssue(issueId) {
  // 同单号状态联动：取「Redmine 单号分析」历史中该 issue 最新的 run。
  // 历史数组本身就是最新在前，started_at 只用来比较轮询插入后的顺序。
  var wanted = String(issueId == null ? '' : issueId);
  if (!wanted || !singleIssueAnalysisHistory.length) return null;
  var best = null;
  var bestKey = '';
  singleIssueAnalysisHistory.forEach(function (entry) {
    if (!entry || !entry.run || !entry.run.run_id) return;
    var issue = (entry.issues || [])[0] || {};
    if (String(issue.issue_id) !== wanted) return;
    var key = String(entry.run.started_at || '');
    if (!best || (key && (!bestKey || key > bestKey))) {
      best = entry;
      bestKey = key;
    }
  });
  return best;
}

function renderDailyBriefFromCache() {
  // 单号分析状态变化时同步刷新晨报卡片（同单号状态联动），无晨报数据则跳过。
  var card = document.getElementById('dailyBriefCard');
  if (card && dailyBriefCache) card.innerHTML = renderDailyBriefInner(dailyBriefCache);
}

function upsertSingleIssueAnalysis(item) {
  if (!item || !item.run || !item.run.run_id) return;
  var id = String(item.run.run_id);
  var issueId = String(((item.issues || [])[0] || {}).issue_id || '');
  var previousIndex = singleIssueAnalysisHistory.findIndex(function (entry) {
    var entryIssue = ((entry || {}).issues || [])[0] || {};
    return entry && entry.run && (String(entry.run.run_id) === id || String(entryIssue.issue_id) === issueId);
  });
  // Polling and historical refreshes can legitimately omit expensive fields
  // (report/statistics/title).  Merge those partial replies with the last
  // durable row so a refresh never makes already-visible data disappear.
  if (previousIndex >= 0) {
    var previous = singleIssueAnalysisHistory[previousIndex];
    var priorIssue = ((previous.issues || [])[0] || {});
    var nextIssue = ((item.issues || [])[0] || {});
    item = {
      run: Object.assign({}, previous.run || {}, item.run || {}),
      issues: [Object.assign({}, priorIssue, nextIssue)],
    };
  }
  // Polling must update the existing row in place. Moving every response to
  // the front makes concurrently-running issues swap positions every 5 s.
  if (previousIndex >= 0) {
    singleIssueAnalysisHistory.splice(previousIndex, 1, item);
  } else {
    singleIssueAnalysisHistory.unshift(item);
  }
}

function renderSingleIssueAnalysisHistory() {
  // 单号分析历史变化（轮询/停止/完成）时，晨报行的同单号状态联动随之刷新。
  renderDailyBriefFromCache();
  var box = document.getElementById('singleIssueAnalysisHistory');
  var pagination = document.getElementById('singleIssueAnalysisPagination');
  if (!box) return;
  var items = singleIssueAnalysisHistory.filter(function (item) {
    return item && item.run && Array.isArray(item.issues) && item.issues.length;
  });
  // 「Redmine 单号」输入框只做定位不过滤：命中时由 focusSingleIssueHistory
  // 翻页/滚动/高亮，未命中时完整保留历史列表。
  var pageCount = Math.max(1, Math.ceil(items.length / SINGLE_ISSUE_ANALYSIS_PAGE_SIZE));
  singleIssueAnalysisPage = Math.max(1, Math.min(singleIssueAnalysisPage, pageCount));
  var pageItems = items.slice(
    (singleIssueAnalysisPage - 1) * SINGLE_ISSUE_ANALYSIS_PAGE_SIZE,
    singleIssueAnalysisPage * SINGLE_ISSUE_ANALYSIS_PAGE_SIZE,
  );
  if (!items.length) {
    box.innerHTML = '<div class="muted">暂无历史分析。输入 Redmine 单号可开始新的分析。</div>';
    if (pagination) pagination.innerHTML = '';
    return;
  }
  box.innerHTML = pageItems.map(function (item) {
      var run = item.run || {};
      var issue = item.issues[0] || {};
      var meta = singleIssueAnalysisMeta(run, issue);
      var subject = String(issue.subject || '').trim();
      var running = singleIssueAnalysisIsRunning(run, issue);
      var runActive = ['pending', 'snapshotting', 'analyzing', 'running']
        .indexOf(String(run.status || '')) >= 0;
      var stopping = runActive && Boolean(
        singleIssueStopRequested[String(run.run_id)] || run.cancel_requested
      );
      var displayStatus = singleIssueAnalysisEffectiveStatus(run, issue);
      if (subject === '#' + issue.issue_id) subject = '';
      return '<article class="single-issue-analysis-entry'
        + (String(issue.issue_id) === singleIssueIndexedHistoryId ? ' is-indexed' : '')
        + '" data-single-issue-run="' + esc(run.run_id)
        + '" data-single-issue-id="' + esc(issue.issue_id) + '">'
        + '<div class="daily-brief-row"><div class="daily-brief-main"><span class="daily-brief-priority" title="手动提交分析">🔴</span>'
        + '<span class="daily-brief-issue-title"><b>#' + esc(issue.issue_id) + '</b>'
        + (subject ? ' ' + esc(subject) : '') + '<span class="single-issue-analysis-meta">' + meta + '</span></span></div>'
        + '<span class="daily-brief-state ' + (displayStatus === 'failed' ? 'failed' : '') + '">' + esc(stopping ? '⏳ 停止中' : singleIssueAnalysisStatus(displayStatus)) + '</span>'
        + '<div class="daily-brief-row-actions">'
        + '<button class="ka-btn" data-single-issue-view="' + esc(run.run_id) + '">查看分析</button>'
        + '<button class="ka-btn" data-click="openRedmineIssue" data-a0="' + esc(issue.issue_id) + '">打开 Redmine</button>'
        + '<button class="ka-btn" data-single-issue-statistics="' + esc(run.run_id) + '">AI 统计</button>'
        + '<select class="single-issue-reanalysis-mode" data-single-issue-reanalysis-mode aria-label="#' + esc(issue.issue_id) + ' 重新分析方式"' + (running ? ' disabled' : '') + '>'
        + '<option value="incremental">增量</option><option value="full">全量</option></select>'
        + (running
          ? '<button class="ka-btn" data-single-issue-cancel="' + esc(run.run_id) + '"' + (stopping ? ' disabled' : '') + '>' + (stopping ? '停止中…' : '停止分析') + '</button>'
          : '<button class="ka-btn" data-single-issue-reanalyze="' + esc(run.run_id) + '" data-single-issue-id="' + esc(issue.issue_id) + '">重新分析</button>')
        + '</div></div></article>';
    }).join('');
  if (pagination) {
    pagination.innerHTML = pageCount > 1
      ? '<button class="ka-btn" data-single-issue-page="' + (singleIssueAnalysisPage - 1) + '"' + (singleIssueAnalysisPage === 1 ? ' disabled' : '') + '>上一页</button>'
        + '<span class="muted">' + singleIssueAnalysisPage + ' / ' + pageCount + '</span>'
        + '<button class="ka-btn" data-single-issue-page="' + (singleIssueAnalysisPage + 1) + '"' + (singleIssueAnalysisPage === pageCount ? ' disabled' : '') + '>下一页</button>'
      : '';
  }
}

function focusSingleIssueHistory(issueId) {
  var target = String(issueId);
  var index = singleIssueAnalysisHistory.findIndex(function (item) {
    var issue = ((item || {}).issues || [])[0] || {};
    return String(issue.issue_id) === target;
  });
  if (index < 0) return false;
  singleIssueIndexedHistoryId = target;
  clearTimeout(singleIssueIndexedHistoryTimer);
  singleIssueAnalysisPage = Math.floor(index / SINGLE_ISSUE_ANALYSIS_PAGE_SIZE) + 1;
  renderSingleIssueAnalysisHistory();
  singleIssueIndexedHistoryTimer = setTimeout(function () {
    if (singleIssueIndexedHistoryId !== target) return;
    singleIssueIndexedHistoryId = '';
    renderSingleIssueAnalysisHistory();
  }, 2600);
  requestAnimationFrame(function () {
    var row = document.querySelector('[data-single-issue-id="' + target + '"]');
    if (row) row.scrollIntoView({behavior: 'smooth', block: 'center'});
  });
  return true;
}

async function lookupSingleIssueHistory(issueId) {
  var target = String(issueId || '').trim();
  if (!/^[0-9]+$/.test(target)) return false;
  if (focusSingleIssueHistory(target)) return true;
  try {
    var data = await api('/api/redmine-agent/daily-brief/issue-analyses/' + encodeURIComponent(target)) || {};
    if (data.run && Array.isArray(data.issues) && data.issues.length) {
      upsertSingleIssueAnalysis(data);
      return focusSingleIssueHistory(target);
    }
  } catch (_) {
    // A 404 means that this is a new number; the submit path can analyse it.
  }
  return false;
}

// 单号分析与每日晨报行共用的实机取证六态标签。device_evidence_status 由
// Controller 端 _tool_status 从真实 trace 推导，非模型自报。
function dailyBriefEvidenceTag(evidenceStatus) {
  var status = String(evidenceStatus || '').trim();
  if (!status) return '';
  var text = ({
    succeeded: '实机取证成功',
    service_unavailable: '取证服务暂不可用',
    invalid_request: '取证请求无效',
    device_unavailable: '设备取证未完成',
    unavailable: '实机取证失败',
    collecting: '实机取证中',
    not_collected: '未执行实机取证'
  })[status] || '未执行实机取证';
  var evidenceClass = status === 'succeeded' ? 'evidence-ok'
    : (['service_unavailable', 'invalid_request', 'device_unavailable', 'unavailable'].includes(status)
      ? 'evidence-failed' : 'evidence-pending');
  return '<span class="' + evidenceClass + '">' + esc(text) + '</span>';
}

function dailyBriefEvidenceGateNotice(result) {
  var value = result && typeof result === 'object' ? result : {};
  if (!value.needs_human_review) return '';
  var gate = value.evidence_gate && typeof value.evidence_gate === 'object'
    ? value.evidence_gate : {};
  var findings = [];
  if (gate.attachments_checked === false) findings.push('附件尚未全部读取或校验');
  if (gate.history_search_required !== false && gate.history_checked !== true) {
    findings.push('相似历史工单检索未达到要求');
  }
  if (gate.source_evidence_required === true && gate.source_evidence_checked !== true) {
    findings.push('测试失败缺少可追溯的源码级证据');
  }
  if (!findings.length) findings.push('运行时证据校验尚未闭环');
  return '<div class="daily-brief-warning"><div><b>⚠️ 取证未闭环，仅供人工复核</b><ul>'
    + findings.map(function (item) { return '<li>' + esc(item) + '</li>'; }).join('')
    + '</ul></div></div>';
}

function singleIssueAnalysisMeta(run, issue) {
  var timestamp = String(run.finished_at || run.started_at || '').replace('T', ' ').slice(0, 16);
  var tags = timestamp ? ['<span>分析时间 ' + esc(timestamp) + '</span>'] : [];
  var serial = String(run.device_serial || '').trim();
  if (serial) {
    tags.push('<span>设备 ' + esc(serial) + '</span>');
    tags.push(dailyBriefEvidenceTag(((issue.ai_execution || {}).device_evidence_status) || 'not_collected'));
  }
  return tags.join('');
}

function findSingleIssueAnalysis(runId) {
  return singleIssueAnalysisHistory.find(function (item) {
    return item && item.run && String(item.run.run_id) === String(runId);
  }) || null;
}

function singleIssueAnalysisReportBody(item) {
  // 「查看分析」静态报告弹框与实时进度弹框（终态切换）共用的正文构建。
  var issue = (item.issues || [])[0] || {};
  var previousAnalysis = issue.previous_analysis || {};
  var report = String((issue.result || {}).detailed_report || '').trim();
  var previousReport = String((previousAnalysis.result || {}).detailed_report || '').trim();
  var usesPreviousReport = !report && Boolean(previousReport);
  var displayedResult = usesPreviousReport ? (previousAnalysis.result || {}) : (issue.result || {});
  if (!report) report = previousReport;
  var previousAt = String(previousAnalysis.finished_at || '').replace('T', ' ').slice(0, 16);
  var previousNotice = usesPreviousReport
    ? '<div class="daily-brief-warning">最新一次分析已停止，以下展示最近一次已保存的分析结论'
      + (previousAt ? '（' + esc(previousAt) + '）' : '') + '。</div>'
    : '';
  if (report) {
    return previousNotice + dailyBriefEvidenceGateNotice(displayedResult)
      + '<div class="daily-brief-section daily-brief-section-md"><div class="daily-brief-section-body analysis-doc">'
      + renderMarkdownDoc(report) + '</div></div>';
  }
  return previousNotice + '<div class="muted">' + esc(issue.error || singleIssueAnalysisStatus(issue.status || item.run.status)) + '</div>';
}

function showSingleIssueAnalysis(runId, statisticsOnly) {
  var item = findSingleIssueAnalysis(runId);
  var issue = item && (item.issues || [])[0];
  if (item && issue) openSingleIssueReportModal(item, statisticsOnly);
}

// 「查看分析」弹框统一构建：单号分析历史条目与每日晨报条目共用同一布局
// （item = {run, issues: [issue]}；标题「🤖 AI 分析 · #id · 状态」+ 纯
// Markdown 报告正文 + 底部「执行过程 / 关闭」）。AI 统计、重新分析入口
// 都在行级按钮上，弹框内不再重复。
function openSingleIssueReportModal(item, statisticsOnly) {
  var issue = (item.issues || [])[0] || {};
  if (!issue.issue_id) return;
  var statistics = renderDailyBriefIssueStatistics(issue.ai_statistics);
  var subject = String(issue.subject || '').trim();
  var statusText = singleIssueAnalysisStatus(issue.status || item.run.status);
  var currentResult = issue.result || {};
  var previousResult = (issue.previous_analysis || {}).result || {};
  var displayedResult = String(currentResult.detailed_report || '').trim()
    ? currentResult : previousResult;
  if (!statisticsOnly && displayedResult.needs_human_review) {
    statusText = '⚠ 取证未闭环';
  }
  var body = statisticsOnly
    ? (statistics || '<div class="muted">本单号尚未保存可展示的 AI 统计。</div>')
    : singleIssueAnalysisReportBody(item);
  var modalId = 'singleIssueAnalysisModal-' + Date.now();
  // 30 天保留期内的终态 run 提供执行过程回看；超期按钮禁用并提示。
  var hasHistory = !statisticsOnly && singleIssueAnalysisHasHistory(item.run, issue);
  var hasSession = !statisticsOnly
    && Boolean(String((issue.ai_execution || {}).session_id || '').trim())
    && Boolean(String(item.run.run_id || '').trim());
  var modal = document.createElement('div');
  modal.id = modalId;
  modal.className = statisticsOnly ? 'modal' : 'modal daily-brief-analysis-overlay';
  modal.innerHTML = '<div class="modal-content daily-brief-modal' + (statisticsOnly ? ' daily-brief-statistics-modal' : '') + '">'
    + '<div class="modal-header"><span class="modal-title daily-brief-modal-title"><span>'
    + (statisticsOnly ? '📊 AI 统计' : '🤖 AI 分析') + ' · #' + esc(issue.issue_id)
    + (statusText ? ' · ' + esc(statusText) : '') + '</span>'
    + (subject ? '<span class="daily-brief-modal-subject">' + esc(subject) + '</span>' : '')
    + '</span><button type="button" class="modal-close" aria-label="关闭" data-click="removeDynamicModal" data-a0="' + modalId + '">&times;</button></div>'
    + '<div class="modal-body daily-brief-modal-body">' + body + '</div>'
    + '<div class="modal-buttons daily-brief-modal-footer">'
    + (statisticsOnly ? '' : '<button class="secondary" data-click="openSingleIssueAnalysisTimeline" data-a0="' + esc(issue.issue_id) + '"'
      + (hasHistory ? '' : ' disabled title="' + esc(singleIssueAnalysisHistoryDisabledHint(item.run, issue)) + '"') + '>执行过程</button>')
    + (hasSession
      ? '<button class="secondary" data-click="showIssueFullSession" data-a0="' + esc(item.run.run_id)
        + '" data-a1="' + esc(issue.issue_id) + '" data-a2="' + esc(subject) + '" data-prevent>完整会话</button>'
      : '')
    + '<button class="secondary" data-click="removeDynamicModal" data-a0="' + modalId + '">关闭</button></div></div>';
  if (statisticsOnly) {
    document.body.appendChild(modal);
    showModal(modalId);
  } else {
    showDailyBriefAnalysisModal(modal);
  }
}

function closeReplacedReportModal() {
  // 单弹框语义：「执行过程」入口打开时间线前，先移除底下的报告弹框
  // （AI 分析 / 每日晨报），避免叠两层要关两次。
  var open = document.querySelectorAll('.modal.show');
  for (var index = open.length - 1; index >= 0; index -= 1) {
    var modalId = String(open[index].id || '');
    if (/^(singleIssueAnalysisModal|dailyBriefIssueModal)-/.test(modalId)) {
      removeDynamicModal(modalId);
      return;
    }
  }
}

function openSingleIssueAnalysisTimeline(issueId) {
  // 「执行过程」回看入口：历史条目用列表缓存定位 run；晨报条目回落到
  // dailyBriefCache。终态走历史时间线（不自动切报告）；运行中走实时
  // 时间线。事件超期清理时由时间线空态提示（按钮置灰仅覆盖能判断的场景）。
  var id = String(issueId || '');
  var item = findSingleIssueAnalysisByIssueId(id);
  var run = (item || {}).run || {};
  var issue = ((item || {}).issues || [])[0] || {};
  if (!run.run_id && dailyBriefCache && dailyBriefCache.run) {
    run = dailyBriefCache.run;
    issue = findDailyBriefIssue(id) || issue;
  }
  if (!run.run_id || !id) return;
  // 报告弹框被时间线弹框替换而非叠加；要回看报告用时间线里的「查看报告」。
  closeReplacedReportModal();
  if (singleIssueAnalysisIsRunning(run, issue)) {
    showAnalysisTimelineModal(run.run_id, issue.issue_id || Number(id), { subject: issue.subject });
    return;
  }
  showAnalysisTimelineModal(run.run_id, issue.issue_id || Number(id), {
    subject: issue.subject, history: true,
  });
}

// ---- 实时分析进度时间线（「查看分析」弹框优化） ----
// Worker 在分析执行期把标准化进度事件落库（redmine_daily_brief_analysis_events，
// consume_event 分流），这里以 2.5s 增量轮询（after_sequence 协议）渲染执行
// 时间线；run 到达终态后原地切换为最终报告/失败状态。只更新弹框 body 的
// innerHTML，显示控制仍归 ModalManager 单一所有者；轮询循环以
// getElementById 检测弹框被移除并自行退出（abort + 停止调度）。
// 过程事件保留天数（与 features/redmine/daily_brief_analysis_events.py 的
// ANALYSIS_EVENT_RETENTION_DAYS 同步）：终态 run 的完整时间线在该窗口内
// 可回看，超期由 Worker 启动时清理，前端据此提示「过程已清理」。
var ANALYSIS_EVENT_RETENTION_DAYS = 30;

var analysisTimelineState = {
  modalId: '', runId: '', issueId: 0, after: 0, events: [],
  timer: null, controller: null, openedAt: 0,
  // history：历史回看模式（终态 run 的完整时间线，不轮询、不自动切报告）；
  // terminal/runStatus：终态后保留在 state 上供报告 ↔ 执行过程切换复用。
  history: false, terminal: false, runStatus: '',
};

function stopAnalysisTimelinePolling() {
  var state = analysisTimelineState;
  clearTimeout(state.timer);
  if (state.controller) { try { state.controller.abort(); } catch (_) {} }
  state.modalId = ''; state.runId = ''; state.issueId = 0; state.after = 0;
  state.events = []; state.timer = null; state.controller = null;
  state.history = false; state.terminal = false; state.runStatus = '';
}

function haltAnalysisTimelineLoop() {
  // 终态收尾：只停调度与在途请求，保留 run/事件快照，供「报告 ↔ 执行
  // 过程」原地切换；下一次 showAnalysisTimelineModal 会整体重置。
  var state = analysisTimelineState;
  clearTimeout(state.timer);
  if (state.controller) { try { state.controller.abort(); } catch (_) {} }
  state.timer = null; state.controller = null;
  state.terminal = true;
}

function analysisTimelineMarkEnded(stateLabel) {
  // 终态兜底视图（终态详情读取失败 / 记录不存在）：把「正在分析」弹框
  // 收敛为确定的结束视图——标题换「已结束」徽标、meta 换状态、移除
  // 停止按钮，footer 保留「查看报告」切换（瞬时失败可由此重试）。
  // 须在弹框仍在、state.runId 尚未清理时调用。
  var state = analysisTimelineState;
  var meta = document.getElementById(state.modalId + '-meta');
  if (meta) meta.innerHTML = '<span>状态 ' + esc(stateLabel) + '</span>';
  analysisTimelineSetTitle('timeline', stateLabel);
  setAnalysisTimelineFooterView('timeline');
  var stopButton = document.querySelector('[data-analysis-timeline-stop][data-a0="' + state.runId + '"]');
  if (stopButton) stopButton.remove();
}

function analysisTimelineRunStateLabel(runStatus) {
  return ({ pending: '排队中', snapshotting: '准备中', analyzing: '分析中',
    completed: '已完成', partial: '已完成（部分）', failed: '失败', cancelled: '已停止' })[runStatus] || runStatus || '—';
}

// 工具调用 → 人类可读动作短语：时间线正文用「在做什么」描述（参数摘要
// 跟随其后），原始工具名保留为行尾小标签。按子串匹配以兼容
// mcp__gms__x / gms_rt_x / 裸名等多种前缀；顺序即优先级，具体动作在前、
// 泛化在后。未命中时回退为去掉 MCP 前缀的工具名。
var ANALYSIS_TOOL_ACTIONS = [
  ['redmine_issue_fetch', '读取 Redmine 工单详情'],
  ['redmine_issue', '读取 Redmine 工单详情'],
  ['redmine_journals', '读取工单评论记录'],
  ['redmine_attachments', '读取附件清单'],
  ['redmine_image', '读取附件图片'],
  ['artifact_search', '检索附件与日志内容'],
  ['artifact_read', '读取附件内容'],
  ['history_search', '检索历史相似工单'],
  ['knowledge_search', '检索平台知识库'],
  ['redmine_triage', '读取待回复清单'],
  ['codesearch', '检索平台源码'],
  ['sdk_search', '检索 SDK 源码'],
  ['sdk_read', '读取 SDK 源码'],
  ['sdk_sources', '枚举 SDK 源配置'],
  ['apk_resolve', '定位测试模块 APK'],
  ['apk_analyze', '解析测试 APK'],
  ['apk_manifest', '读取 APK 权限清单'],
  ['apk_source_read', '读取反编译源码'],
  ['apk_source_search', '检索反编译源码'],
  ['apk_source', '浏览反编译源码树'],
  ['apk_search', '检索反编译源码'],
  ['apk_status', '查询 APK 解析进度'],
  ['shell_exec', '执行设备命令'],
  ['devices_snapshot', '采集设备快照'],
  ['screencap', '设备截屏取证'],
  ['logcat', '读取设备日志'],
  ['shell', '查询设备信息'],
  ['devices_wait', '等待设备上线'],
  ['devices_list', '枚举在线设备'],
  ['cluster_devices', '枚举集群设备'],
  ['cluster_workers', '枚举集群 Worker'],
  ['jobs_events', '读取测试任务事件'],
  ['jobs_follow', '跟踪测试任务进度'],
  ['jobs_status', '查询测试任务状态'],
  ['jobs_list', '枚举测试任务'],
  ['test_suites_list', '枚举测试套件'],
  ['reports_list', '查询测试报告'],
  ['doctor', '环境自检'],
  ['context', '环境自检'],
  ['commands', '枚举命令目录'],
  ['TodoWrite', '更新任务清单'],
  ['TodoList', '更新任务清单'],
  ['Bash', '执行本地命令'],
  ['Grep', '搜索本地文件'],
  ['Glob', '查找文件'],
  ['Read', '读取本地文件'],
  ['WebSearch', '网络搜索'],
  ['WebFetch', '读取网页'],
  ['Web', '网络检索'],
];

function analysisTimelineToolAction(toolName) {
  var name = String(toolName || '').trim();
  if (!name) return '';
  for (var i = 0; i < ANALYSIS_TOOL_ACTIONS.length; i += 1) {
    if (name.indexOf(ANALYSIS_TOOL_ACTIONS[i][0]) >= 0) return ANALYSIS_TOOL_ACTIONS[i][1];
  }
  return name.replace(/^mcp__[^_]+__/, '').replace(/^(gms_rt_|gms-rt-)/, '') || name;
}

// 把 summary（`tool(key=val, …)` 形态，失败事件带 ` · 原因` 尾巴）拆成
// 「动作短语 + 参数摘要 (+ 失败原因)」，返回已转义好的 HTML 片段与原始
// 工具名（供行尾标签展示）。
function analysisTimelineEventText(event) {
  var type = String(event.event_type || '');
  var summary = String(event.summary || '');
  if (['tool_started', 'tool_completed', 'tool_failed'].indexOf(type) < 0) {
    return { text: esc(summary || type), tool: '' };
  }
  var tool = String(event.tool_name || '');
  var args = summary;
  var failedReason = '';
  if (type === 'tool_failed') {
    var sep = args.indexOf(' · ');
    if (sep >= 0) { failedReason = args.slice(sep + 3); args = args.slice(0, sep); }
  }
  var openIdx = args.indexOf('(');
  if (openIdx >= 0) args = args.slice(openIdx + 1);
  if (args.slice(-1) === ')') args = args.slice(0, -1);
  var action = analysisTimelineToolAction(tool) || tool || '工具调用';
  return {
    text: '<b>' + esc(action) + '</b>'
      + (args ? ' <span class="analysis-timeline-args">' + esc(args) + '</span>' : '')
      + (failedReason ? ' <span class="analysis-timeline-fail-reason">' + esc(failedReason) + '</span>' : ''),
    tool: tool,
  };
}

function analysisTimelineEventRow(event) {
  var type = String(event.event_type || '');
  var cancelled = type === 'analysis_failed' && String(event.status || '') === 'cancelled';
  var cls = ({ tool_completed: 'done', analysis_completed: 'done',
    tool_failed: 'failed', analysis_failed: cancelled ? 'stopped' : 'failed' })[type] || 'running';
  var icon = ({ analysis_started: '▶', stage_changed: '◆', tool_started: '●',
    tool_completed: '✓', tool_failed: '✗', progress: '●',
    analysis_completed: '✓', analysis_failed: cancelled ? '■' : '✗' })[type] || '·';
  var time = String(event.created_at || '').replace('T', ' ').slice(11, 19);
  var duration = type === 'tool_completed' && Number(event.duration_ms) > 0
    ? '<span class="analysis-timeline-duration">' + (Math.round(Number(event.duration_ms) / 100) / 10) + 's</span>'
    : '';
  var parts = analysisTimelineEventText(event);
  return '<div class="analysis-timeline-item ' + cls + '">'
    + '<span class="analysis-timeline-icon">' + icon + '</span>'
    + (time ? '<span class="analysis-timeline-time">' + esc(time) + '</span>' : '')
    + '<span class="analysis-timeline-text">' + parts.text + '</span>'
    + (parts.tool ? '<span class="analysis-timeline-tool">' + esc(parts.tool) + '</span>' : '')
    + duration + '</div>';
}

// 阶段分组标题：预采集（Controller 证据预采集）与 kkagent（AI 取证阶段）
// 之间插一行分组行，长过程一眼可分辨「平台在做准备」和「模型在取证」。
var ANALYSIS_STAGE_LABELS = { preflight: 'Controller 证据预采集', kkagent: 'AI 取证分析' };

function renderAnalysisTimelineBody() {
  var state = analysisTimelineState;
  if (!state.events.length) {
    var empty = state.terminal
      ? '没有保存的过程事件（终态过程事件仅保留 ' + ANALYSIS_EVENT_RETENTION_DAYS + ' 天，超期会被清理）。'
      : (state.history ? '正在读取执行过程…' : '排队中，等待 Worker 领取任务…');
    return '<div class="analysis-timeline-queued"><span class="analysis-timeline-item '
      + (state.terminal ? 'stopped' : 'running') + '">'
      + '<span class="analysis-timeline-icon">' + (state.terminal ? '✕' : '⏳') + '</span>'
      + '<span class="analysis-timeline-text">' + esc(empty) + '</span></span></div>';
  }
  var html = '';
  var currentStage = null;
  state.events.forEach(function (event) {
    var stage = String(event.stage || '') || null;
    if (ANALYSIS_STAGE_LABELS[stage] && stage !== currentStage) {
      html += '<div class="analysis-timeline-stage">'
        + '<span>' + esc(ANALYSIS_STAGE_LABELS[stage]) + '</span></div>';
    }
    currentStage = stage;
    html += analysisTimelineEventRow(event);
  });
  return html;
}

function analysisTimelineMetaHtml(runStatus) {
  var state = analysisTimelineState;
  var started = state.events.filter(function (event) { return event.event_type === 'tool_started'; }).length;
  var done = state.events.filter(function (event) { return event.event_type === 'tool_completed'; }).length;
  var failed = state.events.filter(function (event) { return event.event_type === 'tool_failed'; }).length;
  var elapsed = Math.max(0, Math.floor((Date.now() - state.openedAt) / 1000));
  var elapsedText = elapsed >= 60
    ? Math.floor(elapsed / 60) + 'm ' + (elapsed % 60) + 's'
    : elapsed + 's';
  var live = ['pending', 'snapshotting', 'analyzing'].indexOf(runStatus) >= 0;
  return '<span>状态 ' + esc(analysisTimelineRunStateLabel(runStatus)) + '</span>'
    + '<span>工具调用 ' + started + '</span>'
    + '<span>成功 ' + done + '</span>'
    + '<span>失败 ' + failed + '</span>'
    + (live ? '<span>本次观察 ' + elapsedText + '</span>' : '');
}

function analysisTimelineSetTitle(view, statusText) {
  // 弹框标题跟随当前视图：运行态「🔵 正在分析」，过程视图「🕐 执行过程」
  // + 终态徽标，报告视图「📋 分析总结」（状态在 meta 行展示，不重复徽标）。
  var state = analysisTimelineState;
  var title = document.getElementById(state.modalId + '-title');
  if (!title) return;
  var prefix = ({ live: '🔵 正在分析 · #', timeline: '🕐 执行过程 · #',
    report: '📋 分析总结 · #' })[view] || '🕐 执行过程 · #';
  title.innerHTML = '<span>' + prefix + esc(state.issueId) + '</span>'
    + (view === 'timeline' ? '<span class="analysis-timeline-badge">已结束'
      + (statusText ? ' · ' + esc(statusText) : '') + '</span>' : '');
}

async function fetchAllAnalysisTimelineEvents() {
  // 历史回看需要完整时间线，轮询只拿增量；终态 run 的事件不可变，按
  // limit=200 翻页直到取完（events API 单页上限 200）。返回 null 表示
  // 弹框已被关闭，调用方直接退出。
  var state = analysisTimelineState;
  var after = 0;
  var events = [];
  var runStatus = '';
  var terminal = false;
  for (var page = 0; page < 25; page += 1) {
    var data = await api('/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(state.runId)
      + '/issues/' + encodeURIComponent(state.issueId)
      + '/events?after_sequence=' + after + '&limit=200') || {};
    if (!document.getElementById(state.modalId)) return null;
    var pageEvents = Array.isArray(data.events) ? data.events : [];
    events = events.concat(pageEvents);
    after = Number(data.next_sequence) || after;
    runStatus = String(data.run_status || runStatus);
    terminal = Boolean(data.terminal);
    if (pageEvents.length < 200) break;
  }
  return { events: events, runStatus: runStatus, terminal: terminal };
}

async function loadAnalysisTimelineHistory() {
  var state = analysisTimelineState;
  var modalId = state.modalId;
  if (!state.runId || !document.getElementById(modalId)) { stopAnalysisTimelinePolling(); return; }
  try {
    var result = await fetchAllAnalysisTimelineEvents();
    if (!result || !document.getElementById(modalId)) { stopAnalysisTimelinePolling(); return; }
    if (!result.terminal) {
      // 罕见竞态：历史入口打开瞬间 run 刚被重新分析置回运行态。退回实时
      // 轮询，终态后照常原地切换报告。
      state.history = false;
      analysisTimelineSetTitle('live', '');
      pollAnalysisTimeline();
      return;
    }
    state.events = result.events;
    state.runStatus = result.runStatus;
    state.terminal = true;
  } catch (error) {
    if (!document.getElementById(modalId)) { stopAnalysisTimelinePolling(); return; }
    state.terminal = true;
    // 读取失败仍进入终态视图：渲染已取到的部分；空时间线显示「未保存」文案。
  }
  analysisTimelineSetTitle('timeline', analysisTimelineRunStateLabel(state.runStatus));
  renderAnalysisTimelineBox();
  var meta = document.getElementById(modalId + '-meta');
  if (meta) meta.innerHTML = analysisTimelineMetaHtml(state.runStatus);
  setAnalysisTimelineFooterView('timeline');
}

function analysisTimelineEnsureToggleButton() {
  // 报告 ↔ 过程的唯一切换按钮：位置固定在关闭按钮左侧，只换文案，
  // 视图切换时 footer 不跳动；终态/历史收尾时按需创建（运行态无此按钮）。
  var modal = document.getElementById(analysisTimelineState.modalId);
  var footer = modal && modal.querySelector('.modal-buttons');
  if (!footer) return null;
  var toggle = footer.querySelector('[data-analysis-timeline-toggle]');
  if (!toggle) {
    toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'ka-btn';
    toggle.setAttribute('data-analysis-timeline-toggle', '1');
    var close = footer.querySelector('[data-analysis-timeline-close]');
    if (close) footer.insertBefore(toggle, close); else footer.appendChild(toggle);
  }
  return toggle;
}

function setAnalysisTimelineFooterView(view) {
  var toggle = analysisTimelineEnsureToggleButton();
  if (!toggle) return;
  toggle.dataset.view = view;
  toggle.textContent = view === 'timeline' ? '查看报告' : '查看过程';
}

async function setAnalysisTimelineView(view) {
  // 终态/历史弹框的报告 ↔ 执行过程原地切换（不重建弹框，显示控制仍归
  // ModalManager 单一所有者）。
  var state = analysisTimelineState;
  var modalId = state.modalId;
  if (!state.runId || !document.getElementById(modalId)) return;
  var box = document.getElementById(modalId + '-timeline');
  var meta = document.getElementById(modalId + '-meta');
  if (!box || !meta) return;
  if (view === 'report') {
    try {
      var payload = await api('/api/redmine-agent/daily-brief/runs/'
        + encodeURIComponent(state.runId)) || {};
      if (!document.getElementById(modalId)) return;
      var run = payload.run || {};
      var issue = (payload.issues || [])[0] || {};
      state.runStatus = String(run.status || state.runStatus || '');
      box.innerHTML = '<div class="analysis-timeline-final">'
        + analysisTimelineFinalBody(issue, run) + '</div>';
      meta.innerHTML = '<span>状态 ' + esc(analysisTimelineRunStateLabel(state.runStatus)) + '</span>';
      analysisTimelineSetTitle('report', analysisTimelineRunStateLabel(state.runStatus));
      setAnalysisTimelineFooterView('report');
    } catch (error) {
      notifyUser('报告读取失败', (error && error.message) || '', 'error');
    }
    return;
  }
  if (!state.events.length) {
    var result = await fetchAllAnalysisTimelineEvents();
    if (!document.getElementById(modalId)) { stopAnalysisTimelinePolling(); return; }
    if (result) {
      state.events = result.events;
      state.runStatus = result.runStatus || state.runStatus;
    }
    state.terminal = true;
  }
  renderAnalysisTimelineBox();
  box.scrollTop = box.scrollHeight;
  meta.innerHTML = analysisTimelineMetaHtml(state.runStatus);
  analysisTimelineSetTitle('timeline', analysisTimelineRunStateLabel(state.runStatus));
  setAnalysisTimelineFooterView('timeline');
}

function showAnalysisTimelineModal(runId, issueId, options) {
  var state = analysisTimelineState;
  stopAnalysisTimelinePolling();
  options = options || {};
  var modalId = 'analysisTimelineModal-' + Date.now();
  state.modalId = modalId;
  state.runId = String(runId || '');
  state.issueId = Number(issueId) || 0;
  state.openedAt = Date.now();
  // 历史模式：终态 run 的完整时间线回看。不轮询、无停止按钮，终态时不
  // 自动切换报告；「查看报告 / 查看过程」可互相切换。
  state.history = Boolean(options.history);
  var subject = String(options.subject || '').trim();
  var modal = document.createElement('div');
  modal.id = modalId;
  modal.className = 'modal daily-brief-analysis-overlay';
  modal.innerHTML = '<div class="modal-content daily-brief-modal">'
    + '<div class="modal-header"><span class="modal-title daily-brief-modal-title">'
    + '<span id="' + modalId + '-title"><span>'
    + (state.history ? '🕐 执行过程 · #' : '🔵 正在分析 · #') + esc(state.issueId) + '</span>'
    + (state.history ? '<span class="analysis-timeline-badge">已结束</span>' : '')
    + '</span>'
    + (subject ? '<span class="daily-brief-modal-subject">' + esc(subject) + '</span>' : '')
    + '</span><button type="button" class="modal-close" aria-label="关闭" data-click="removeDynamicModal" data-a0="' + modalId + '" data-analysis-timeline-close>&times;</button></div>'
    + '<div class="modal-body daily-brief-modal-body"><div class="analysis-timeline" id="' + modalId + '-timeline">'
    + renderAnalysisTimelineBody() + '</div></div>'
    + '<div class="modal-buttons daily-brief-modal-footer"><div class="analysis-timeline-meta" id="' + modalId + '-meta">'
    + analysisTimelineMetaHtml(state.history ? '' : 'pending') + '</div>'
    + (state.history ? '' : '<button class="ka-btn" data-analysis-timeline-stop data-a0="' + esc(state.runId) + '">停止分析</button>')
    + '<button class="secondary" data-click="removeDynamicModal" data-a0="' + modalId + '" data-analysis-timeline-close>关闭</button></div></div>';
  showDailyBriefAnalysisModal(modal);
  if (state.history) loadAnalysisTimelineHistory(); else pollAnalysisTimeline();
}

function renderAnalysisTimelineBox() {
  var state = analysisTimelineState;
  var box = document.getElementById(state.modalId + '-timeline');
  if (!box) return;
  var stick = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.innerHTML = renderAnalysisTimelineBody();
  if (stick) box.scrollTop = box.scrollHeight;
}

async function pollAnalysisTimeline() {
  var state = analysisTimelineState;
  if (!state.runId || !document.getElementById(state.modalId)) {
    stopAnalysisTimelinePolling();
    return;
  }
  try {
    state.controller = new AbortController();
    var data = await api('/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(state.runId)
      + '/issues/' + encodeURIComponent(state.issueId)
      + '/events?after_sequence=' + state.after + '&limit=100', { signal: state.controller.signal }) || {};
    if (!document.getElementById(state.modalId)) { stopAnalysisTimelinePolling(); return; }
    if (Array.isArray(data.events) && data.events.length) {
      state.events = state.events.concat(data.events).slice(-400);
      state.after = Number(data.next_sequence) || state.after;
      renderAnalysisTimelineBox();
    }
    var meta = document.getElementById(state.modalId + '-meta');
    if (meta) meta.innerHTML = analysisTimelineMetaHtml(String(data.run_status || ''));
    if (data.terminal) {
      await finishAnalysisTimeline();
      return;
    }
  } catch (error) {
    if (!document.getElementById(state.modalId)) { stopAnalysisTimelinePolling(); return; }
    if (error && (error.name === 'AbortError' || error.status === 404)) {
      // 弹框已关（abort）或 run 永久不可达（不存在/无权限/已清理）：停止
      // 轮询，不做无意义重试；不可达时把弹框收敛为确定的结束提示，不再
      // 停留在「正在分析」、也不残留必失败的「停止分析」按钮。
      if (error.status === 404 && document.getElementById(state.modalId)) {
        var missedBox = document.getElementById(state.modalId + '-timeline');
        if (missedBox) missedBox.innerHTML = '<div class="analysis-timeline-queued"><span class="analysis-timeline-item stopped">'
          + '<span class="analysis-timeline-icon">✕</span>'
          + '<span class="analysis-timeline-text">分析记录不存在或已被清理，无法继续跟踪。</span></span></div>';
        analysisTimelineMarkEnded('记录不存在');
      }
      stopAnalysisTimelinePolling();
      return;
    }
    // 网络瞬断：继续下一轮。
  }
  var interval = document.hidden ? 12000 : 2500;
  analysisTimelineState.timer = setTimeout(pollAnalysisTimeline, interval);
}

function analysisTimelineFinalBody(issue, run) {
  var banner = '';
  var status = String((issue || {}).status || run.status || '');
  if (status === 'failed') {
    banner = '<div style="color:var(--bad,#ef4444)"><b>分析失败'
      + (issue.error_type ? '（' + esc(issue.error_type) + '）' : '') + '</b>'
      + '<div style="white-space:pre-wrap;margin-top:4px">' + esc(issue.error || run.error || '未知错误') + '</div></div>';
  } else if (status === 'cancelled' || run.status === 'cancelled') {
    banner = '<div class="daily-brief-warning">分析已停止。已完成的结果会保留，可稍后重新分析。</div>';
  }
  var body = '';
  if (String(((issue || {}).result || {}).detailed_report || '').trim()) {
    body = singleIssueAnalysisReportBody({ run: run, issues: [issue] });
  }
  return (banner || '<div class="muted">本次分析未生成报告。</div>') + body;
}

async function finishAnalysisTimeline() {
  var state = analysisTimelineState;
  if (!state.runId || !document.getElementById(state.modalId)) { stopAnalysisTimelinePolling(); return; }
  var modalId = state.modalId;
  try {
    var payload = await api('/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(state.runId)) || {};
    if (!document.getElementById(modalId)) { stopAnalysisTimelinePolling(); return; }
    var run = payload.run || {};
    var issue = (payload.issues || [])[0] || {};
    // 独立单号分析：把终态写回列表缓存；晨报 run 交给整卡刷新。
    if (String(run.mode || '').indexOf('issue:') === 0) {
      upsertSingleIssueAnalysis(payload);
      renderSingleIssueAnalysisHistory();
    } else {
      loadDailyBrief();
    }
    state.runStatus = String(run.status || state.runStatus || '');
    analysisTimelineSetTitle('report', analysisTimelineRunStateLabel(state.runStatus));
    var box = document.getElementById(modalId + '-timeline');
    if (box) box.innerHTML = '<div class="analysis-timeline-final">' + analysisTimelineFinalBody(issue, run) + '</div>';
    var meta = document.getElementById(modalId + '-meta');
    if (meta) meta.innerHTML = '<span>状态 ' + esc(analysisTimelineRunStateLabel(run.status)) + '</span>';
    setAnalysisTimelineFooterView('report');
    var stopButton = document.querySelector('[data-analysis-timeline-stop][data-a0="' + state.runId + '"]');
    if (stopButton) stopButton.remove();
  } catch (_) {
    // 终态详情拉取失败：不丢弃已渲染的时间线，就地收敛为确定的结束
    // 视图并保留「查看报告」重试入口，避免标题停留在「正在分析」、
    // 停止按钮残留（列表轮询仍会自然收敛行状态）。
    var finalBox = document.getElementById(modalId + '-timeline');
    if (finalBox) finalBox.insertAdjacentHTML('afterbegin',
      '<div class="daily-brief-warning">分析已结束，报告详情读取失败，可点击「查看报告」重试。</div>');
    analysisTimelineMarkEnded('报告读取失败');
  }
  haltAnalysisTimelineLoop();
}

async function stopAnalysisTimelineRun(runId, button) {
  if (!runId || !button || button.disabled) return;
  button.disabled = true;
  button.textContent = '停止中…';
  try {
    await api('/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(runId) + '/cancel', { method: 'POST' });
    // 不退出轮询：时间线会随后续事件/run 终态收敛为「已停止」。
  } catch (error) {
    button.disabled = false;
    button.textContent = '停止分析';
    notifyUser('停止失败', error.message, 'error');
  }
}

document.addEventListener('click', function (event) {
  var stop = event.target.closest('[data-analysis-timeline-stop]');
  if (stop) stopAnalysisTimelineRun(stop.dataset.a0, stop);
  // 报告 ↔ 执行过程单按钮切换：点按钮即去另一个视图，按钮自身不移动。
  var toggle = event.target.closest('[data-analysis-timeline-toggle]');
  if (toggle) setAnalysisTimelineView(toggle.dataset.view === 'timeline' ? 'report' : 'timeline');
});

async function loadSingleIssueAnalysisHistory() {
  try {
    var data = await api('/api/redmine-agent/daily-brief/issue-analyses?limit=30') || {};
    var items = Array.isArray(data.items) ? data.items : [];
    items.slice().reverse().forEach(upsertSingleIssueAnalysis);
    renderSingleIssueAnalysisHistory();
  } catch (_) {
    // History must not prevent the current analysis or morning brief from loading.
  }
}

async function loadSingleIssueAnalysis() {
  var runId = singleIssueAnalysisRunId;
  if (!runId) return;
  clearTimeout(singleIssueAnalysisTimer);
  try {
    var data = await api('/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(runId)) || {};
    if (singleIssueAnalysisRunId !== runId) return;
    var run = data.run || {};
    var issue = (data.issues || [])[0] || {};
    upsertSingleIssueAnalysis(data);
    renderSingleIssueAnalysisHistory();
    var busy = ['pending', 'snapshotting', 'analyzing'].indexOf(run.status) >= 0;
    setSingleIssueAnalysisBusy(busy);
    if (busy) {
      singleIssueAnalysisTimer = setTimeout(loadSingleIssueAnalysis, 5000);
    }
  } catch (e) {
    if (singleIssueAnalysisRunId !== runId) return;
    if (e && e.status === 404) {
      // A saved run may have been removed or become inaccessible after an
      // ownership change.  It is permanent for this browser session, not a
      // transient network error, so do not retry it forever.
      rememberSingleIssueAnalysisRun('');
      setSingleIssueAnalysisBusy(false);
      return;
    }
    singleIssueAnalysisTimer = setTimeout(loadSingleIssueAnalysis, 5000);
  }
}

async function restoreSingleIssueAnalysis() {
  if (singleIssueAnalysisRunId) {
    await loadSingleIssueAnalysis();
    return;
  }
  // Prior releases persisted completed run IDs.  A run is owner-scoped and
  // can legitimately disappear, so use the server's active-run query as the
  // sole cross-reload source of truth and discard old browser state.
  try { sessionStorage.removeItem('gms-redmine-single-issue-run-id'); } catch (_) {}
  try {
    var data = await api('/api/redmine-agent/daily-brief/active-issue') || {};
    var run = data.run || {};
    if (!run.run_id) return;
    rememberSingleIssueAnalysisRun(run.run_id);
    var issue = (data.issues || [])[0] || {};
    await loadSingleIssueAnalysis();
  } catch (_) {}
}

async function analyzeSingleIssueFromInput(skipHistoryLookup) {
  var input = document.getElementById('singleIssueAnalysisId');
  var value = input.value.trim();
  if (!/^[0-9]+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) <= 0) {
    notifyUser('单号无效', '请输入正整数 Redmine 单号。', 'warning');
    return;
  }
  // A click on “开始分析” is an explicit re-analysis request.  It still
  // checks remote history first so older records receive the default
  // incremental mode; Enter handles lookup before it reaches this path.
  if (!skipHistoryLookup) await lookupSingleIssueHistory(value);
  document.getElementById('singleIssueAnalysisStart').disabled = true;
  try {
    var modeSelect = document.getElementById('singleIssueAnalysisMode');
    var hasHistory = singleIssueAnalysisHistory.some(function (item) {
      var issue = (item && item.issues || [])[0] || {};
      return String(issue.issue_id) === value;
    });
    var analysisMode = hasHistory && modeSelect && modeSelect.value === 'incremental' ? 'incremental' : 'full';
    var deviceSerials = selectedSingleIssueDevices();
    var analysisHint = singleIssueAnalysisHint.trim();
    var payload = {issue_id: Number(value)};
    if (analysisMode === 'full') payload.analysis_mode = analysisMode;
    if (deviceSerials.length) payload.device_serials = deviceSerials;
    if (analysisHint) payload.analysis_hint = analysisHint;
    var queued = await api('/api/redmine-agent/daily-brief/analyze-issue', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }) || {};
    if (!queued.run_id) throw new Error(queued.error || '未创建分析任务');
    rememberSingleIssueAnalysisRun(queued.run_id);
    resetSingleIssueAnalysisForm();
    await loadSingleIssueAnalysis();
  } catch (e) {
    setSingleIssueAnalysisBusy(false);
    notifyUser('提交分析失败', e.message, 'error');
  }
}

async function reanalyzeSavedSingleIssue(runId, issueId) {
  var button = document.querySelector('[data-single-issue-reanalyze="' + String(runId).replace(/"/g, '\\"') + '"]');
  var modeSelect = button && button.parentElement.querySelector('[data-single-issue-reanalysis-mode]');
  var analysisMode = String((modeSelect && modeSelect.value) || 'incremental');
  var deviceSerials = selectedSingleIssueDevices();
  var analysisHint = singleIssueAnalysisHint.trim();
  var originalText = button && button.textContent;
  if (button) { button.disabled = true; button.textContent = '⏳ 分析中'; }
  try {
    var payload = {issue_id: Number(issueId), analysis_mode: analysisMode};
    if (deviceSerials.length) payload.device_serials = deviceSerials;
    if (analysisHint) payload.analysis_hint = analysisHint;
    var queued = await api('/api/redmine-agent/daily-brief/analyze-issue', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    }) || {};
    if (!queued.run_id) throw new Error(queued.error || '未创建增量分析任务');
    // 条目级重分析与顶部的“开始分析”彼此独立：只更新和轮询当前行，
    // 不能把顶部控件切成运行/停止状态。
    upsertSingleIssueAnalysis({
      run: {run_id: queued.run_id, status: queued.status || 'pending', device_serial: deviceSerials.join(',')},
      issues: [{issue_id: Number(issueId), status: 'pending'}],
    });
    renderSingleIssueAnalysisHistory();
    loadSingleIssueHistoryRun(queued.run_id);
  } catch (error) {
    if (button) { button.disabled = false; button.textContent = originalText; }
    notifyUser('增量分析失败', error.message, 'error');
  }
}

async function loadSingleIssueHistoryRun(runId) {
  var id = String(runId || '');
  if (!id) return;
  clearTimeout(singleIssueHistoryRunTimers[id]);
  try {
    var data = await api('/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(id)) || {};
    var run = data.run || {};
    var issue = (data.issues || [])[0] || {};
    if (!run.run_id) return;
    upsertSingleIssueAnalysis(data);
    renderSingleIssueAnalysisHistory();
    if (singleIssueAnalysisIsRunning(run, issue)) {
      singleIssueHistoryRunTimers[id] = setTimeout(function () {
        loadSingleIssueHistoryRun(id);
      }, 5000);
    } else {
      delete singleIssueStopRequested[id];
      delete singleIssueHistoryRunTimers[id];
      renderSingleIssueAnalysisHistory();
    }
  } catch (_) {
    singleIssueHistoryRunTimers[id] = setTimeout(function () {
      loadSingleIssueHistoryRun(id);
    }, 5000);
  }
}

async function stopSavedSingleIssueAnalysis(runId, button) {
  var id = String(runId || '');
  if (!id || singleIssueStopRequested[id]) return;
  singleIssueStopRequested[id] = true;
  // Keep the pressed element stable through the native pointer/click cycle.
  // A polling repaint between mousedown and click otherwise disconnects the
  // dynamic button and makes a click appear to do nothing.
  if (button) {
    button.disabled = true;
    button.textContent = '停止中…';
  }
  requestAnimationFrame(renderSingleIssueAnalysisHistory);
  try {
    await api('/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(id) + '/cancel', {method: 'POST'});
    await loadSingleIssueHistoryRun(id);
  } catch (error) {
    delete singleIssueStopRequested[id];
    renderSingleIssueAnalysisHistory();
    notifyUser('停止失败', error.message, 'error');
  }
}

document.addEventListener('pointerdown', function (event) {
  var cancel = event.target.closest('[data-single-issue-cancel]');
  if (!cancel || cancel.disabled) return;
  // Start cancellation before a concurrent 5-second poll can replace this
  // row. The click listener below is retained as keyboard/fallback support.
  stopSavedSingleIssueAnalysis(cancel.dataset.singleIssueCancel, cancel);
});

document.addEventListener('click', function (event) {
  var cancel = event.target.closest('[data-single-issue-cancel]');
  if (cancel) {
    stopSavedSingleIssueAnalysis(cancel.dataset.singleIssueCancel, cancel);
    return;
  }
  var reanalyze = event.target.closest('[data-single-issue-reanalyze]');
  if (reanalyze) {
    reanalyzeSavedSingleIssue(reanalyze.dataset.singleIssueReanalyze, reanalyze.dataset.singleIssueId);
    return;
  }
  var view = event.target.closest('[data-single-issue-view]');
  if (view) {
    // 运行中的分析打开实时进度时间线（终态后原地切换报告，可再切回过程）；
    // 历史条目仍默认静态报告弹框，30 天内可在其中经「执行过程」回看时间线。
    var item = findSingleIssueAnalysis(view.dataset.singleIssueView);
    var run = (item || {}).run || {};
    var issue = ((item || {}).issues || [])[0] || {};
    if (item && singleIssueAnalysisIsRunning(run, issue)) {
      showAnalysisTimelineModal(run.run_id, issue.issue_id, { subject: issue.subject });
    } else {
      showSingleIssueAnalysis(view.dataset.singleIssueView, false);
    }
    return;
  }
  var statistics = event.target.closest('[data-single-issue-statistics]');
  if (statistics) { showSingleIssueAnalysis(statistics.dataset.singleIssueStatistics, true); return; }
  var page = event.target.closest('[data-single-issue-page]');
  if (!page || page.disabled) return;
  singleIssueAnalysisPage = Number(page.dataset.singleIssuePage) || 1;
  renderSingleIssueAnalysisHistory();
});

document.addEventListener('input', function (event) {
  if (event.target.id !== 'singleIssueAnalysisId') return;
  // 只定位不过滤：不重绘列表；纯数字输入按单号翻页/滚动/高亮，
  // 未命中时历史列表保持原样。
  clearTimeout(singleIssueHistoryLookupTimer);
  var value = String(event.target.value || '').trim();
  if (!/^[0-9]+$/.test(value)) return;
  singleIssueHistoryLookupTimer = setTimeout(function () {
    if (value !== String((document.getElementById('singleIssueAnalysisId') || {}).value || '').trim()) return;
    if (value === singleIssueHistoryLookupValue && findSingleIssueAnalysisByIssueId(value)) return;
    singleIssueHistoryLookupValue = value;
    lookupSingleIssueHistory(value);
  }, 250);
});

function findSingleIssueAnalysisByIssueId(issueId) {
  return singleIssueAnalysisHistory.find(function (item) {
    var issue = ((item || {}).issues || [])[0] || {};
    return String(issue.issue_id) === String(issueId);
  }) || null;
}

function selectedSingleIssueDevices() {
  var picker = document.getElementById('singleIssueAnalysisDevice');
  return Array.from((picker && picker.querySelectorAll('input[data-device-serial]:checked')) || [])
    .map(function (input) { return String(input.value || '').trim(); }).filter(Boolean);
}

function syncSingleIssueDevicePickerLabel() {
  var picker = document.getElementById('singleIssueAnalysisDevice');
  var label = picker && picker.querySelector('[data-device-label]');
  if (!label) return;
  var devices = selectedSingleIssueDevices();
  label.textContent = devices.length ? (devices.length === 1 ? devices[0] : '已选 ' + devices.length + ' 台设备') : '不使用实机验证';
}

function setSingleIssueDevicePickerOpen(open) {
  var picker = document.getElementById('singleIssueAnalysisDevice');
  if (!picker) return;
  var toggle = picker.querySelector('[data-device-toggle]');
  var options = picker.querySelector('[data-device-options]');
  if (!toggle || !options) return;
  options.hidden = !open;
  toggle.setAttribute('aria-expanded', String(open));
  var card = picker.closest('.daily-brief-card');
  if (card) card.toggleAttribute('data-device-picker-open', open);
}

async function loadSingleIssueDevices() {
  if (singleIssueDevicesLoading) return singleIssueDevicesLoading;
  var picker = document.getElementById('singleIssueAnalysisDevice');
  if (!picker) return;
  singleIssueDevicesLoading = (async function () {
    var oldValues = selectedSingleIssueDevices();
    picker.setAttribute('aria-busy', 'true');
    try {
      var response = await fetch('/api/devices/list?force_refresh=true', {credentials: 'same-origin'});
      if (!response.ok) throw new Error('设备列表读取失败（HTTP ' + response.status + '）');
      var devices = await response.json();
      if (!Array.isArray(devices)) throw new Error('设备列表格式不正确');
      var options = devices.filter(function (device) {
        return device && device.protocol === 'adb' && device.status === 'online'
          && (!device.locked || device.locked_by_self);
      });
      var listRoot = picker.querySelector('[data-device-list]');
      if (!listRoot) return;
      listRoot.replaceChildren();
      options.forEach(function (device) {
        var label = document.createElement('label');
        var option = document.createElement('input');
        option.type = 'checkbox';
        option.dataset.deviceSerial = 'true';
        option.value = String(device.device_id || '');
        option.checked = oldValues.indexOf(option.value) >= 0;
        label.append(option, document.createTextNode(' ' + option.value + (device.transport ? ' · ' + device.transport : '')));
        listRoot.appendChild(label);
      });
      var none = picker.querySelector('[data-device-none]');
      if (none) none.checked = oldValues.length === 0;
    } catch (error) {
      notifyUser('读取 ADB 设备失败', error.message, 'warning');
    } finally {
      picker.removeAttribute('aria-busy');
      singleIssueDevicesLoading = null;
    }
  })();
  return singleIssueDevicesLoading;
}
window.loadSingleIssueDevices = loadSingleIssueDevices;

document.addEventListener('change', function (event) {
  var picker = event.target.closest && event.target.closest('#singleIssueAnalysisDevice');
  if (!picker) return;
  var none = picker.querySelector('[data-device-none]');
  if (event.target === none && none.checked) {
    picker.querySelectorAll('input[data-device-serial]').forEach(function (input) { input.checked = false; });
  } else if (event.target.matches && event.target.matches('input[data-device-serial]')) {
    if (none) none.checked = false;
  }
  syncSingleIssueDevicePickerLabel();
});

document.addEventListener('click', function (event) {
  var picker = document.getElementById('singleIssueAnalysisDevice');
  if (!picker) return;
  var toggle = event.target.closest && event.target.closest('[data-device-toggle]');
  if (toggle && picker.contains(toggle)) {
    var options = picker.querySelector('[data-device-options]');
    var open = !!(options && options.hidden);
    setSingleIssueDevicePickerOpen(open);
    if (open) loadSingleIssueDevices();
    return;
  }
  if (!picker.contains(event.target)) setSingleIssueDevicePickerOpen(false);
});

document.addEventListener('keydown', function (event) {
  if (event.key === 'Escape') setSingleIssueDevicePickerOpen(false);
});

if (!window.singleIssueAnalysisSubmitBound) {
  window.singleIssueAnalysisSubmitBound = true;
  document.addEventListener('keydown', function (event) {
    if (event.target.id !== 'singleIssueAnalysisId' || event.key !== 'Enter') return;
    event.preventDefault();
    if (document.getElementById('singleIssueAnalysisStart').disabled) return;
    // Enter in the unified search box means “find first”. The only case that
    // starts a task is a confirmed absence of a saved analysis.
    (async function () {
      var value = String(event.target.value || '').trim();
      if (await lookupSingleIssueHistory(value)) return;
      analyzeSingleIssueFromInput(true);
    })();
  });
  document.addEventListener('submit', function (event) {
    if (event.target.id !== 'singleIssueAnalysisForm') return;
    event.preventDefault();
    if (document.getElementById('singleIssueAnalysisStart').disabled) return;
    analyzeSingleIssueFromInput();
  });
}

function dailyBriefSetting(name) {
  return document.querySelector('[data-daily-brief-setting="' + name + '"]');
}

function appendDailyBriefModelOption(select, value, label) {
  var option = document.createElement('option');
  option.value = String(value || '');
  option.textContent = String(label || value || '');
  select.appendChild(option);
}

async function loadDailyBriefAgentProfiles(selectedProfile) {
  var select = dailyBriefSetting('agent_profile');
  if (!select) return;
  var selected = String(selectedProfile || '').trim();
  select.replaceChildren();
  try {
    var payload = await api('/api/redmine-agent/daily-brief/agent-profiles') || {};
    var profiles = Array.isArray(payload.profiles) ? payload.profiles : [];
    var known = {};
    if (profiles.length > 1 && !selected) {
      appendDailyBriefModelOption(select, '', '请选择 kkagent Profile');
    }
    profiles.forEach(function(item) {
      var profile = String(item || '').trim();
      if (!profile || known[profile]) return;
      known[profile] = true;
      appendDailyBriefModelOption(select, profile, profile);
    });
    if (!profiles.length) {
      appendDailyBriefModelOption(select, '', '未发现本机 kkagent Profile');
    } else if (selected && !known[selected]) {
      appendDailyBriefModelOption(select, selected, '当前保存的 Profile：' + selected);
    }
    select.value = selected || String(payload.default_profile || '').trim();
  } catch (_) {
    appendDailyBriefModelOption(select, selected, selected || 'Profile 列表读取失败');
    select.value = selected;
  }
}

async function loadDailyBriefModelOptions(selectedModel) {
  var select = dailyBriefSetting('model');
  if (!select) return;
  var selected = String(selectedModel || '').trim();
  select.replaceChildren();
  try {
    var payload = await api('/api/redmine-agent/daily-brief/model-options') || {};
    var models = Array.isArray(payload.models) ? payload.models : [];
    var known = {};
    models.forEach(function(item) {
      var model = String((item || {}).model || '').trim();
      if (!model || known[model]) return;
      known[model] = true;
      var display = String((item || {}).display_name || model).trim() || model;
      var provider = String((item || {}).provider || '').trim();
      appendDailyBriefModelOption(select, model, provider ? display + '（' + provider + '）' : display);
    });
    if (!models.length) {
      appendDailyBriefModelOption(select, '', '未发现已启用的系统模型（使用 kkagent 默认模型）');
    } else if (selected && !known[selected]) {
      appendDailyBriefModelOption(select, selected, '当前保存的模型：' + selected);
    }
    select.value = selected || String(payload.default_model || '').trim();
    if (!select.value && select.options.length) select.selectedIndex = 0;
  } catch (_) {
    appendDailyBriefModelOption(select, selected, selected || '模型列表读取失败（使用 kkagent 默认模型）');
    select.value = selected;
  }
}

async function loadDailyBrief() {
  var card = document.getElementById('dailyBriefCard');
  if (!card) return;
  try {
    var data = await api('/api/redmine-agent/daily-brief/latest');
    if ((!data || data.account_available !== false) && !dailyBriefConfigCache) {
      try { dailyBriefConfigCache = await api('/api/redmine-agent/daily-brief/config') || {}; } catch (_) {}
    }
    dailyBriefCache = (data && data.success !== false) ? data : null;
    card.innerHTML = renderDailyBriefInner(dailyBriefCache);
    updateRedmineToolbar();
    scheduleDailyBriefAutoRefresh(dailyBriefCache);
    await loadSingleIssueAnalysisHistory();
    await restoreSingleIssueAnalysis();
  } catch (e) {
    card.innerHTML = renderDailyBriefInner(null);
    updateRedmineToolbar();
  }
}

// 每日晨报 run 进行中时自动轮询 latest，完成后停；手动触发已有 pollDailyBriefRun，此处兜底刷新。
var dailyBriefAutoPollTimer = null;
var dailyBriefRunStarting = false;   // 点击「重新分析」后到 POST 返回前的过渡态
var dailyBriefStoppingRunId = '';    // 取消请求已提交，等待 Worker 终止 KkAgent
var dailyBriefManualPoll = false;    // 手动触发后的轮询进行中标记
function scheduleDailyBriefAutoRefresh(data) {
  var run = (data && data.run) || {};
  var inflight = run.status === 'pending' || run.status === 'snapshotting' || run.status === 'analyzing';
  if (!inflight || dailyBriefManualPoll) return; // 手动轮询已覆盖，不重复刷
  if (dailyBriefAutoPollTimer) return; // 已有轮询在跑
  dailyBriefAutoPollTimer = setTimeout(async function () {
    dailyBriefAutoPollTimer = null;
    if (currentTab !== 'daily-brief') return; // 离开每日晨报页后不再刷
    await loadDailyBrief();
  }, 10000);
}

function dailyBriefMetricNumber(value) {
  var number = Number(value || 0);
  return Number.isFinite(number) ? number.toLocaleString('zh-CN') : '0';
}

function dailyBriefDuration(value) {
  var milliseconds = Number(value || 0);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return '—';
  if (milliseconds < 1000) return Math.round(milliseconds) + ' ms';
  var seconds = Math.round(milliseconds / 1000);
  if (seconds < 60) return seconds.toLocaleString('zh-CN') + ' 秒';
  var minutes = Math.floor(seconds / 60);
  return minutes.toLocaleString('zh-CN') + ' 分 ' + (seconds % 60) + ' 秒';
}

// 每日晨报行的 meta 标签：与「Redmine 单号分析」行同构（分析时间 / 实机
// 取证状态）。晨报按天聚合，附上分析时间便于区分增量重跑与隔天批次。
function dailyBriefIssueMetaTags(issue) {
  var tags = [];
  var analyzedAt = String(issue.finished_at || issue.started_at || '').replace('T', ' ').slice(0, 16);
  if (analyzedAt) tags.push('<span>分析时间 ' + esc(analyzedAt) + '</span>');
  var evidenceTag = dailyBriefEvidenceTag(dailyBriefObject(issue.ai_execution).device_evidence_status);
  if (evidenceTag) tags.push(evidenceTag);
  return tags.join('');
}

function renderDailyBriefIssueStatistics(statistics) {
  var stats = dailyBriefObject(statistics);
  if (!Number(stats.execution_count || 0)) return '';
  var tokens = dailyBriefObject(stats.tokens);
  var timing = dailyBriefObject(stats.timing);
  var models = dailyBriefList(stats.models);
  var tools = dailyBriefList(stats.gms_tools);
  var recommendations = dailyBriefList(stats.tool_improvement_recommendations);
  var modelRows = models.map(function (model) {
    var item = dailyBriefObject(model);
    return '<tr><td>' + esc(item.model_name || '—') + '</td><td>'
      + esc(dailyBriefMetricNumber(item.execution_count)) + '</td><td>'
      + esc(dailyBriefMetricNumber(item.total_tokens)) + '</td><td>'
      + esc(dailyBriefMetricNumber(item.input_tokens)) + '</td><td>'
      + esc(dailyBriefMetricNumber(item.output_tokens)) + '</td></tr>';
  }).join('');
  var toolRows = tools.map(function (tool) {
    var item = dailyBriefObject(tool);
    var failure = Number(item.failed_count || 0);
    return '<tr><td>' + esc(item.tool_name || '—') + '</td><td>'
      + esc(dailyBriefMetricNumber(item.call_count)) + '</td><td>'
      + esc(dailyBriefMetricNumber(item.succeeded_count)) + '</td><td class="'
      + (failure ? 'daily-brief-tool-failed' : '') + '">' + esc(dailyBriefMetricNumber(failure))
      + '</td></tr>';
  }).join('');
  var recommendationRows = recommendations.length
    ? '<ul class="daily-brief-tool-recommendations">' + recommendations.map(function (entry) {
      var item = dailyBriefObject(entry);
      return '<li><code>' + esc(item.tool_name || 'gms_rt_*') + '</code>：' + esc(item.message || '') + '</li>';
    }).join('') + '</ul>'
    : '<div class="muted">本单号未观察到需要优先处理的工具可靠性或重复调用问题。</div>';
  var value = function (label, amount) {
    return '<div class="daily-brief-stat-value"><span>' + esc(label) + '</span><b>'
      + esc(amount) + '</b></div>';
  };
  return '<div class="daily-brief-ai-statistics"><div class="daily-brief-stat-overview">'
    + '<section class="daily-brief-stat-summary"><h4>分析概览</h4><div class="daily-brief-stat-values">'
    + value('分析尝试', dailyBriefMetricNumber(stats.execution_count))
    + value('总耗时', dailyBriefDuration(timing.total_duration_ms))
    + value('平均耗时', dailyBriefDuration(timing.average_duration_ms))
    + '</div></section><section class="daily-brief-stat-summary"><h4>Token 用量</h4><div class="daily-brief-stat-values">'
    + value('总 Tokens', dailyBriefMetricNumber(tokens.total_tokens))
    + value('输入 Tokens', dailyBriefMetricNumber(tokens.input_tokens))
    + value('输出 Tokens', dailyBriefMetricNumber(tokens.output_tokens))
    + '</div></section></div><div class="daily-brief-stat-grid"><section><h4>模型用量</h4><table class="daily-brief-stat-table"><thead><tr><th>模型</th><th>尝试</th><th>总计</th><th>输入</th><th>输出</th></tr></thead><tbody>'
    + (modelRows || '<tr><td colspan="5" class="muted">暂无模型轨迹</td></tr>')
    + '</tbody></table></section><section><h4>gms-remote-test 工具 <span>调用 ' + esc(dailyBriefMetricNumber(stats.gms_tool_call_count)) + ' 次</span></h4><table class="daily-brief-stat-table"><thead><tr><th>工具</th><th>调用</th><th>成功</th><th>失败</th></tr></thead><tbody>'
    + (toolRows || '<tr><td colspan="4" class="muted">本次未调用 gms-remote-test 工具</td></tr>')
    + '</tbody></table></section></div><section class="daily-brief-tool-improvements"><h4>工具改进建议 <span>基于本单号的失败率和重复调用量</span></h4>'
    + recommendationRows + '</section></div>';
}

function renderDailyBriefInner(data) {
  var controls = document.getElementById('dailyBriefRunControls');
  if (data && data.account_available === false) {
    if (controls) controls.innerHTML = '<span class="daily-brief-status unavailable">当前账号不可用</span>';
    return '<div class="daily-brief-warning daily-brief-error"><span>⚠️ '
      + esc(data.message || '当前账号不可用于每日晨报，请切换账号后重试。') + '</span></div>';
  }
  if (!data || !data.run) {
    if (controls) controls.innerHTML = '<span class="muted">暂无每日晨报</span>'
      + '<button class="ka-btn" data-click="startDailyBriefRun">▶ 生成每日晨报</button>';
    return '';
  }
  var run = data.run || {};
  var issues = data.issues || [];
  var counts = (run.report_json && run.report_json.counts) || {};
  var profileMissing = Boolean(dailyBriefConfigCache && !dailyBriefConfigCache.agent_profile);
  var profileRequiredError = profileMissing
    && /(?:未绑定.*agent[ _]profile|agent_profile)/i.test(String(run.error || ''));
  var statusText = ({ completed: '完成', partial: '部分完成', failed: '失败', cancelled: '已停止', analyzing: '分析中…', pending: '排队中', snapshotting: '生成快照中…' })[run.status] || run.status;
  var statusBadge = '<span class="daily-brief-status ' + esc(run.status) + '">' + esc(statusText) + '</span>';
  var inflight = (run.status === 'pending' || run.status === 'snapshotting' || run.status === 'analyzing');
  var stopping = inflight && dailyBriefStoppingRunId === String(run.run_id || run.brief_date || '');
  if (!inflight && dailyBriefStoppingRunId === String(run.run_id || run.brief_date || '')) {
    dailyBriefStoppingRunId = '';
  }
  var actionBtn;
  if (dailyBriefRunStarting) {
    actionBtn = '<span class="daily-brief-action"><button class="ka-btn" disabled>⏳ 启动中…</button></span>';
  } else if (inflight) {
    var liveLabel = ({ pending: '排队中…', snapshotting: '生成快照中…', analyzing: '分析中…' })[run.status] || '处理中…';
    actionBtn = '<span class="daily-brief-action"><button class="ka-btn" disabled>⏳ ' + esc(liveLabel) + '</button>'
      + (stopping
        ? '<button class="ka-btn" disabled>⏳ 停止中…</button>'
        : '<button class="ka-btn" data-click="stopDailyBriefRun">■ 停止分析</button>')
      + '</span>';
  } else if (run.status === 'completed' || run.status === 'partial' || run.status === 'failed' || run.status === 'cancelled') {
    actionBtn = '<span class="daily-brief-action"><button class="ka-btn" data-click="startDailyBriefRun">'
      + (run.status === 'failed' || run.status === 'cancelled' ? '重试全部分析' : '重新分析全部') + '</button></span>';
  } else {
    actionBtn = '';
  }
  if (controls) controls.innerHTML = statusBadge
    + actionBtn;
  var head = '<div class="daily-brief-head">'
    + '<div class="daily-brief-metrics">'
    + '<div class="daily-brief-metric"><span>今日待处理</span><b>' + esc(counts.total != null ? counts.total : run.issue_count || 0) + '</b></div>'
    + '<div class="daily-brief-metric"><span>待回复</span><b>' + esc(run.waiting_my_reply_count || 0) + '</b></div>'
    + '<div class="daily-brief-metric"><span>超 3 天未回复</span><b>' + esc(run.no_reply_3_days_count || 0) + '</b></div>'
    + '<div class="daily-brief-metric"><span>需人工确认</span><b>' + esc(counts.needs_human_review || 0) + '</b></div>'
    + '</div></div>';
  if (run.status === 'failed' && run.error) {
    head += '<div class="daily-brief-warning daily-brief-error"><span>❌ ' + esc(run.error) + '</span>'
      + (profileRequiredError ? '<button class="ka-btn" data-click="showSettingsModal">立即设置</button>' : '')
      + '</div>';
  }
  if (profileMissing && !profileRequiredError) {
    head += '<div class="daily-brief-warning"><span>⚠️ 每日晨报必须先绑定取证 Agent Profile，才能完成 Redmine 只读取证与分析。</span>'
      + '<button class="ka-btn" data-click="showSettingsModal">立即设置</button></div>';
  }
  if (!issues.length) return head;
  var issueStateHtml = function (issue) {
    var r = issue.result || {};
    if (issue.status === 'running') return '<span class="daily-brief-state">⏳ 分析中…</span>';
    if (issue.status === 'pending') return '<span class="daily-brief-state">⏳ 排队中</span>';
    if (issue.status === 'failed') {
      var etype = String(issue.error_type || '').trim();
      var err = String(issue.error || '').trim();
      var errorLabel = ({ schema_mismatch: '返回格式不兼容', invalid_ai_output: 'AI 返回无法解析', evidence_gate_failed: '证据门禁未通过', kkagent_error: '分析服务异常', interrupted: '分析进程被中断', timeout: '分析超时', llm_timeout: '模型服务超时', kkagent_unavailable: '分析服务不可用', oversized_output: '输出超限被终止', repair_failed: '同会话自动修复失败', max_turns: '步数预算耗尽' })[etype] || etype;
      return '<span class="daily-brief-state failed" title="' + esc(err) + '">❌ 分析失败'
        + (errorLabel ? ' · ' + esc(errorLabel) : '') + '</span>';
    }
    // run 级取消会把条目收敛为 cancelled（服务端双重保证），行内给出与
    // 单号分析列表一致的终态标签；stale 为预留状态，防御性标注。
    if (issue.status === 'cancelled') return '<span class="daily-brief-state">■ 已停止</span>';
    if (issue.status === 'stale') return '<span class="daily-brief-state">⚠ 旧批次结论，可能已被更新分析取代</span>';
    // 模型自评与实际取证完成情况分别展示。
    var labels = [];
    if (r.confidence != null) labels.push('模型自评 ' + esc(r.confidence));
    if (r.needs_human_review) labels.push('⚠️ 取证未闭环 · 需人工确认');
    return '<span class="daily-brief-state">' + labels.join(' · ') + '</span>';
  };
  var rows = issues.slice().sort(function (a, b) {
    return (Number(b.priority_score) || 0) - (Number(a.priority_score) || 0) || Number(a.issue_id) - Number(b.issue_id);
  }).map(function (issue) {
    var r = issue.result || {};
    var prio = issue.priority === 'P1' ? '🔴' : (issue.priority === 'P2' ? '🟡' : '⚪');
    var subject = String(issue.subject || '').trim();
    var titleFull = '#' + issue.issue_id + (subject ? ' ' + subject : '');
    var hover = r.problem_summary ? titleFull + ' — ' + r.problem_summary : titleFull;
    // 与「Redmine 单号分析」条目同构：标题行只放 #单号 + 标题 + meta 标签
    // （分析时间 / 实机取证状态），problem_summary / suggested_solution 收进
    // 「查看分析」弹框（悬停 title 仍给一行摘要）。按钮集一致：查看分析 /
    // 打开 Redmine / AI 统计 / 增量-全量选择 / 重新分析 ↔ 停止分析。增量在
    // 晨报 run 上重跑（结论原地更新）；全量走 analyze-issue 新建独立 run
    // （晨报记录保留可审计，新结论进「Redmine 单号分析」历史）。行级停止走
    // run 级精确取消（stopDailyBriefRun）。同单号在单号分析中运行时，行状态
    // 与其联动（见 overlay*），行级停止指向该独立 run。
    var issueRunning = ['pending', 'snapshotting', 'analyzing', 'running']
      .indexOf(String(issue.status || '')) >= 0;
    // 同单号状态联动：单号分析存在该 issue 的 run 时镜像其状态（运行中/
    // 停止中实时一致）；该 run 终态且晚于晨报记录时，展示其结论，避免两个
    // 区块对同一单号各说各话。终态覆盖要求 started_at 晚于晨报记录时间，
    // 缺时间戳时保持晨报自身结论（只联动实时状态）。
    var overlayEntry = latestSingleIssueRunForIssue(issue.issue_id) || {};
    var overlayRun = overlayEntry.run || {};
    var overlayIssue = (overlayEntry.issues || [])[0] || {};
    var overlayStatus = overlayRun.run_id ? singleIssueAnalysisEffectiveStatus(overlayRun, overlayIssue) : '';
    var overlayRunning = Boolean(overlayRun.run_id) && singleIssueAnalysisIsRunning(overlayRun, overlayIssue);
    var overlayStopping = overlayRunning
      && Boolean(singleIssueStopRequested[String(overlayRun.run_id)] || overlayRun.cancel_requested);
    var briefFinishedAt = String(issue.finished_at || issue.started_at || '');
    var overlayNewerThanBrief = Boolean(overlayRun.run_id) && !overlayRunning
      && Boolean(String(overlayRun.started_at || ''))
      && (!briefFinishedAt || String(overlayRun.started_at) > briefFinishedAt);
    return '<div class="daily-brief-row">'
      + '<div class="daily-brief-main"><span class="daily-brief-priority">' + prio + '</span>'
      + '<span class="daily-brief-issue-title" title="' + esc(hover) + '">'
      + '<b>#' + esc(issue.issue_id) + '</b>' + (subject ? ' ' + esc(subject) : '')
      + '<span class="single-issue-analysis-meta">' + dailyBriefIssueMetaTags(issue) + '</span></span>'
      + '</div>'
      + (overlayStopping
        ? '<span class="daily-brief-state">⏳ 停止中…</span>'
        : overlayRunning
          ? '<span class="daily-brief-state" title="同单号的「Redmine 单号分析」正在运行">⏳ 分析中…</span>'
          : overlayNewerThanBrief
            ? '<span class="daily-brief-state' + (overlayStatus === 'failed' ? ' failed' : '')
              + '" title="最新结论来自「Redmine 单号分析」">' + esc(singleIssueAnalysisStatus(overlayStatus)) + '</span>'
            : (stopping && issueRunning
              ? '<span class="daily-brief-state">⏳ 停止中…</span>'
              : issueStateHtml(issue)))
      + '<div class="daily-brief-row-actions"><button type="button" class="ka-btn" data-click="showDailyBriefIssue" data-a0="' + esc(issue.issue_id) + '" data-prevent data-stop>查看分析</button>'
      + '<button class="ka-btn" data-click="openRedmineIssue" data-a0="' + esc(issue.issue_id) + '">打开 Redmine</button>'
      + '<button class="ka-btn" data-click="showDailyBriefIssueStatistics" data-a0="' + esc(issue.issue_id) + '">AI 统计</button>'
      + (overlayRunning
        ? // 联动的单号分析 run：停止必须指向该独立 run，而不是晨报 run。
          '<button class="ka-btn" data-daily-brief-overlay-stop="' + esc(overlayRun.run_id) + '"'
          + (overlayStopping ? ' disabled' : '') + '>' + (overlayStopping ? '⏳ 停止中…' : '停止分析') + '</button>'
        : issueRunning
          ? '<button class="ka-btn" data-click="stopDailyBriefRun"' + (stopping ? ' disabled' : '') + '>' + (stopping ? '⏳ 停止中…' : '停止分析') + '</button>'
          : '<select class="single-issue-reanalysis-mode" data-daily-brief-reanalysis-mode'
            + ' aria-label="#' + esc(issue.issue_id) + ' 重新分析方式"'
            + (inflight ? ' disabled title="晨报批次仍在执行，等待结束后再重分析此项"' : '') + '>'
            + '<option value="incremental">增量</option><option value="full">全量</option></select>'
            + '<button class="ka-btn" data-daily-brief-reanalyze="' + esc(issue.issue_id) + '"'
            + (inflight ? ' disabled title="晨报批次仍在执行，等待结束后再重分析此项"' : '') + '>重新分析</button>')
      + '</div>'
      + '</div>';
  }).join('');
  return head + rows;
}

async function openRedmineIssue(issueId) {
  // 每日晨报卡片在首页即可见，用户可能从未进入统计/设置页加载过
  // statsConfig；此处显式拉取（60s 缓存兜底），否则永远误报「未配置」。
  await loadStatsConfig();
  var base = redmineBaseUrl();  // 已有 helper：statsConfig.redmine.base_url 去尾部斜杠
  if (!base) { notifyUser('未配置 Redmine 地址', '请先在设置页配置 Redmine base_url', 'warning'); return; }
  window.open(base + '/issues/' + issueId, '_blank');
}

function findDailyBriefIssue(issueId) {
  var issues = (dailyBriefCache && dailyBriefCache.issues) || [];
  for (var index = 0; index < issues.length; index += 1) {
    if (String(issues[index].issue_id) === String(issueId)) return issues[index];
  }
  return null;
}

function showDailyBriefIssue(issueId) {
  // 运行中的分析展示实时进度时间线（含晨报批量 run 内的单条分析）；
  // 完成后原地切换为报告，并保留「执行过程」入口回看 30 天内的时间线。
  var run = (dailyBriefCache && dailyBriefCache.run) || {};
  var issue = findDailyBriefIssue(issueId) || {};
  var busyStates = ['pending', 'snapshotting', 'analyzing', 'running'];
  if (run.run_id && (busyStates.indexOf(String(run.status || '')) >= 0
    || busyStates.indexOf(String(issue.status || '')) >= 0)) {
    showAnalysisTimelineModal(run.run_id, issueId, { subject: issue.subject });
    return;
  }
  if (!issue.issue_id) {
    notifyUser('每日晨报数据已更新', '未找到 #' + issueId + '，正在刷新后重试。', 'warning');
    loadDailyBrief();
    return;
  }
  try {
    // 与「Redmine 单号分析」查看分析同一布局（纯报告正文 + 执行过程/关闭）。
    openSingleIssueReportModal({ run: run, issues: [issue] }, false);
  } catch (error) {
    console.error('Failed to render daily brief analysis', error);
    showDailyBriefIssueFallback(issueId);
    notifyUser('分析内容格式不兼容', '已打开简化视图；可重新分析此项以生成完整结果。', 'warning');
  }
}

function showDailyBriefIssueStatistics(issueId) {
  var issue = findDailyBriefIssue(issueId);
  var body = issue && renderDailyBriefIssueStatistics(issue.ai_statistics);
  if (!body) {
    notifyUser('暂无 AI 统计', '#' + issueId + ' 尚未保存可展示的分析轨迹。', 'warning');
    return;
  }
  var modalId = 'dailyBriefIssueStatisticsModal-' + Date.now();
  var modal = document.createElement('div');
  modal.id = modalId;
  modal.className = 'modal';
  modal.innerHTML = '<div class="modal-content daily-brief-modal daily-brief-statistics-modal">'
    + '<div class="modal-header"><span class="modal-title">📊 AI 统计 · #' + esc(issueId) + '</span>'
    + '<button type="button" class="modal-close" aria-label="关闭" data-click="removeDynamicModal" data-a0="' + modalId + '">&times;</button></div>'
    + '<div class="modal-body daily-brief-modal-body">' + body + '</div>'
    + '<div class="modal-buttons daily-brief-modal-footer"><button class="secondary" data-click="removeDynamicModal" data-a0="' + modalId + '">关闭</button></div></div>';
  document.body.appendChild(modal);
  showModal(modalId);
}

function dailyBriefList(value) {
  if (Array.isArray(value)) return value;
  return value == null || value === '' ? [] : [value];
}

function dailyBriefObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

// 与服务端 evidence_gate.result_analysis_mode 同构：无 evidence_gate 的
// 历史结果按 triage 特征（根因为空/占位且类型 unknown）判定，不能把
// undefined !== 'triage' 当成 diagnostic 放行「存为案例」。
function dailyBriefAnalysisMode(r, gate) {
  if (gate.analysis_mode) return gate.analysis_mode === 'triage' ? 'triage' : 'diagnostic';
  var rootCause = String(r.root_cause || '').trim();
  var placeholder = rootCause === '' || rootCause === '未进行深度诊断';
  var type = r.root_cause_type || 'unknown';
  return placeholder && type === 'unknown' ? 'triage' : 'diagnostic';
}

function showDailyBriefIssueFallback(issueId) {
  var modalId = 'dailyBriefIssueModal-' + Date.now();
  var modal = document.createElement('div');
  modal.id = modalId;
  modal.className = 'modal daily-brief-analysis-overlay';
  modal.innerHTML = '<div class="modal-content daily-brief-modal">'
    + '<div class="modal-header"><span class="modal-title">每日晨报 · #' + esc(issueId) + '</span>'
    + '<button type="button" class="modal-close" aria-label="关闭" data-click="removeDynamicModal" data-a0="' + modalId + '">&times;</button></div>'
    + '<div class="modal-body daily-brief-modal-body"><div class="muted">该历史分析结果包含旧格式字段，暂无法完整呈现。重新分析此项后会生成兼容的完整结果。</div></div>'
    + '<div class="modal-buttons daily-brief-modal-footer"><button data-daily-brief-reanalyze="' + esc(issueId) + '">深度分析此项</button>'
    + '<button class="secondary" data-click="removeDynamicModal" data-a0="' + modalId + '">关闭</button></div></div>';
  showDailyBriefAnalysisModal(modal);
}

// act-bridge 按 window 查找委托目标；显式导出避免页面脚本加载方式变化后
// 「查看分析」成为静默无响应的按钮。
window.showDailyBriefIssue = showDailyBriefIssue;
window.showDailyBriefIssueStatistics = showDailyBriefIssueStatistics;
window.openSingleIssueAnalysisTimeline = openSingleIssueAnalysisTimeline;
window.openMemberDashboard = openMemberDashboard;

function briefRowReanalysisMode(button) {
  // 晨报行「重新分析」读同行选择器；报告弹框按钮无选择器，默认增量。
  var actions = button.closest('.daily-brief-row-actions');
  var select = actions && actions.querySelector('[data-daily-brief-reanalysis-mode]');
  return String((select && select.value) || 'incremental') === 'full' ? 'full' : 'incremental';
}

async function reanalyzeDailyBriefIssue(issueId, button) {
  var run = dailyBriefCache && dailyBriefCache.run;
  if (!run || !run.brief_date) return;
  var mode = briefRowReanalysisMode(button);
  var original = button.textContent;
  button.disabled = true;
  button.textContent = '⏳ 分析中…';
  try {
    var base = '/api/redmine-agent/daily-brief';
    if (mode === 'full') {
      // 全量：与单号分析同语义——新建独立 run 保留完整审计链，不覆盖
      // 晨报 run 里这条的历史结论；结果出现在「Redmine 单号分析」列表。
      // 晨报行与该独立 run 状态联动（运行中/终态镜像），由单号分析历史
      // 轮询驱动刷新。
      var payload = {issue_id: Number(issueId), analysis_mode: 'full'};
      if (String(run.device_serial || '').trim()) payload.device_serial = String(run.device_serial).trim();
      if (String(run.analysis_hint || '').trim()) payload.analysis_hint = String(run.analysis_hint).trim();
      var queued = await api(base + '/analyze-issue', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
      }) || {};
      if (!queued.run_id) throw new Error(queued.error || '未创建全量分析任务');
      button.disabled = false;
      button.textContent = original;
      notifyUser('已加入全量分析队列', '#' + issueId + ' 将在独立 run 中重新分析；完成后可在下方 Redmine 单号分析中查看', 'success');
      upsertSingleIssueAnalysis({
        run: {run_id: queued.run_id, status: queued.status || 'pending', device_serial: String(payload.device_serial || '')},
        issues: [{issue_id: Number(issueId), status: 'pending'}],
      });
      renderSingleIssueAnalysisHistory();
      loadSingleIssueHistoryRun(queued.run_id);
      return;
    }
    var runId = run.run_id || '';
    var url = runId
      ? base + '/runs/' + encodeURIComponent(runId) + '/issues/' + encodeURIComponent(issueId) + '/reanalyze'
      : base + '/' + encodeURIComponent(run.brief_date) + '/issues/' + encodeURIComponent(issueId) + '/reanalyze';
    var incremental = await api(url, {method: 'POST'}) || {};
    if (dailyBriefCache && dailyBriefCache.run) dailyBriefCache.run.status = incremental.status || 'pending';
    var issue = dailyBriefCache && (dailyBriefCache.issues || []).find(function (item) { return String(item.issue_id) === String(issueId); });
    if (issue) issue.status = 'pending';
    dailyBriefManualPoll = true;
    await loadDailyBrief();
    // 晨报行内的「重新分析」不在弹框里（closest 为 null），只关弹框场景。
    var modal = button.closest('.modal');
    if (modal) removeDynamicModal(modal.id);
    notifyUser('已加入分析队列', '#' + issueId + ' 将由独立 Worker 重新分析', 'success');
    pollDailyBriefRun(incremental.run_id || run.run_id);
  } catch (e) {
    button.disabled = false;
    button.textContent = original;
    notifyUser('重新分析失败', e.message, 'error');
  }
}

document.addEventListener('click', function (event) {
  // 晨报行联动的是「单号分析」独立 run，行级停止精确指向该 run。
  var overlayStop = event.target.closest('[data-daily-brief-overlay-stop]');
  if (overlayStop) {
    stopSavedSingleIssueAnalysis(overlayStop.dataset.dailyBriefOverlayStop, overlayStop);
    return;
  }
  var button = event.target.closest('[data-daily-brief-reanalyze]');
  if (!button) return;
  reanalyzeDailyBriefIssue(button.dataset.dailyBriefReanalyze, button);
});

async function stopDailyBriefRun() {
  var run = dailyBriefCache && dailyBriefCache.run;
  if (!run || (!run.run_id && !run.brief_date)) return;
  var runKey = String(run.run_id || run.brief_date || '');
  dailyBriefStoppingRunId = runKey;
  var card = document.getElementById('dailyBriefCard');
  if (card && dailyBriefCache) card.innerHTML = renderDailyBriefInner(dailyBriefCache);
  try {
    // 优先按 run_id 精确停止（同一天可能存在 nightly/manual/delta 多个
    // run，按日期停"最新一次"可能停错目标）。
    var url = run.run_id
      ? '/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(run.run_id) + '/cancel'
      : '/api/redmine-agent/daily-brief/' + encodeURIComponent(run.brief_date) + '/cancel';
    var data = await api(url, {method: 'POST'}) || {};
    if (data.already_terminal) {
      dailyBriefStoppingRunId = '';
      notifyUser('无需停止', '该每日晨报已结束，无进行中的分析', 'info');
    } else {
      notifyUser('正在停止分析', 'Worker 正在终止当前 KkAgent；已完成结果会保留，未开始项可稍后重试', 'success');
    }
    // 立即刷新一次;收敛为 cancelled 后轮询会自然停。
    loadDailyBrief();
  } catch (e) {
    dailyBriefStoppingRunId = '';
    if (card && dailyBriefCache) card.innerHTML = renderDailyBriefInner(dailyBriefCache);
    notifyUser('停止失败', String(e), 'error');
  }
}

async function startDailyBriefRun() {
  var card = document.getElementById('dailyBriefCard');
  var currentStatus = dailyBriefCache && dailyBriefCache.run && dailyBriefCache.run.status;
  var force = currentStatus === 'completed' || currentStatus === 'partial' || currentStatus === 'failed' || currentStatus === 'cancelled';
  var rerender = function () {
    if (card && dailyBriefCache) card.innerHTML = renderDailyBriefInner(dailyBriefCache);
    updateRedmineToolbar();
  };
  dailyBriefRunStarting = true;
  rerender(); // 立即反馈：按钮变「⏳ 启动中…」
  try {
    // api() 已解包 envelope：返回值即 data.data 本体。
    var data = await api('/api/redmine-agent/daily-brief/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({force: Boolean(force)})
    }) || {};
    dailyBriefRunStarting = false;
    if (data.configured === false) { rerender(); notifyUser('未配置凭据', data.message || '请先在设置页保存 Redmine 凭据', 'warning'); return; }
    if (data.reused) { rerender(); notifyUser('每日晨报已存在', '当天每日晨报已完成，未重复生成', 'info'); loadDailyBrief(); return; }
    if (data.run_id) {
      if (data.already_running) { notifyUser('正在生成', '当天每日晨报已在执行中，已接入实时刷新', 'info'); }
      else { notifyUser('已开始生成', 'AI 正在分析，下方进度实时更新（run ' + data.run_id.slice(0, 10) + '…）', 'success'); }
      // 立即把本地缓存标为进行中，按钮切到「排队中/分析中…」状态
      if (dailyBriefCache && dailyBriefCache.run) {
        dailyBriefCache.run.status = data.status || 'pending';
        dailyBriefCache.run.finished_at = '';
      }
      dailyBriefManualPoll = true;
      rerender();
      pollDailyBriefRun(data.run_id);
    } else {
      rerender();
      notifyUser('启动失败', data.error || '未知错误', 'error');
    }
  } catch (e) {
    dailyBriefRunStarting = false;
    rerender();
    notifyUser('启动失败', String(e), 'error');
  }
}

function pollDailyBriefRun(runId, attempt) {
  var n = attempt || 0;
  if (n > 120) { dailyBriefManualPoll = false; loadDailyBrief(); return; }
  setTimeout(async function () {
    try {
      // 每个周期都刷新卡片：状态徽标、按钮与逐条 issue 状态实时可见
      await loadDailyBrief();
      var run = dailyBriefCache && dailyBriefCache.run;
      if (run && run.run_id === runId && (run.status === 'completed' || run.status === 'partial' || run.status === 'failed' || run.status === 'cancelled')) {
        dailyBriefManualPoll = false;
        notifyUser(
          run.status === 'cancelled' ? '每日晨报已停止' : '每日晨报已更新',
          '状态：' + run.status,
          run.status === 'failed' || run.status === 'cancelled' ? 'warning' : 'success'
        );
        return;
      }
      if (!run || run.run_id !== runId) {
        // run 被替换/删除，停止本Manual轮询，交给自动兜底
        dailyBriefManualPoll = false;
        return;
      }
    } catch (_) { /* 轮询失败静默重试 */ }
    pollDailyBriefRun(runId, n + 1);
  }, 5000);
}


// act-bridge 委托目标（替代历史 inline handler）。
function _actStopPropagation(event) { event.stopPropagation(); }
function _actClickById(elementId) { document.getElementById(elementId).click(); }
