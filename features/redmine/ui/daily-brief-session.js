// kkagent 会话回放：按真实 message/turn 聚合，工具结果回挂到对应调用。
// 并发正确性：状态按 modalId 独立存放，异步回调只写自己捕获的
// state（绝不读“全局最新 state”）；每个请求带自增 token 与独立
// AbortController，弹框被移除或新请求开始时旧响应一律丢弃——快速
// 打开 A→B 时 A 的迟到响应不可能写进 B 的弹框。
var issueSessionStates = {};
// 最近一次打开的会话 state（诊断/兼容用途，逻辑一律不依赖它）。
var issueSessionState = null;

function issueSessionNewState(modalId, runId, issueId) {
  return {
    modalId: modalId, runId: String(runId || ''), issueId: Number(issueId) || 0,
    nextOffset: 0, totalTurns: null, truncated: true, mode: 'turns', loading: false,
    rawLoaded: false, rawNextOffset: 0, rawTotalMessages: null, rawTruncated: false,
    requestToken: 0, controller: null,
  };
}

function issueSessionBeginRequest(state) {
  // 新请求作废上一笔在途请求：同一弹框不会出现两次并发写 DOM。
  if (state.controller) { try { state.controller.abort(); } catch (error) {} }
  state.controller = new AbortController();
  state.requestToken += 1;
  return { token: state.requestToken, signal: state.controller.signal };
}

function issueSessionIsStale(state, token) {
  return state.requestToken !== token || issueSessionStates[state.modalId] !== state;
}

function issueSessionForget(modalId) {
  var state = issueSessionStates[modalId];
  if (!state) return;
  if (state.controller) { try { state.controller.abort(); } catch (error) {} }
  delete issueSessionStates[modalId];
  if (issueSessionState === state) issueSessionState = null;
}

var issueSessionRemovalObserver = null;

function ensureIssueSessionRemovalObserver() {
  if (issueSessionRemovalObserver || typeof MutationObserver !== 'function') return;
  var root = document.body || document.documentElement;
  if (!root) return;
  issueSessionRemovalObserver = new MutationObserver(function (mutations) {
    for (var i = 0; i < mutations.length; i += 1) {
      var removed = mutations[i].removedNodes || [];
      for (var j = 0; j < removed.length; j += 1) {
        var node = removed[j];
        if (!node || node.nodeType !== 1) continue;
        var id = String(node.id || '');
        if (id && Object.prototype.hasOwnProperty.call(issueSessionStates, id)) {
          issueSessionForget(id);
        }
      }
    }
  });
  issueSessionRemovalObserver.observe(root, { childList: true, subtree: true });
}

function issueSessionFetchUrl(runId, issueId, offset, view) {
  var selectedView = view === 'raw' ? 'raw' : 'turns';
  return '/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(runId)
    + '/issues/' + encodeURIComponent(issueId)
    + '/session?offset=' + Number(offset || 0)
    + '&limit=' + (selectedView === 'raw' ? 20 : 80)
    + '&view=' + selectedView;
}

function issueSessionPrettyValue(value) {
  var text = String(value == null ? '' : value);
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (error) { return text; }
}

function issueSessionTime(value) {
  var text = String(value || '');
  return text.replace('T', ' ').replace(/\.\d+(?:Z)?$/, '').replace(/Z$/, '');
}

function issueSessionToolHint(value) {
  try {
    var data = JSON.parse(String(value || ''));
    if (!data || Array.isArray(data) || typeof data !== 'object') return '';
    var keys = ['issue_id', 'artifact_id', 'snapshot_id', 'device', 'offset', 'limit'];
    return keys.filter(function (key) { return data[key] != null; }).slice(0, 3)
      .map(function (key) { return key + '=' + String(data[key]); }).join(' · ');
  } catch (error) {
    return '';
  }
}

