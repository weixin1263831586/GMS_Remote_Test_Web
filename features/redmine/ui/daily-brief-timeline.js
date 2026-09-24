// Daily Brief 分析时间线的实时会话轨迹辅助。
// 从 page.js 拆出：kkagent session 回放的轻量行渲染与时间格式化，
// 供「正在分析」弹框把 Controller 进度事件与 kkagent 会话融合到同一
// 时间线窗口（分析中实时追加，结束后自然成为历史回放）。
// thinking/redacted_thinking 在服务端已过滤，这里只消费可见块。

function issueSessionTimelineTime(value) {
  var text = String(value || '');
  return text.replace('T', ' ').replace(/\.\d+(?:Z)?$/, '').replace(/Z$/, '');
}

// 把 kkagent 回放回合渲染成时间线行：text 截断 400 字，tool_use 显示
// 工具名 + 状态图标；thinking 不经过此层（服务端已剔除）。
function analysisTimelineSessionTurnRow(turn) {
  var blocks = turn.blocks || [];
  var parts = [];
  blocks.forEach(function (block) {
    if (block.kind === 'text') {
      var text = String(block.text || '').trim();
      if (text) parts.push(esc(text.length > 400 ? text.slice(0, 400) + '…' : text));
    } else if (block.kind === 'tool_use') {
      var result = block.result || null;
      var icon = result ? (result.is_error ? '✕' : '✓') : '…';
      parts.push('<code class="analysis-session-tool">' + icon + ' '
        + esc(block.tool_name || 'tool') + '</code>');
    } else if (block.kind === 'tool_result') {
      parts.push('<code class="analysis-session-tool">'
        + (block.is_error ? '✕' : '✓') + ' ' + esc(block.tool_name || 'tool')
        + '（未匹配结果）</code>');
    }
  });
  if (!parts.length) return '';
  var time = issueSessionTimelineTime(turn.created_at || '');
  return '<div class="analysis-timeline-item session">'
    + '<span class="analysis-timeline-icon">🧵</span>'
    + (time ? '<span class="analysis-timeline-time">' + esc(time) + '</span>' : '')
    + '<span class="analysis-timeline-text">' + parts.join('<br>') + '</span></div>';
}

