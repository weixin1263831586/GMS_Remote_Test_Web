// Redmine analysis-timeline; loaded by page.html in dependency order.
var ANALYSIS_EVENT_RETENTION_DAYS = 30;

var analysisTimelineState = {
  modalId: '', runId: '', issueId: 0, after: 0, events: [],
  timer: null, controller: null, requestToken: 0, openedAt: 0,
  // history：历史回看模式（终态 run 的完整时间线，不轮询、不自动切报告）；
  // terminal/runStatus：终态后保留在 state 上供报告 ↔ 执行过程切换复用。
  history: false, terminal: false, runStatus: '',
  // 实时会话轨迹：progress 时间线里出现 session_started 事件后
  // 自动 tail kkagent 会话（session 回放 API + offset 协议），把模型
  // text/tool_use/tool_result 追加进同一时间线窗口；分析结束后停止
  // tail，窗口保留为历史回放。sessionId 为空 = 尚未发现/不支持。
  sessionId: '', sessionAfter: 0, sessionEnd: false, sessionLoading: false,
  sessionTurns: [], sessionController: null, sessionRequestToken: 0,
};

function stopAnalysisTimelinePolling() {
  var state = analysisTimelineState;
  clearTimeout(state.timer);
  if (state.controller) { try { state.controller.abort(); } catch (_) {} }
  state.requestToken += 1;
  if (state.sessionController) { try { state.sessionController.abort(); } catch (_) {} }
  state.sessionRequestToken += 1;
  state.modalId = ''; state.runId = ''; state.issueId = 0; state.after = 0;
  state.events = []; state.timer = null; state.controller = null;
  state.history = false; state.terminal = false; state.runStatus = '';
  state.sessionId = ''; state.sessionAfter = 0;
  state.sessionEnd = false; state.sessionLoading = false;
  state.sessionTurns = []; state.sessionController = null;
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
  // 历史回放同样加载 kkagent 会话轨迹；在同一窗口中，分析结束即
  // 自然成为完整历史会话）。一次性拉全，不再 tail。
  await loadAnalysisTimelineSessionHistory();
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

// 实时会话轨迹：渲染与时间格式化在 daily-brief-timeline.js
// （analysisTimelineSessionTurnRow / issueSessionTimelineTime）。

async function pollAnalysisTimeline() {
  var state = analysisTimelineState;
  if (!state.runId || !document.getElementById(state.modalId)) {
    stopAnalysisTimelinePolling();
    return;
  }
  var modalId = state.modalId;
  var runId = state.runId;
  var issueId = state.issueId;
  var token = ++state.requestToken;
  try {
    state.controller = new AbortController();
    var data = await api('/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(runId)
      + '/issues/' + encodeURIComponent(issueId)
      + '/events?after_sequence=' + state.after + '&limit=100', { signal: state.controller.signal }) || {};
    if (token !== state.requestToken || state.modalId !== modalId
        || state.runId !== runId || state.issueId !== issueId) return;
    if (!document.getElementById(modalId)) { stopAnalysisTimelinePolling(); return; }
    if (Array.isArray(data.events) && data.events.length) {
      state.events = state.events.concat(data.events).slice(-400);
      state.after = Number(data.next_sequence) || state.after;
      renderAnalysisTimelineBox();
      // 实时会话轨迹：发现 session_started 事件后启动 kkagent
      // 会话 tail（一次），此后每轮轮询并行增量拉取。
      if (!state.sessionId) {
        for (var i = 0; i < data.events.length; i += 1) {
          if (data.events[i].event_type === 'session_started') {
            var summary = String(data.events[i].summary || '');
            var match = summary.match(/session_id=(\S+)/);
            if (match) {
              state.sessionId = match[1];
            }
            break;
          }
        }
      }
    }
    // 会话增长不一定伴随新的 Controller 进度事件，所以
    // tail 必须在每轮 events 轮询后独立运行。
    if (state.sessionId && !state.sessionEnd) pollAnalysisTimelineSession();
    var meta = document.getElementById(state.modalId + '-meta');
    if (meta) meta.innerHTML = analysisTimelineMetaHtml(String(data.run_status || ''));
    if (data.terminal) {
      await finishAnalysisTimeline(modalId, runId, issueId);
      return;
    }
  } catch (error) {
    if (token !== state.requestToken || state.modalId !== modalId
        || state.runId !== runId || state.issueId !== issueId) return;
    if (!document.getElementById(modalId)) { stopAnalysisTimelinePolling(); return; }
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
  if (token !== state.requestToken || state.modalId !== modalId
      || state.runId !== runId || state.issueId !== issueId) return;
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

async function finishAnalysisTimeline(expectedModalId, expectedRunId, expectedIssueId) {
  var state = analysisTimelineState;
  if (state.modalId !== expectedModalId || state.runId !== expectedRunId
      || state.issueId !== expectedIssueId) return;
  if (!state.runId || !document.getElementById(state.modalId)) { stopAnalysisTimelinePolling(); return; }
  var modalId = state.modalId;
  try {
    var payload = await api('/api/redmine-agent/daily-brief/runs/' + encodeURIComponent(state.runId)) || {};
    if (state.modalId !== expectedModalId || state.runId !== expectedRunId
        || state.issueId !== expectedIssueId) return;
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
    // 终态前重读完整会话：这会作废任何在途的实时
    // tail，避免最后几个 message 在 events 先到终态时丢失。
    await loadAnalysisTimelineSessionHistory();
    if (state.modalId !== expectedModalId || state.runId !== expectedRunId
        || state.issueId !== expectedIssueId
        || !document.getElementById(modalId)) return;
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
    if (state.modalId !== expectedModalId || state.runId !== expectedRunId
        || state.issueId !== expectedIssueId) return;
    // 终态详情拉取失败：不丢弃已渲染的时间线，就地收敛为确定的结束
    // 视图并保留「查看报告」重试入口，避免标题停留在「正在分析」、
    // 停止按钮残留（列表轮询仍会自然收敛行状态）。
    var finalBox = document.getElementById(modalId + '-timeline');
    if (finalBox) finalBox.insertAdjacentHTML('afterbegin',
      '<div class="daily-brief-warning">分析已结束，报告详情读取失败，可点击「查看报告」重试。</div>');
    analysisTimelineMarkEnded('报告读取失败');
  }
  if (state.modalId === expectedModalId && state.runId === expectedRunId
      && state.issueId === expectedIssueId) haltAnalysisTimelineLoop();
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