function renderIssueSessionTool(block) {
  var result = block.result || null;
  var failed = Boolean(result && result.is_error);
  var status = result ? (failed ? '失败' : '完成') : '无返回';
  var hint = issueSessionToolHint(block.tool_input);
  return '<details class="issue-session-tool' + (failed ? ' error' : '') + '"'
    + (failed ? ' open' : '') + '><summary>'
    + '<span class="issue-session-tool-icon">' + (failed ? '×' : (result ? '✓' : '•')) + '</span>'
    + '<code>' + esc(block.tool_name || 'unknown_tool') + '</code>'
    + '<span class="issue-session-tool-status">' + status + '</span>'
    + (hint ? '<span class="issue-session-tool-hint">' + esc(hint) + '</span>' : '')
    + '</summary><div class="issue-session-tool-body">'
    + '<div class="issue-session-payload-label">输入</div><pre>'
    + esc(issueSessionPrettyValue(block.tool_input)) + '</pre>'
    + (result ? '<div class="issue-session-payload-label">输出</div><pre>'
      + esc(issueSessionPrettyValue(result.output)) + '</pre>' : '')
    + '</div></details>';
}

function renderIssueSessionOrphanResult(block) {
  var failed = Boolean(block.is_error);
  return '<details class="issue-session-tool issue-session-orphan' + (failed ? ' error' : '')
    + '"' + (failed ? ' open' : '') + '><summary>'
    + '<span class="issue-session-tool-icon">' + (failed ? '×' : '✓') + '</span>'
    + '<code>未匹配的工具结果</code><span class="issue-session-tool-status">'
    + (failed ? '失败' : '完成') + '</span></summary>'
    + '<div class="issue-session-tool-body"><div class="issue-session-payload-label">输出</div>'
    + '<pre>' + esc(issueSessionPrettyValue(block.output)) + '</pre></div></details>';
}

function renderIssueSessionRawMessage(message) {
  var roleLabel = message.role === 'assistant' ? 'assistant' : (message.role || 'user');
  var content = typeof message.content === 'string'
    ? message.content : JSON.stringify(message.content, null, 2);
  return '<article class="issue-session-raw-message"><div class="issue-session-raw-header"><strong>'
    + esc(roleLabel) + '</strong><span>message #' + esc(message.sequence) + '</span>'
    + (message.created_at ? '<span>' + esc(issueSessionTime(message.created_at)) + '</span>' : '')
    + (message.token_count ? '<span>' + esc(message.token_count) + ' tokens</span>' : '')
    + '</div><pre>' + esc(content) + '</pre></article>';
}

function renderIssueSessionContext(turn) {
  var texts = (turn.blocks || []).filter(function (block) { return block.kind === 'text'; })
    .map(function (block) { return block.text || ''; });
  var label = turn.kind === 'context' ? '任务上下文' : '补充指令';
  var text = texts.join('\n\n');
  return '<details class="issue-session-context"><summary><span>' + label + '</span>'
    + '<span class="issue-session-context-meta">#' + esc(turn.sequence) + ' · '
    + text.length + ' 字' + (turn.created_at ? ' · ' + esc(issueSessionTime(turn.created_at)) : '')
    + '</span></summary><pre>' + esc(text) + '</pre></details>';
}

function renderIssueSessionTurn(turn) {
  if (turn.kind === 'context' || turn.kind === 'request') {
    return renderIssueSessionContext(turn);
  }
  var blocks = turn.blocks || [];
  var body = blocks.map(function (block) {
    if (block.kind === 'text') {
      return '<div class="issue-session-message">' + renderMarkdownDoc(block.text || '') + '</div>';
    }
    if (block.kind === 'tool_use') return renderIssueSessionTool(block);
    if (block.kind === 'tool_result') return renderIssueSessionOrphanResult(block);
    return '';
  }).join('');
  var failed = blocks.some(function (block) {
    return block.kind === 'tool_result' ? block.is_error : Boolean(block.result && block.result.is_error);
  });
  return '<article class="issue-session-turn' + (failed ? ' has-error' : '') + '">'
    + '<div class="issue-session-turn-header"><span class="issue-session-avatar">K</span><strong>kkagent</strong>'
    + '<span class="issue-session-turn-meta">回合 #' + esc(turn.sequence)
    + (turn.created_at ? ' · ' + esc(issueSessionTime(turn.created_at)) : '') + '</span>'
    + (turn.is_final ? '<span class="issue-session-final-tag">最终回答</span>' : '')
    + '</div><div class="issue-session-turn-body">' + body + '</div></article>';
}

