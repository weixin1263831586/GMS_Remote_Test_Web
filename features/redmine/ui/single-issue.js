// Single-issue diagnosis history, task controls and device selection.
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

// 单号分析与每日晨报行共用的实机取证六态标签 / 证据门禁提示 / 系统权威
// 元数据 / 证据质量提示已拆至 daily-brief-diagnosis.js（全局审查 P0：
// size ratchet 反弹，诊断呈现域继续拆分回落；运行时全局函数，加载顺序
// 与其他辅助模块一致）。

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
    return previousNotice + dailyBriefSystemMetadata(displayedResult)
      + dailyBriefQualityNotice(displayedResult) + dailyBriefEvidenceGateNotice(displayedResult)
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
        + '" data-a1="' + esc(issue.issue_id) + '" data-a2="' + esc(subject) + '" data-prevent>会话回放</button>'
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