function renderAnalysisTimelineBody() {
  var state = analysisTimelineState;
  if (!state.events.length && !state.sessionTurns.length) {
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
  if (state.sessionTurns.length) {
    html += '<div class="analysis-timeline-stage">'
      + '<span>AI 会话轨迹（thinking 已过滤，长内容截断）</span></div>';
    state.sessionTurns.forEach(function (turn) {
      html += analysisTimelineSessionTurnRow(turn);
    });
    if (!state.sessionEnd) {
      html += '<div class="analysis-timeline-item session running">'
        + '<span class="analysis-timeline-icon">⏳</span>'
        + '<span class="analysis-timeline-text">会话轨迹实时跟踪中…</span></div>';
    }
  }
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
  var state = analysisTimelineState;
  var title = document.getElementById(state.modalId + '-title');
  if (!title) return;
  var prefix = ({ live: '🔵 正在分析 · #', timeline: '🕐 执行过程 · #',
    report: '📋 分析总结 · #' })[view] || '🕐 执行过程 · #';
  title.innerHTML = '<span>' + prefix + esc(state.issueId) + '</span>'
    + (view === 'timeline' ? '<span class="analysis-timeline-badge">已结束'
      + (statusText ? ' · ' + esc(statusText) : '') + '</span>' : '');
}

function renderAnalysisTimelineBox() {
  var state = analysisTimelineState;
  var box = document.getElementById(state.modalId + '-timeline');
  if (!box) return;
  var stick = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.innerHTML = renderAnalysisTimelineBody();
  if (stick) box.scrollTop = box.scrollHeight;
}

async function loadAnalysisTimelineSessionHistory() {
  // 历史回放：一次性按 offset 翻页取完 kkagent 会话回合（turns 视图），
  // 并入同一时间线窗口；无会话（未落库/已清理）时静默跳过。
  var state = analysisTimelineState;
  var modalId = state.modalId;
  var runId = state.runId;
  var issueId = state.issueId;
  var turns = [];
  var offset = 0;
  if (state.sessionController) { try { state.sessionController.abort(); } catch (_) {} }
  state.sessionController = new AbortController();
  var token = ++state.sessionRequestToken;
  var signal = state.sessionController.signal;
  state.sessionLoading = true;
  try {
    for (var page = 0; page < 25; page += 1) {
      var data = await api(
        '/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(runId)
        + '/issues/' + encodeURIComponent(issueId)
        + '/session?offset=' + offset + '&limit=80&view=turns',
        { signal: signal }) || {};
      if (token !== state.sessionRequestToken || state.modalId !== modalId
          || state.runId !== runId || state.issueId !== issueId
          || !document.getElementById(modalId)) return;
      var pageTurns = Array.isArray(data.turns) ? data.turns : [];
      turns = turns.concat(pageTurns);
      offset = Number(data.next_offset || 0) || offset;
      if (!data.truncated || !pageTurns.length) break;
    }
  } catch (error) {
    // 404（无会话）/ 读取失败：保持仅 progress 时间线，不阻断回放视图。
    if (token === state.sessionRequestToken) {
      state.sessionLoading = false;
      state.sessionEnd = true;
    }
    return;
  }
  if (token !== state.sessionRequestToken || state.modalId !== modalId
      || state.runId !== runId || state.issueId !== issueId
      || !document.getElementById(modalId)) return;
  state.sessionTurns = turns.slice(-200);
  state.sessionAfter = offset;
  state.sessionEnd = true;
  state.sessionLoading = false;
}

async function pollAnalysisTimelineSession() {
  // kkagent 会话增量 tail：turns 视图按 offset 分页拉回合，
  // 服务端完成 turn 聚合与 thinking 过滤；无新回合时静默等待下一轮。
  var state = analysisTimelineState;
  if (!state.sessionId || state.sessionEnd || state.sessionLoading) return;
  if (!document.getElementById(state.modalId)) { stopAnalysisTimelinePolling(); return; }
  var modalId = state.modalId;
  var runId = state.runId;
  var issueId = state.issueId;
  // 回退一个回合重读：会话末尾的 assistant turn 可能先以
  // “工具尚无返回”出现，随后的 tool_result 会回挂到同一
  // sequence。只从 next_offset 向后读会永久错过这次更新。
  var offset = Math.max(0, Number(state.sessionAfter || 0) - 1);
  if (state.sessionController) { try { state.sessionController.abort(); } catch (_) {} }
  state.sessionController = new AbortController();
  var token = ++state.sessionRequestToken;
  state.sessionLoading = true;
  try {
    var data = await api(
      '/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(runId)
      + '/issues/' + encodeURIComponent(issueId)
      + '/session?offset=' + offset
      + '&limit=80&view=turns', { signal: state.sessionController.signal }) || {};
    if (token !== state.sessionRequestToken || state.modalId !== modalId
        || state.runId !== runId || state.issueId !== issueId
        || !document.getElementById(modalId)) return;
    var turns = Array.isArray(data.turns) ? data.turns : [];
    if (turns.length) {
      var bySequence = {};
      state.sessionTurns.concat(turns).forEach(function (turn) {
        bySequence[String(turn.sequence)] = turn;
      });
      state.sessionTurns = Object.keys(bySequence).map(function (key) {
        return bySequence[key];
      }).sort(function (left, right) {
        return Number(left.sequence) - Number(right.sequence);
      }).slice(-200);
      renderAnalysisTimelineBox();
    }
    state.sessionAfter = Number(data.next_offset || 0) || state.sessionAfter;
    // truncated=false 只表示“此刻已追到会话末尾”，不是会话
    // 终态。分析运行中后续 message 仍会追加，必须继续轮询。
  } catch (error) {
    if (token !== state.sessionRequestToken) return;
    if (error && error.name === 'AbortError') return;
    if (error && error.status && error.status !== 404) {
      // 403/5xx 等确定失败不持续刷屏；404 表示会话尚未
      // 落库，无 status 的网络瞬断交给下一轮重试。
      state.sessionEnd = true;
    }
  } finally {
    if (token === state.sessionRequestToken) state.sessionLoading = false;
  }
}
