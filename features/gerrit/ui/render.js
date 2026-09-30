async function loadPendingReviewQueue(owner, refresh) {
  if (!owner) return null;
  try {
    const url = '/api/gerrit-dashboard/review-queue?owner=' + encodeURIComponent(owner) + '&refresh=' + (refresh ? 'true' : 'false');
    const data = await api(url);
    return {count: (typeof data.count === 'number') ? data.count : 0, items: data.items || []};
  } catch (e) { return null; }
}
function renderDashboard(data, pendingMyReview) {
  const isPersonal = currentTab === 'personal';
  const controls = isPersonal ? personalControlsHtml() : '';
  const title = isPersonal ? 'Gerrit 个人提交汇总' : 'Gerrit 提交汇总';
  const s = data.summary || {};
  const reviewCount = (pendingMyReview && typeof pendingMyReview.count === 'number') ? pendingMyReview.count : null;
  // 汇总卡片按处理优先级排序：待我评审 → 待评审 → 未合并 → 已合并
  // → 历史提交 → 已废弃（待我评审仅个人页有数据时出现）。
  const cards = [];
  if (isPersonal && reviewCount != null) {
    cards.push({label:'待我评审', value:reviewCount, className:'warn clickable-stat', clickSection:'sec-review-of-me'});
  }
  cards.push(
    {label:'待评审', value:s.pending_review_count || 0, className:'bad clickable-stat', clickSection:'sec-pending-review'},
    {label:'未合并', value:s.open_count || 0, className:'warn clickable-stat', clickSection:'sec-open'},
    {label:'已合并', value:s.merged_count || 0, className:'ok clickable-stat', clickSection:'sec-merged'},
    {label:'历史提交', value:s.total_count || 0},
    {label:'已废弃', value:s.abandoned_count || 0, className:'clickable-stat', clickSection:'sec-abandoned'}
  );
  return '<section class="list-section">'
    + renderSummaryHeader(title, controls)
    + renderCards(cards) + '</section><div class="trend-grid">'
    + renderTrend('每天提交', (data.trends || {}).daily || [], 'date', 'personal_daily')
    + renderTrend('每周提交', (data.trends || {}).weekly || [], 'week', 'personal_weekly')
    + renderTrend('每月提交', (data.trends || {}).monthly || [], 'month', 'personal_monthly')
    + renderTrend('每年提交', (data.trends || {}).yearly || [], 'year', 'personal_yearly')
    + '</div>' + renderReviewOfMeList(pendingMyReview)
    + renderLists(data.lists || {});
}
function renderReviewOfMeList(pendingMyReview) {
  if (currentTab !== 'personal' || !pendingMyReview || !Array.isArray(pendingMyReview.items)) return '';
  return renderChangeList('待我评审', pendingMyReview.items, 'sec-review-of-me');
}
function renderCards(cards) {
  // 跳转锚点走 act-bridge 委托：data-click 只放 handler 名 + data-a0
  // 放 section id（此前把整条调用表达式塞进 data-click，按名查表
  // 查不到函数，点击静默失效）。
  return '<div class="stats-grid">' + cards.map(card => '<div class="stat-card ' + esc(card.className || '') + '"' + (card.clickSection ? ' data-click="scrollToSection" data-a0="' + esc(card.clickSection) + '"' : '') + '><div class="value">' + esc(card.value) + '</div><div class="label">' + esc(card.label) + '</div></div>').join('') + '</div>';
}
function renderTrend(title, rows, key, chartKey) {
  chartKey = chartKey || title;
  const filtered = filterTrendItems(rows || [], key, chartKey);
  const sorted = filtered.slice().sort(function(a, b) {
    return String(b[key] || '').localeCompare(String(a[key] || ''));
  });
  const max = Math.max(1, ...sorted.map(x => Number(x.count || 0)));
  const bars = sorted.map(row => {
    const count = Number(row.count || 0);
    const label = row[key];
    const clickable = count > 0 ? ' style="cursor:pointer" data-click="showGerritTrendDetail" data-a0="' + esc(key) + '" data-a1="' + esc(String(label)) + '" title="点击查看该时段提交明细"' : '';
    return '<div class="bar-row"' + clickable + '><div class="bar-label">' + esc(label) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + Math.max(5, Math.round(count * 100 / max)) + '%"></div></div><div class="bar-count">' + esc(count) + '</div></div>';
  }).join('') || '<div class="muted">无数据</div>';
  const range = trendDateRange(chartKey);
  const tip = (range.start || range.end) ? ('范围: ' + (range.start || '不限') + ' 至 ' + (range.end || '不限')) : '设置统计日期范围';
  return '<section class="trend-panel"><div class="trend-title-row"><h2>' + esc(title) + '</h2><button class="trend-start-btn" data-click="setTrendStartDate" data-a0="' + esc(chartKey) + '" data-a1="' + esc(title) + '" title="' + esc(tip) + '">⚙</button></div><div class="trend-body">' + bars + '</div></section>';
}
function trendDateRange(chartKey) {
  return ((config.chart_date_ranges || {})[chartKey] || {});
}
function trendStartDate(chartKey) {
  return String((trendDateRange(chartKey) || {}).start || '').trim();
}
function trendEndDate(chartKey) {
  return String((trendDateRange(chartKey) || {}).end || '').trim();
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
function setTrendStartDate(chartKey, title) {
  pendingTrendChartKey = chartKey || '';
  document.getElementById('trendStartModalTitle').textContent = title + ' 日期范围';
  document.getElementById('trendStartDateInput').value = trendStartDate(chartKey);
  document.getElementById('trendEndDateInput').value = trendEndDate(chartKey);
  showModal('trendStartModal');
  setTimeout(function() {
    var input = document.getElementById('trendStartDateInput');
    if (input) input.focus();
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
  var ranges = Object.assign({}, config.chart_date_ranges || {});
  if (start || end) ranges[chartKey] = Object.assign({}, start ? {start:start} : {}, end ? {end:end} : {});
  else delete ranges[chartKey];
  try {
    config = await api('/api/gerrit-dashboard/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({chart_date_ranges:ranges})});
    hideTrendStartModal();
    if (currentTab === 'department') await loadDepartment(false);
    else await loadPersonal(false);
  } catch (e) { notifyUser('保存统计日期失败', e.message, 'error'); }
}
function renderLists(lists, prefix) {
  const idPrefix = prefix || 'sec';
  return renderChangeList('待评审', lists.pending_review || [], idPrefix + '-pending-review')
    + renderChangeList('未合并', lists.open || [], idPrefix + '-open')
    + renderChangeList('已合并', lists.merged || [], idPrefix + '-merged')
    + renderChangeList('已废弃', lists.abandoned || [], idPrefix + '-abandoned');
}
function renderChangeList(title, rows, sectionId) {
  return '<section class="list-section" id="' + esc(sectionId || '') + '"><h2>' + esc(title) + '</h2><div class="wrap"><table><thead><tr><th>变更</th><th>项目</th><th>分支</th><th>状态</th><th>更新时间</th></tr></thead><tbody>'
    + (rows || []).map(renderStatsRow).join('')
    + ((rows || []).length ? '' : '<tr><td colspan="5" class="muted">无记录</td></tr>')
    + '</tbody></table></div></section>';
}
function renderDepartmentUsers(users) {
  if (!users.length) return '';
  // 成员按姓名（中文拼音序）排序；姓名缺失则回退邮箱前缀，保持稳定顺序。
  const sorted = users.slice().sort(function(a, b) {
    const na = String(a.name || (a.owner || '').split('@')[0] || '').trim();
    const nb = String(b.name || (b.owner || '').split('@')[0] || '').trim();
    return na.localeCompare(nb, 'zh');
  });
  const rows = sorted.map(user => {
    const s = user.summary || {};
    const extra = user.summary_extra || {};
    const owner = String(user.owner || '');
    const name = String(user.name || owner.split('@')[0] || '-');
    // 整行可点击跳个人看板；操作列 stopPropagation 避免点「移出」也触发跳转。
    const clickAttr = owner ? ' style="cursor:pointer" data-click="viewMemberInPersonal" data-a0="' + esc(owner) + '"' : '';
    return '<tr' + clickAttr + '>'
      + '<td><strong>' + esc(name) + '</strong></td>'
      + '<td class="muted">' + esc(owner || '-') + '</td>'
      + '<td>' + esc(s.total_count || 0) + '</td>'
      + '<td>' + esc(s.merged_count || 0) + '</td>'
      + '<td>' + esc(s.open_count || 0) + '</td>'
      + '<td>' + esc(s.pending_review_count || 0) + '</td>'
      + '<td>' + esc(extra.pending_review_of_me_count || 0) + '</td>'
      + '<td>' + esc(user.error || '') + '</td>'
      + '<td data-click="_actStopPropagation2"><button class="secondary dept-action-btn" data-click="removeDepartmentOwner" data-a0="' + esc(owner) + '" data-r1="el">移出</button></td>'
      + '</tr>';
  }).join('');
  return '<section class="list-section"><div class="dept-section-head"><h2>成员汇总</h2><span class="muted dept-hint">点击任意成员行可在个人看板查看其详细提交。</span></div><div class="dept-table-wrap"><table class="dept-table"><thead><tr><th>姓名</th><th>邮箱</th><th>历史提交</th><th>已合并</th><th>未合并</th><th>待评审</th><th>待成员评审</th><th>错误</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table></div></section>';
}
// 按部门中文数字编号排序，无编号部门置后。
// 用码点而非 localeCompare：拼音序会把“系统二部”排到“系统一部”前面（èr<yī）。
const _DEPT_CN_NUM = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10};
function departmentSortKey(name) {
  name = String(name || '');
  const m = name.match(/^(.*?)([一二三四五六七八九十])(.*)$/);
  if (m) return [m[1], _DEPT_CN_NUM[m[2]] || 0, m[3]];
  return [name, Infinity, ''];
}
function departmentOptionsHtml(selectedId, includeAll) {
  const seen = {};
  const departments = [];
  (config.department_profiles || []).forEach(function(p) {
    if (!p || !p.id) return;
    seen[p.id] = true;
    departments.push(p);
  });
  (config.redmine_departments || []).forEach(function(p) {
    if (!p || !p.id || seen[p.id]) return;
    seen[p.id] = true;
    departments.push({id:p.id, name:p.name || p.id, source:'redmine'});
  });
  return departments.filter(function(p) {
    return includeAll || p.id !== 'all';
  }).sort(function(a, b) {
    // “全部部门”置顶；其余按 departmentSortKey 的码点序。
    if (a.id === 'all') return -1;
    if (b.id === 'all') return 1;
    const ka = departmentSortKey(a.name || a.id);
    const kb = departmentSortKey(b.name || b.id);
    return (ka < kb) ? -1 : (ka > kb) ? 1 : 0;
  }).map(function(p) {
    return '<option value="' + esc(p.id) + '" data-name="' + esc(p.name || p.id) + '"' + (p.id === selectedId ? ' selected' : '') + '>' + esc((p.name || p.id) + (p.source === 'redmine' ? ' / Redmine' : '')) + '</option>';
  }).join('');
}
function populateDepartmentSelect(selectId, selectedId, includeAll) {
  const select = document.getElementById(selectId);
  if (!select) return;
  select.innerHTML = departmentOptionsHtml(selectedId || '', includeAll) || '<option value="">暂无部门</option>';
}
// 成员先按部门排序，再按姓名拼音排序。
function compareMembers(a, b) {
  const da = a.department && String(a.department).trim();
  const db = b.department && String(b.department).trim();
  if (!!da !== !!db) return da ? -1 : 1;
  if (da) {
    const ka = departmentSortKey(da), kb = departmentSortKey(db);
    for (let i = 0; i < ka.length; i++) {
      if (ka[i] < kb[i]) return -1;
      if (ka[i] > kb[i]) return 1;
    }
  }
  return String(a.name || a.owner || a.id).trim().localeCompare(
    String(b.name || b.owner || b.id).trim(), 'zh');
}
function personalPlaceholderHtml(message) {
  return '<section class="list-section">' + renderSummaryHeader('Gerrit 个人提交汇总', personalControlsHtml(), '')
    + placeholderHtml(message) + '</section>';
}
function personalControlsHtml() {
  const profiles = config.personal_profiles || [];
  const orderedProfiles = profiles.slice().sort(compareMembers);
  // owner 取选中 profile 的邮箱，不回退到第一条。
  const selectedProfile = findPersonalProfile(currentPersonalProfileId) || {};
  const selectedProfileId = selectedProfile.id || '';
  const ownerValue = requestedOwner || selectedProfile.owner || (document.getElementById('owner') ? document.getElementById('owner').value : '') || config.default_owner || '';
  return '<div class="select-with-add"><select id="personalProfile" data-change="onPersonalProfileChange"><option value="">按邮箱查询</option>' + orderedProfiles.map(p => '<option value="' + esc(p.id) + '"' + (p.id === selectedProfileId ? ' selected' : '') + '>' + esc((p.department ? p.department + ' / ' : '') + (p.name || p.owner || p.id)) + '</option>').join('') + '</select><button class="select-add-btn" type="button" data-click="showAddPersonalModal" title="添加成员">＋</button></div>'
    + '<input id="owner" value="' + esc(ownerValue) + '" placeholder="owner 邮箱" data-input="onPersonalOwnerInput">';
}
function departmentControlsHtml(profileId) {
  return '<div class="select-with-add"><select id="departmentProfile" data-change="loadDepartmentForced" style="min-width:160px">' + departmentOptionsHtml(profileId, true) + '</select><button class="select-add-btn" type="button" data-click="showAddDepartmentModal" data-a0="departmentProfile" title="添加部门">＋</button></div>'
    + '<button class="secondary" data-click="syncRedmineMembers" data-r0="el">同步成员</button>';
}
function renderDepartmentDashboard(data) {
  const profile = data.profile || {};
  currentDepartmentProfileId = profile.id || currentDepartmentProfileId || '';
  const s = data.summary || {};
  const header = '<section class="list-section">' + renderSummaryHeader((profile.name || '部门') + ' Gerrit 提交汇总', departmentControlsHtml(currentDepartmentProfileId), '成员: ' + ((data.users || []).length))
    + renderCards([
      {label:'历史提交', value:s.total_count || 0},
      {label:'已合并', value:s.merged_count || 0, className:'ok'},
      {label:'未合并', value:s.open_count || 0, className:'warn'},
      {label:'待评审', value:s.pending_review_count || 0, className:'bad'},
      {label:'已废弃', value:s.abandoned_count || 0}
    ]) + '</section>';
  const trends = '<div class="trend-grid">' + renderTrend('每天提交', (data.trends || {}).daily || [], 'date', 'department_daily') + renderTrend('每周提交', (data.trends || {}).weekly || [], 'week', 'department_weekly') + renderTrend('每月提交', (data.trends || {}).monthly || [], 'month', 'department_monthly') + renderTrend('每年提交', (data.trends || {}).yearly || [], 'year', 'department_yearly') + '</div>';
  return header + trends + renderDepartmentUsers(data.users || []);
}
function showAddPersonalModal() {
  document.getElementById('addPersonalName').value='';
  document.getElementById('addPersonalOwner').value='';
  const selectedDepartment = (currentTab === 'department' && currentDepartmentProfileId && currentDepartmentProfileId !== 'all') ? currentDepartmentProfileId : '';
  populateDepartmentSelect('addPersonalDepartment', selectedDepartment, false);
  showModal('addPersonalModal');
  document.getElementById('addPersonalName').focus();
}
function hideAddPersonalModal() { hideModal('addPersonalModal'); }
async function savePersonalProfile() {
  try {
    const departmentSelect = document.getElementById('addPersonalDepartment');
    const departmentOption = departmentSelect && departmentSelect.selectedOptions ? departmentSelect.selectedOptions[0] : null;
    const result = await api('/api/gerrit-dashboard/personal-profiles', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:document.getElementById('addPersonalName').value, owner:document.getElementById('addPersonalOwner').value, department_id:departmentSelect ? departmentSelect.value : '', department:departmentOption ? (departmentOption.dataset.name || departmentOption.textContent || '') : ''})});
    config = result.dashboard || config;
    currentPersonalProfileId = (result.profile || {}).id || currentPersonalProfileId;
    if ((result.profile || {}).department_id) currentDepartmentProfileId = result.profile.department_id;
    hideAddPersonalModal();
    await init();
    if (currentTab === 'department') await loadDepartment(true);
  } catch (e) { notifyUser('保存成员失败', e.message, 'error'); }
}
function showAddDepartmentModal(targetSelectId) {
  pendingDepartmentTargetSelect = targetSelectId || 'departmentProfile';
  document.getElementById('addDepartmentName').value='';
  document.getElementById('addDepartmentId').value='';
  document.getElementById('addDepartmentOwners').value='';
  showModal('addDepartmentModal');
  document.getElementById('addDepartmentName').focus();
}
function hideAddDepartmentModal() { hideModal('addDepartmentModal'); }
async function saveDepartmentProfile() {
  try {
    const result = await api('/api/gerrit-dashboard/department-profiles', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:document.getElementById('addDepartmentName').value, id:document.getElementById('addDepartmentId').value, owners:document.getElementById('addDepartmentOwners').value})});
    config = result.dashboard || config;
    currentDepartmentProfileId = (result.profile || {}).id || currentDepartmentProfileId;
    hideAddDepartmentModal();
    if (pendingDepartmentTargetSelect === 'addPersonalDepartment') {
      populateDepartmentSelect('addPersonalDepartment', (result.profile || {}).id || '', false);
    } else {
      await init();
      switchTab('department');
    }
  } catch (e) { notifyUser('添加部门看板失败', e.message, 'error'); }
}
function showAddDepartmentOwnerModal() { document.getElementById('addDepartmentOwnerValue').value=''; showModal('addDepartmentOwnerModal'); }
function hideAddDepartmentOwnerModal() { hideModal('addDepartmentOwnerModal'); }
async function saveDepartmentOwner() {
  try {
    const profileSelect = document.getElementById('departmentProfile');
    const profileId = currentDepartmentProfileId || (profileSelect ? profileSelect.value : '') || '';
    const result = await api('/api/gerrit-dashboard/department-profiles/' + encodeURIComponent(profileId) + '/owners', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({owner:document.getElementById('addDepartmentOwnerValue').value})});
    config = result.dashboard || config;
    hideAddDepartmentOwnerModal();
    await loadDepartment(true);
  } catch (e) { notifyUser('添加部门成员失败', e.message, 'error'); }
}
async function removeDepartmentOwner(owner, btn) {
  const profileSelect = document.getElementById('departmentProfile');
  const profileId = currentDepartmentProfileId || (profileSelect ? profileSelect.value : '') || '';
  if (!profileId || !owner) return;
  if (profileId === 'all') {
    notifyUser('无法移出成员', '请先选择具体部门，再移出成员。', 'warning');
    return;
  }
  if (!await confirmUserAction(
    '移出部门成员',
    '确认从当前部门移出 ' + owner + '？'
  )) return;
  if (btn) btn.disabled = true;
  try {
    const result = await api('/api/gerrit-dashboard/department-profiles/' + encodeURIComponent(profileId) + '/owners', {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({owner:owner})});
    config = result.dashboard || config;
    await loadDepartment(true);
  } catch (e) {
    notifyUser('移出部门成员失败', e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}
async function syncRedmineMembers(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '同步中'; }
  try {
    config = await api('/api/gerrit-dashboard/sync-redmine-members', {method:'POST'});
    await loadDepartment(true);
  } catch (e) {
    notifyUser('同步成员失败', e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '同步成员'; }
  }
}
function changeUrl(item) {
  if (item.url) return item.url;
  const id = item._number || item.number || item.id || '';
  const base = String(config.base_url || '').replace(new RegExp('/+$'), '');
  return base && id ? base + '/c/' + encodeURIComponent(id) : '';
}
function changePatchset(item) {
  return String(item.patchset || item.patch_set || item.currentPatchSet?.number || item.current_patch_set || '');
}
function selectGerritWorkspaceChange(changeId, patchset) {
  const id = String(changeId || '').replace(/^#/, '').trim();
  if (!id || id === '-') return;
  const ps = String(patchset || '').trim();
  gerritWorkspaceContext = Object.assign({}, gerritWorkspaceContext, {
    gerrit_change_id: id,
    gerrit_patchset: ps
  });
  window.GmsEmbeddedWorkspace?.update({
    gerrit_change_id: id,
    gerrit_patchset: ps,
    origin_page: 'gerrit'
  });
}
function navigateFromGerritChange(page, changeId, patchset) {
  const id = String(changeId || '').replace(/^#/, '').trim();
  const ps = String(patchset || '').trim();
  selectGerritWorkspaceChange(id, ps);
  window.GmsEmbeddedWorkspace?.navigate(page, {
    gerrit_change_id: id,
    gerrit_patchset: ps,
    origin_page: 'gerrit'
  });
}
async function applyGerritWorkspaceContext(next, navigate) {
  gerritWorkspaceContext = Object.assign({}, gerritWorkspaceContext, next || {});
  const id = String(gerritWorkspaceContext.gerrit_change_id || '').replace(/^#/, '').trim();
  if (!navigate || !id || pendingWorkspaceChangeId === id) return;
  pendingWorkspaceChangeId = id;
  try {
    await gerritInitPromise;
    const query = document.getElementById('query');
    if (query) query.value = 'change:' + id;
    switchTab('query');
    await loadChanges();
    const row = document.querySelector('[data-gerrit-change-id="' + CSS.escape(id) + '"]');
    if (row) row.scrollIntoView({behavior:'smooth', block:'center'});
  } finally {
    pendingWorkspaceChangeId = '';
  }
}
function renderStatsRow(item) {
  const url = changeUrl(item);
  const id = item.number || item._number || item.id || '-';
  const patchset = changePatchset(item);
  const subject = item.subject || '-';
  return '<tr data-gerrit-change-id="' + esc(id) + '" data-gerrit-patchset="' + esc(patchset) + '"><td><div class="change-title">' + (url ? '<a data-gerrit-select href="' + esc(url) + '" target="_blank">#' + esc(id) + '</a>' : '#' + esc(id)) + '<span title="' + esc(subject) + '">' + esc(subject) + '</span></div></td><td>' + esc(item.project || '-') + '</td><td>' + esc(item.branch || '-') + '</td><td>' + esc(item.status || '-') + '</td><td>' + esc(item.updated || item.lastUpdated || item.updated_at || '-') + '</td></tr>';
}
function renderRow(item) {
  const url = changeUrl(item);
  const id = item._number || item.number || item.id || item.changeId || '-';
  const patchset = changePatchset(item);
  const subject = item.subject || '-';
  // 缓存原始 change，供「存为Wiki」按钮按 change id 取回
  if (id !== '-') {
    window.__changeCache = window.__changeCache || {};
    window.__changeCache[String(id)] = item;
  }
  return '<tr data-gerrit-change-id="' + esc(id) + '" data-gerrit-patchset="' + esc(patchset) + '"><td><div class="change-title">' + (url ? '<a data-gerrit-select href="' + esc(url) + '" target="_blank">#' + esc(id) + '</a>' : '#' + esc(id)) + '<span title="' + esc(subject) + '">' + esc(subject) + '</span>' + '<button class="gerrit-wiki-btn" title="把该补丁存入 Wiki「Gerrit补丁说明」分类，并建立外链" data-click="saveChangeToWiki" data-a0="' + esc(String(id)) + '" data-prevent data-stop>📥Wiki</button><button class="gerrit-wiki-btn" data-gerrit-nav-page="automation" title="带此 Change 打开 GMS ATS">⚙️ATS</button><button class="gerrit-wiki-btn" data-gerrit-nav-page="reports" title="带此 Change 打开报告页">📊报告</button>' + '</div></td><td>' + esc(item.project || '-') + '</td><td>' + esc(item.branch || '-') + '</td><td>' + esc(item.status || '-') + '</td><td>' + esc((item.owner || {}).name || (item.owner || {}).email || '-') + '</td><td>' + esc(item.updated || item.lastUpdated || item.createdOn || item.created || '-') + '</td></tr>';
}
document.addEventListener('click', function(event) {
  const row = event.target.closest('[data-gerrit-change-id]');
  if (!row) return;
  const pageButton = event.target.closest('[data-gerrit-nav-page]');
  if (pageButton) {
    event.preventDefault();
    event.stopPropagation();
    navigateFromGerritChange(pageButton.dataset.gerritNavPage, row.dataset.gerritChangeId, row.dataset.gerritPatchset);
    return;
  }
  if (event.target.closest('[data-gerrit-select]')) {
    selectGerritWorkspaceChange(row.dataset.gerritChangeId, row.dataset.gerritPatchset);
  }
});
window.addEventListener('gms:embedded-workspace', function(event) {
  applyGerritWorkspaceContext(
    event.detail && event.detail.context || {},
    event.detail && event.detail.type === 'workspace-context-navigate'
  ).catch(function(error) { notifyUser('打开 Gerrit Change 失败', error.message, 'error'); });
});
