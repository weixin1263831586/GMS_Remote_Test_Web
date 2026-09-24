// kkagent 完整会话回放：与 Controller 标准化“执行过程”时间线互补。
var issueSessionState = {
  modalId: '', runId: '', issueId: 0, nextOffset: 0, totalEvents: null,
  truncated: false, loading: false,
};

function issueSessionFetchUrl(runId, issueId, offset) {
  return '/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(runId)
    + '/issues/' + encodeURIComponent(issueId)
    + '/session?offset=' + Number(offset || 0) + '&limit=80';
}

function renderIssueSessionEvent(event) {
  var kindLabel = ({ text: '输出', tool_use: '工具调用', tool_result: '工具结果' })[event.kind] || event.kind;
  var roleLabel = event.role === 'user' ? '环境' : '模型';
  var meta = '<div class="issue-session-meta"><span>#' + esc(event.sequence) + '</span>'
    + '<span>' + esc(roleLabel) + '</span><span>' + esc(kindLabel) + '</span>';
  var body = '';
  if (event.kind === 'text') {
    body = '<pre>' + esc(event.text || '') + '</pre>';
  } else if (event.kind === 'tool_use') {
    meta += '<span>' + esc(event.tool_name || '') + '</span>';
    body = '<pre>' + esc(event.tool_input || '') + '</pre>';
  } else if (event.kind === 'tool_result') {
    if (event.is_error) meta += '<span class="issue-session-error-tag">失败</span>';
    body = '<pre>' + esc(event.output || '') + '</pre>';
  }
  meta += '</div>';
  return '<div class="issue-session-event' + (event.is_error ? ' error' : '')
    + (event.kind === 'tool_result' && !event.is_error ? ' ok' : '') + '">'
    + meta + body + '</div>';
}

function appendIssueSessionEvents(data) {
  var state = issueSessionState;
  var stream = document.getElementById(state.modalId + '-stream');
  if (!stream) return;
  var html = '';
  (data.events || []).forEach(function (event) { html += renderIssueSessionEvent(event); });
  stream.insertAdjacentHTML('beforeend', html);
  state.nextOffset = Number(data.next_offset || 0);
  state.totalEvents = data.total_events == null ? null : Number(data.total_events);
  state.truncated = Boolean(data.truncated);
  var more = document.getElementById(state.modalId + '-more');
  if (!more) return;
  if (state.truncated) {
    more.disabled = false;
    more.textContent = '加载更多（已显示 ' + state.nextOffset + ' 条）';
  } else {
    more.disabled = true;
    var total = state.totalEvents == null ? state.nextOffset : state.totalEvents;
    more.textContent = '已全部加载（' + total + ' 条事件）';
  }
}

async function loadMoreIssueSession(modalId) {
  var state = issueSessionState;
  if (state.modalId !== modalId || state.loading || !state.truncated) return;
  var button = document.getElementById(modalId + '-more');
  if (button) { button.disabled = true; button.textContent = '⏳ 加载中…'; }
  state.loading = true;
  try {
    var data = await api(issueSessionFetchUrl(state.runId, state.issueId, state.nextOffset)) || {};
    appendIssueSessionEvents(data);
  } catch (error) {
    console.error('Failed to load issue session page', error);
    notifyUser('会话加载失败', String(error && error.message || error), 'error');
    if (button) { button.disabled = false; button.textContent = '重试加载更多'; }
  } finally {
    state.loading = false;
  }
}

async function showIssueFullSession(runId, issueId, subject) {
  closeReplacedReportModal();
  var modalId = 'issueFullSessionModal-' + Date.now();
  issueSessionState = {
    modalId: modalId, runId: String(runId || ''), issueId: Number(issueId) || 0,
    nextOffset: 0, totalEvents: null, truncated: true, loading: false,
  };
  var modal = document.createElement('div');
  modal.id = modalId;
  modal.className = 'modal daily-brief-analysis-overlay';
  modal.innerHTML = '<div class="modal-content daily-brief-modal">'
    + '<div class="modal-header"><span class="modal-title daily-brief-modal-title"><span>🧵 kkagent 完整会话 · #' + esc(issueId) + '</span>'
    + (subject ? '<span class="daily-brief-modal-subject">' + esc(subject) + '</span>' : '')
    + '</span><button type="button" class="modal-close" aria-label="关闭" data-click="removeDynamicModal" data-a0="' + modalId + '">&times;</button></div>'
    + '<div class="modal-body daily-brief-modal-body"><div id="' + modalId + '-stream" class="issue-session-stream">'
    + '<div class="muted">⏳ 正在加载会话…</div></div></div>'
    + '<div class="modal-buttons daily-brief-modal-footer">'
    + '<button class="secondary" id="' + modalId + '-more" data-click="loadMoreIssueSession" data-a0="' + modalId + '" disabled>加载更多</button>'
    + '<button class="secondary" data-click="removeDynamicModal" data-a0="' + modalId + '">关闭</button></div></div>';
  document.body.appendChild(modal);
  showModal(modalId);
  try {
    var data = await api(issueSessionFetchUrl(runId, issueId, 0)) || {};
    var stream = document.getElementById(modalId + '-stream');
    if (stream) stream.innerHTML = '';
    if (data && (data.events || []).length) {
      appendIssueSessionEvents(data);
    } else {
      var empty = document.getElementById(modalId + '-stream');
      if (empty) empty.innerHTML = '<div class="muted">该会话没有可回放的事件。</div>';
      var more = document.getElementById(modalId + '-more');
      if (more) more.disabled = true;
    }
  } catch (error) {
    console.error('Failed to load issue session', error);
    var failed = document.getElementById(modalId + '-stream');
    if (failed) failed.innerHTML = '<div class="muted">会话加载失败：' + esc(String(error && error.message || error)) + '</div>';
    var more2 = document.getElementById(modalId + '-more');
    if (more2) more2.disabled = true;
  }
}

window.showIssueFullSession = showIssueFullSession;
window.loadMoreIssueSession = loadMoreIssueSession;