function updateIssueSessionControls(state) {
  var more = document.getElementById(state.modalId + '-more');
  var toggle = document.getElementById(state.modalId + '-view-toggle');
  if (toggle) {
    toggle.disabled = state.loading;
    toggle.textContent = state.mode === 'raw' ? '返回会话视图' : '查看未裁剪原文';
  }
  if (!more) return;
  if (state.loading) {
    more.disabled = true;
    more.textContent = '⏳ 加载中…';
    return;
  }
  var raw = state.mode === 'raw';
  var truncated = raw ? state.rawTruncated : state.truncated;
  var offset = raw ? state.rawNextOffset : state.nextOffset;
  var total = raw ? state.rawTotalMessages : state.totalTurns;
  more.disabled = !truncated;
  more.textContent = truncated
    ? '加载更多（已显示 ' + offset + (raw ? ' 条原始消息）' : ' 个回合）')
    : '已全部加载（' + (total == null ? offset : total)
      + (raw ? ' 条原始消息）' : ' 个回合）');
}

function appendIssueSessionTurns(state, data) {
  var stream = document.getElementById(state.modalId + '-stream');
  if (!stream) return;
  var html = '';
  (data.turns || []).forEach(function (turn) { html += renderIssueSessionTurn(turn); });
  stream.insertAdjacentHTML('beforeend', html);
  state.nextOffset = Number(data.next_offset || 0);
  state.totalTurns = data.total_turns == null ? null : Number(data.total_turns);
  state.truncated = Boolean(data.truncated);
  updateIssueSessionControls(state);
}

function appendIssueSessionRawMessages(state, data) {
  var stream = document.getElementById(state.modalId + '-raw');
  if (!stream) return;
  var html = '';
  (data.messages || []).forEach(function (message) {
    html += renderIssueSessionRawMessage(message);
  });
  stream.insertAdjacentHTML('beforeend', html);
  state.rawLoaded = true;
  state.rawNextOffset = Number(data.next_offset || 0);
  state.rawTotalMessages = data.total_messages == null ? null : Number(data.total_messages);
  state.rawTruncated = Boolean(data.truncated);
  updateIssueSessionControls(state);
}

async function loadMoreIssueSession(modalId) {
  var state = issueSessionStates[modalId];
  if (!state || state.loading) return;
  var raw = state.mode === 'raw';
  if (!(raw ? state.rawTruncated : state.truncated)) return;
  var request = issueSessionBeginRequest(state);
  state.loading = true;
  updateIssueSessionControls(state);
  try {
    var offset = raw ? state.rawNextOffset : state.nextOffset;
    var data = await api(issueSessionFetchUrl(state.runId, state.issueId, offset, state.mode), { signal: request.signal }) || {};
    if (issueSessionIsStale(state, request.token)) return;
    if (raw) appendIssueSessionRawMessages(state, data);
    else appendIssueSessionTurns(state, data);
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    console.error('Failed to load issue session page', error);
    notifyUser('会话加载失败', String(error && error.message || error), 'error');
  } finally {
    if (issueSessionStates[modalId] === state) {
      state.loading = false;
      updateIssueSessionControls(state);
    }
  }
}

async function toggleIssueSessionView(modalId) {
  var state = issueSessionStates[modalId];
  if (!state || state.loading) return;
  state.mode = state.mode === 'raw' ? 'turns' : 'raw';
  var turns = document.getElementById(modalId + '-stream');
  var rawStream = document.getElementById(modalId + '-raw');
  var note = document.getElementById(modalId + '-note');
  if (turns) turns.hidden = state.mode === 'raw';
  if (rawStream) rawStream.hidden = state.mode !== 'raw';
  if (note) note.textContent = state.mode === 'raw'
    ? '未裁剪原文按原始 message 分页展示；内部 thinking/redacted_thinking 不展示。'
    : '按原始消息回合聚合；页面预览会裁剪，工具结果已回挂到对应调用。';
  updateIssueSessionControls(state);
  if (state.mode !== 'raw' || state.rawLoaded) return;
  var request = issueSessionBeginRequest(state);
  state.loading = true;
  if (rawStream) rawStream.innerHTML = '<div class="muted">⏳ 正在加载未裁剪原文…</div>';
  updateIssueSessionControls(state);
  try {
    var data = await api(issueSessionFetchUrl(state.runId, state.issueId, 0, 'raw'), { signal: request.signal }) || {};
    if (issueSessionIsStale(state, request.token)) return;
    if (rawStream) rawStream.innerHTML = '';
    appendIssueSessionRawMessages(state, data);
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    console.error('Failed to load raw issue session', error);
    if (rawStream) rawStream.innerHTML = '<div class="muted">原文加载失败：'
      + esc(String(error && error.message || error)) + '</div>';
    notifyUser('原文加载失败', String(error && error.message || error), 'error');
  } finally {
    if (issueSessionStates[modalId] === state) {
      state.loading = false;
      updateIssueSessionControls(state);
    }
  }
}

