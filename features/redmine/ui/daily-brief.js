// Redmine daily-brief; loaded by page.html in dependency order.
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
    + '<div class="daily-brief-metric"><span>证据已验证</span><b>' + esc(counts.evidence_verified || 0) + '</b></div>'
    + '<div class="daily-brief-metric"><span>证据部分闭环</span><b>' + esc(counts.evidence_partial || 0) + '</b></div>'
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
      var errorLabel = ({ schema_mismatch: '返回格式不兼容', invalid_ai_output: 'AI 返回无法解析', evidence_gate_failed: '证据门禁未通过', kkagent_error: '分析服务异常', interrupted: '分析进程被中断', timeout: '分析超时', llm_timeout: '模型服务超时', provider_overloaded: '模型服务繁忙', kkagent_unavailable: '分析服务不可用', oversized_output: '输出超限被终止', repair_failed: '同会话自动修复失败', max_turns: '步数预算耗尽' })[etype] || etype;
      return '<span class="daily-brief-state failed" title="' + esc(err) + '">❌ 分析失败'
        + (errorLabel ? ' · ' + esc(errorLabel) : '') + '</span>';
    }
    // run 级取消会把条目收敛为 cancelled（服务端双重保证），行内给出与
    // 单号分析列表一致的终态标签；stale 为预留状态，防御性标注。
    if (issue.status === 'cancelled') return '<span class="daily-brief-state">■ 已停止</span>';
    if (issue.status === 'stale') return '<span class="daily-brief-state">⚠ 旧批次结论，可能已被更新分析取代</span>';
    // 模型自评与实际取证完成情况分别展示。
    var labels = [];
    var qualityLabel = ({ verified: '证据已验证', partial: '证据部分闭环', insufficient: '证据不足' })[r.evidence_quality];
    labels.push('执行已完成');
    if (qualityLabel) labels.push(qualityLabel);
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
