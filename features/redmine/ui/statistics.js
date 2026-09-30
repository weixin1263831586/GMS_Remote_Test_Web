// Redmine statistics; loaded by page.html in dependency order.
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