function issueSessionReturnModalId() {
  var open = document.querySelectorAll('.modal.show');
  for (var index = open.length - 1; index >= 0; index -= 1) {
    var modalId = String(open[index].id || '');
    if (/^(singleIssueAnalysisModal|dailyBriefIssueModal)-/.test(modalId)) return modalId;
  }
  return '';
}

async function showIssueFullSession(runId, issueId, subject) {
  // 保留 AI 分析弹框为 ModalController 栈中的下层项；打开会话后它会
  // 自动 inert，移除会话弹框时自动恢复，无需第二次重新打开报告。
  var returnModalId = issueSessionReturnModalId();
  var modalId = 'issueFullSessionModal-' + Date.now();
  ensureIssueSessionRemovalObserver();
  var state = issueSessionNewState(modalId, runId, issueId);
  issueSessionStates[modalId] = state;
  issueSessionState = state;
  var modal = document.createElement('div');
  modal.id = modalId;
  modal.className = 'modal daily-brief-analysis-overlay';
  modal.innerHTML = '<div class="modal-content daily-brief-modal">'
    + '<div class="modal-header"><span class="modal-title daily-brief-modal-title"><span>🧵 kkagent 会话 · #' + esc(issueId) + '</span>'
    + (subject ? '<span class="daily-brief-modal-subject">' + esc(subject) + '</span>' : '')
    + '</span><button type="button" class="modal-close" aria-label="关闭" data-click="removeDynamicModal" data-a0="' + modalId + '">&times;</button></div>'
    + '<div class="modal-body daily-brief-modal-body"><div id="' + modalId + '-note" class="issue-session-note">按原始消息回合聚合；页面预览会裁剪，工具结果已回挂到对应调用。</div>'
    + '<div id="' + modalId + '-stream" class="issue-session-stream">'
    + '<div class="muted">⏳ 正在加载会话…</div></div>'
    + '<div id="' + modalId + '-raw" class="issue-session-raw-stream" hidden></div></div>'
    + '<div class="modal-buttons daily-brief-modal-footer">'
    + '<button class="secondary" id="' + modalId + '-view-toggle" data-click="toggleIssueSessionView" data-a0="' + modalId + '">查看未裁剪原文</button>'
    + '<button class="secondary" id="' + modalId + '-more" data-click="loadMoreIssueSession" data-a0="' + modalId + '" disabled>加载更多</button>'
    + '<button class="secondary" data-click="removeDynamicModal" data-a0="' + modalId + '">'
    + (returnModalId ? '返回 AI 分析' : '关闭') + '</button></div></div>';
  document.body.appendChild(modal);
  showModal(modalId);
  var request = issueSessionBeginRequest(state);
  try {
    var data = await api(issueSessionFetchUrl(runId, issueId, 0), { signal: request.signal }) || {};
    if (issueSessionIsStale(state, request.token)) return;
    var stream = document.getElementById(modalId + '-stream');
    if (stream) stream.innerHTML = '';
    if (data && (data.turns || []).length) {
      appendIssueSessionTurns(state, data);
    } else {
      var empty = document.getElementById(modalId + '-stream');
      if (empty) empty.innerHTML = '<div class="muted">该会话没有可回放的内容。</div>';
      var more = document.getElementById(modalId + '-more');
      if (more) more.disabled = true;
    }
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    console.error('Failed to load issue session', error);
    var failed = document.getElementById(modalId + '-stream');
    if (failed) failed.innerHTML = '<div class="muted">会话加载失败：' + esc(String(error && error.message || error)) + '</div>';
    var more2 = document.getElementById(modalId + '-more');
    if (more2) more2.disabled = true;
  }
}

window.showIssueFullSession = showIssueFullSession;
window.loadMoreIssueSession = loadMoreIssueSession;
window.toggleIssueSessionView = toggleIssueSessionView;
