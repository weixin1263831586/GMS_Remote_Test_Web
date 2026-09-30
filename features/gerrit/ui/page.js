// act-bridge 委托目标（替代历史 inline handler）。
function loadDepartmentForced(){
  // 切换部门强制重拉（refresh=true）；刷新按钮同步进入忙碌状态。
  withRefreshButtonBusy(function() { return loadDepartment(true); });
}
function _actStopPropagation2(event){event.stopPropagation();}

let config = {dashboard_profiles: [], personal_profiles: [], department_profiles: [], default_owner: ''};
let currentTab = 'personal';
let currentPersonalProfileId = '';
let currentDepartmentProfileId = '';
let requestedOwner = '';
// 趋势明细点击上下文：当前看板作用的 owner（个人）或 owners（部门）
let trendOwner = '';
let trendOwners = [];
let trendScope = '';
let trendProfileId = '';
let pendingDepartmentTargetSelect = 'departmentProfile';
let pendingTrendChartKey = '';
let gerritWorkspaceContext = {};
let pendingWorkspaceChangeId = '';
const gerritDashboardRequestGeneration = {personal: 0, department: 0};
const esc = window.escapeHtml;
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
  if (!data.success) throw new Error(data.error || data.detail || '请求失败');
  return data.data || data;
}
async function init() {
  config = await api('/api/gerrit-dashboard/config');
  fillSelect('profile', config.dashboard_profiles || []);
  fillSelect('personalProfile', config.personal_profiles || []);
  fillSelect('departmentProfile', config.department_profiles || []);
  restoreGerritProfileState();
  var validTabs = {'personal':1,'department':1,'query':1};
  if (!validTabs[currentTab]) currentTab = 'personal';
  document.querySelectorAll('.tab').forEach(x => {
    var active = x.dataset.tab === currentTab;
    x.classList.toggle('active', active);
    x.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('.tab-content').forEach(x => x.classList.toggle('active', x.id === 'tab-' + currentTab));
  const first = (config.dashboard_profiles || [])[0] || {};
  var queryInput = document.getElementById('query');
  if (queryInput && !queryInput.value) queryInput.value = first.query || 'status:open';
  // 深链恢复到非第一条 profile 时，避免标题姓名与 owner 邮箱首次绘制错位。
  const person = findPersonalProfile(currentPersonalProfileId) || (config.personal_profiles || [])[0] || {};
  const ownerInput = document.getElementById('owner');
  if (ownerInput) ownerInput.value = person.owner || config.default_owner || '';
  if (currentTab === 'personal') await loadPersonal(false);
  else if (currentTab === 'department') await loadDepartment(false);
  else await loadChanges();
}
function showSettings() {
  document.getElementById('settingBaseUrl').value = config.base_url || '';
  document.getElementById('settingRestUser').value = config.rest_username || '';
  document.getElementById('settingRestPass').value = '';
  document.getElementById('settingSshHost').value = config.ssh_host || '';
  document.getElementById('settingSshUser').value = config.ssh_user || '';
  document.getElementById('settingSshPort').value = config.ssh_port || 29418;
  document.getElementById('settingSshIdentity').value = config.ssh_identity_file || '';
  document.getElementById('settingDefaultOwner').value = config.default_owner || '';
  document.getElementById('settingQueryPageSize').value = ((config.defaults || {}).query_page_size || 500);
  document.getElementById('settingMaxHistory').value = ((config.defaults || {}).max_history_changes || 0);
  showModal('settingsModal');
}
function hideSettings() {
  hideModal('settingsModal');
}
async function saveSettings() {
  const body = {
    base_url: document.getElementById('settingBaseUrl').value,
    rest_username: document.getElementById('settingRestUser').value,
    rest_password: document.getElementById('settingRestPass').value,
    ssh_host: document.getElementById('settingSshHost').value,
    ssh_user: document.getElementById('settingSshUser').value,
    ssh_port: document.getElementById('settingSshPort').value,
    ssh_identity_file: document.getElementById('settingSshIdentity').value,
    default_owner: document.getElementById('settingDefaultOwner').value,
    department_defaults: Object.assign({}, config.defaults || {}, {
      query_page_size: Number(document.getElementById('settingQueryPageSize').value || 500),
      max_history_changes: Number(document.getElementById('settingMaxHistory').value || 0)
    })
  };
  try {
    config = await api('/api/gerrit-dashboard/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    hideSettings();
    await init();
  } catch (e) {
    notifyUser('保存设置失败', e.message, 'error');
  }
}
function fillSelect(id, items) {
  const select = document.getElementById(id);
  if (!select) return;
  select.innerHTML = (items || []).map(p => '<option value="' + esc(p.id) + '">' + esc(p.name || p.owner || p.id) + '</option>').join('');
}
function scrollToSection(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const header = document.querySelector('header');
  const offset = (header ? header.getBoundingClientRect().height : 0) + 14;
  window.scrollTo({top: Math.max(0, el.getBoundingClientRect().top + window.pageYOffset - offset), behavior:'smooth'});
}
// Modal 生命周期统一走共享控制器（web/static/js/embedded-ui/modal-controller.js）：
// 栈/z-index/inert/aria/Escape/backdrop/focus trap 单一所有者，页面只保留
// 全局函数别名——运行时冒烟测试与 data-click 契约依赖这些名字。
function showModal(id) {
  window.EmbeddedModalController.open(id);
}
function hideModal(id) {
  window.EmbeddedModalController.close(id);
}
function notifyUser(title, message, level) {
  level = level || 'info';
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({type:'gms-dashboard-notification', title:title, message:message, level:level}, window.location.origin);
    }
  } catch (_) {}
  var old = document.getElementById('gerrit-local-toast');
  if (old) old.remove();
  var toast = document.createElement('div');
  toast.id = 'gerrit-local-toast';
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
function saveGerritProfileState() {
  try {
    window.sessionStorage.setItem('gerritCurrentTab', currentTab || 'personal');
    window.sessionStorage.setItem('gerritPersonalProfileId', currentPersonalProfileId || '');
    window.sessionStorage.setItem('gerritDepartmentProfileId', currentDepartmentProfileId || '');
    window.sessionStorage.setItem('gerritQueryProfileId', document.getElementById('profile') ? document.getElementById('profile').value : '');
    window.sessionStorage.setItem('gerritQueryValue', document.getElementById('query') ? document.getElementById('query').value : '');
  } catch(_) {}
  var url = new URL(window.location.href);
  if (currentTab && currentTab !== 'personal') url.searchParams.set('tab', currentTab);
  else url.searchParams.delete('tab');
  if (currentTab === 'personal' && currentPersonalProfileId) url.searchParams.set('personal_profile', currentPersonalProfileId);
  else url.searchParams.delete('personal_profile');
  if (currentTab === 'department' && currentDepartmentProfileId) url.searchParams.set('dept_profile', currentDepartmentProfileId);
  else url.searchParams.delete('dept_profile');
  window.history.replaceState({}, '', url.toString());
}
function restoreGerritProfileState() {
  var q = new URLSearchParams(window.location.search);
  var t = q.get('tab');
  currentTab = t || currentTab || 'personal';
  currentPersonalProfileId = q.get('personal_profile') || currentPersonalProfileId || '';
  currentDepartmentProfileId = q.get('dept_profile') || currentDepartmentProfileId || '';
  try {
    if (!t) currentTab = window.sessionStorage.getItem('gerritCurrentTab') || currentTab;
    if (!currentPersonalProfileId) currentPersonalProfileId = window.sessionStorage.getItem('gerritPersonalProfileId') || '';
    if (!currentDepartmentProfileId) currentDepartmentProfileId = window.sessionStorage.getItem('gerritDepartmentProfileId') || '';
    // 默认显示“全部部门”（排在下拉最前），除非用户记忆或 URL 指定了其它部门。
    if (!currentDepartmentProfileId) currentDepartmentProfileId = 'all';
    var savedQueryProfile = window.sessionStorage.getItem('gerritQueryProfileId') || '';
    var savedQuery = window.sessionStorage.getItem('gerritQueryValue') || '';
    var profileEl = document.getElementById('profile');
    if (profileEl && savedQueryProfile) profileEl.value = savedQueryProfile;
    var queryEl = document.getElementById('query');
    if (queryEl && savedQuery) queryEl.value = savedQuery;
  } catch(_) {}
}
function switchTab(tab, force) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(x => {
    var active = x.dataset.tab === tab;
    x.classList.toggle('active', active);
    x.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('.tab-content').forEach(x => x.classList.toggle('active', x.id === 'tab-' + tab));
  saveGerritProfileState();
  // force=true 供成员跳转等场景绕过缓存直达目标数据；返回加载
  // promise 供调用方（act-bridge / viewMemberInPersonal）跟踪完成。
  if (tab === 'personal') return loadPersonal(force === true);
  if (tab === 'department') return loadDepartment(force === true);
  if (tab === 'query') return loadChanges();
  return Promise.resolve();
}

// ARIA tablist 方向键导航（←/→ 循环，Home/End 跳转）。
function gerritTabKeydown(event) {
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
function viewMemberInPersonal(owner) {
  owner = String(owner || '').trim();
  if (!owner) return;
  requestedOwner = owner;
  currentPersonalProfileId = '';
  // 跳转即按目标成员强制重拉（switchTab 默认吃缓存）并让刷新按钮
  // 进入忙碌态；立即回到个人看板顶部，不复用部门看板的滚动位置。
  withRefreshButtonBusy(function() { return switchTab('personal', true); });
  window.scrollTo({top: 0, behavior: 'smooth'});
}
// 将趋势标签转换为 Gerrit 日期查询范围。
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
    var y = parseInt(label, 10);
    if (!y) return null;
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
async function showGerritTrendDetail(granularity, label) {
  var owners = (trendOwners && trendOwners.length) ? trendOwners : (trendOwner ? [trendOwner] : []);
  if (!owners.length) { notifyUser('无法查看提交明细', '未获取到当前看板的 owner', 'warning'); return; }
  var range = trendLabelToDateRange(granularity, label);
  if (!range) { notifyUser('无法解析时段', label, 'warning'); return; }
  var modal = document.getElementById('trendDetailModal');
  var title = document.getElementById('trendDetailTitle');
  var body = document.getElementById('trendDetailBody');
  if (!modal || !title || !body) return;
  title.textContent = '提交明细：' + label + '（' + displayTrendRange(range) + '）';
  body.innerHTML = '<div class="muted">查询中…</div>';
  showModal('trendDetailModal');
  try {
    var params = new URLSearchParams({
      owners: owners.join(','),
      start: range[0],
      end: range[1],
      scope: trendScope || currentTab || '',
      profile_id: trendProfileId || (currentTab === 'department' ? currentDepartmentProfileId : currentPersonalProfileId) || ''
    });
    var data = await api('/api/gerrit-dashboard/changes-by-date?' + params.toString());
    var items = (data && data.items) || [];
    if (!items.length) { body.innerHTML = '<div class="muted">该时段无提交记录。</div>'; return; }
    body.innerHTML = '<div class="muted" style="margin-bottom:8px">共 ' + items.length + ' 条</div><div class="wrap"><table><thead><tr><th>变更</th><th>项目</th><th>分支</th><th>状态</th><th>Owner</th><th>更新时间</th></tr></thead><tbody>'
      + items.slice(0, 200).map(renderRow).join('') + '</tbody></table></div>';
  } catch (e) {
    body.innerHTML = '<span class="error">' + esc(e.message) + '</span>';
  }
}
function setRefreshButtonBusy(busy) {
  var btn = document.getElementById('refreshBtn');
  if (!btn) return;
  btn.disabled = busy;
  // 忙碌文案不带省略号：与空闲态宽度尽量接近，避免按钮尺寸跳动。
  btn.textContent = busy ? '⏳ 刷新中' : '🔄 刷新';
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
    if (currentTab === 'personal') await loadPersonal(true);
    else if (currentTab === 'department') await loadDepartment(true);
    else await loadChanges();
  });
}
function findPersonalProfile(id) {
  return (config.personal_profiles || []).find(x => x.id === id);
}
function onPersonalProfileChange() {
  const select = document.getElementById('personalProfile');
  const id = select ? select.value : '';
  currentPersonalProfileId = id;
  const profile = findPersonalProfile(id);
  if (profile && profile.owner) document.getElementById('owner').value = profile.owner;
  saveGerritProfileState();
  // 切换统计身份强制绕过缓存重拉；刷新按钮同步进入忙碌状态。
  withRefreshButtonBusy(function() { return loadPersonal(true); });
}
function onPersonalOwnerInput() {
  currentPersonalProfileId = '';
  requestedOwner = '';
  const select = document.getElementById('personalProfile');
  if (select) select.value = '';
  saveGerritProfileState();
}
async function loadPersonal(refresh) {
  if (currentTab !== 'personal') return;
  const content = document.getElementById('personalContent');
  const requestGeneration = ++gerritDashboardRequestGeneration.personal;
  const hadRenderedDashboard = content.dataset.loaded === 'true';
  content.setAttribute('aria-busy', 'true');
  const ownerInput = document.getElementById('owner');
  const profileSelect = document.getElementById('personalProfile');
  // 点击成员行跳转时走 requestedOwner：此时不要沿用看板上残留的 profile 下拉值，
  // 否则后端按残留 profile_id（陈伟等）匹配，导致显示成别人的个人看板。
  const profileId = requestedOwner ? '' : ((profileSelect ? profileSelect.value : '') || currentPersonalProfileId || '');
  const selectedProfile = requestedOwner ? null : findPersonalProfile(profileId);
  const owner = String(requestedOwner || (selectedProfile ? selectedProfile.owner : '') || (ownerInput ? ownerInput.value : '') || config.default_owner || '').trim();
  if (!requestedOwner && ownerInput && selectedProfile && selectedProfile.owner) ownerInput.value = selectedProfile.owner;
  currentPersonalProfileId = profileId;
  saveGerritProfileState();
  document.getElementById('personalStatus').textContent = '';
  try {
    if (!owner && !(config.personal_profiles || []).length) {
      content.innerHTML = personalPlaceholderHtml('尚未设置 Gerrit 统计邮箱。请填写 owner 邮箱后点击“刷新”，或点击“＋”添加成员；也可在连接设置中保存默认 Owner。');
      content.dataset.loaded = 'false';
      requestedOwner = '';
      trendOwner = '';
      trendOwners = [];
      trendProfileId = '';
      refreshConnBanner({});
      return;
    }
    if (!hadRenderedDashboard) content.innerHTML = personalPlaceholderHtml('⏳ 正在统计个人 Gerrit 提交...');
    const data = await api('/api/gerrit-dashboard/statistics/personal?profile_id=' + encodeURIComponent(profileId) + '&owner=' + encodeURIComponent(owner) + '&refresh=' + (refresh ? 'true' : 'false'));
    if (requestGeneration !== gerritDashboardRequestGeneration.personal) return;
    // owner 跳转后按返回的 profile 回填下拉选项。
    const resolvedProfile = (data.profile || {}).id || '';
    const resolvedOwner = String(data.owner || (data.profile || {}).owner || owner).trim();
    const reviewQueue = await loadPendingReviewQueue(resolvedOwner, refresh);
    if (requestGeneration !== gerritDashboardRequestGeneration.personal) return;
    currentPersonalProfileId = resolvedProfile;
    saveGerritProfileState();
    content.innerHTML = renderDashboard(data, reviewQueue);
    content.dataset.loaded = 'true';
    // 数据全空且查询报错时，提示用户检查 Gerrit 路由可达性。
    refreshConnBanner(data);
    trendOwner = resolvedOwner;
    trendOwners = resolvedOwner ? [resolvedOwner] : [];
    trendScope = 'personal';
    trendProfileId = resolvedProfile || profileId;
    requestedOwner = '';
  } catch (e) {
    if (requestGeneration !== gerritDashboardRequestGeneration.personal) return;
    requestedOwner = '';
    if (!hadRenderedDashboard) content.innerHTML = personalPlaceholderHtml('<span class="error">' + esc(e.message) + '</span>');
    notifyUser('Gerrit 个人统计失败', e.message, 'error');
    refreshConnBanner({error: e.message, source: ''});
  } finally {
    if (requestGeneration === gerritDashboardRequestGeneration.personal) content.setAttribute('aria-busy', 'false');
  }
}
async function loadDepartment(refresh) {
  if (currentTab !== 'department') return;
  const content = document.getElementById('departmentContent');
  const requestGeneration = ++gerritDashboardRequestGeneration.department;
  const hadRenderedDashboard = content.dataset.loaded === 'true';
  content.setAttribute('aria-busy', 'true');
  const profileSelect = document.getElementById('departmentProfile');
  const profileId = (profileSelect ? profileSelect.value : '') || currentDepartmentProfileId || '';
  currentDepartmentProfileId = profileId;
  saveGerritProfileState();
  document.getElementById('departmentStatus').textContent = '';
  if (!hadRenderedDashboard) content.innerHTML = placeholderHtml('⏳ 正在统计部门 Gerrit 提交...');
  try {
    const data = await api('/api/gerrit-dashboard/statistics/department?profile_id=' + encodeURIComponent(profileId) + '&refresh=' + (refresh ? 'true' : 'false'));
    if (requestGeneration !== gerritDashboardRequestGeneration.department) return;
    content.innerHTML = renderDepartmentDashboard(data);
    content.dataset.loaded = 'true';
    trendOwners = (data.users || []).map(function(u) { return u.owner; }).filter(Boolean);
    trendOwner = '';
    trendScope = 'department';
    trendProfileId = profileId;
  } catch (e) {
    if (requestGeneration !== gerritDashboardRequestGeneration.department) return;
    if (!hadRenderedDashboard) content.innerHTML = placeholderHtml('<span class="error">' + esc(e.message) + '</span>');
    notifyUser('Gerrit 部门统计失败', e.message, 'error');
  } finally {
    if (requestGeneration === gerritDashboardRequestGeneration.department) content.setAttribute('aria-busy', 'false');
  }
}
async function loadChanges() {
  if (currentTab !== 'query') return;
  const profileId = document.getElementById('profile').value || '';
  const query = document.getElementById('query').value || '';
  saveGerritProfileState();
  document.getElementById('status').textContent = '查询中...';
  try {
    const data = await api('/api/gerrit-dashboard/changes?profile_id=' + encodeURIComponent(profileId) + '&query=' + encodeURIComponent(query));
    document.getElementById('status').innerHTML = data.error ? '<span class="error">' + esc(data.error) + '</span>' : esc((data.source || 'unknown') + ' / 查询: ' + data.query + '，结果 ' + (data.items || []).length + ' 条');
    document.getElementById('rows').innerHTML = (data.items || []).map(renderRow).join('') || '<tr><td colspan="6" class="muted">无记录</td></tr>';
  } catch (e) {
    document.getElementById('status').innerHTML = '<span class="error">' + esc(e.message) + '</span>';
  }
}
function placeholderHtml(innerHtml) {
  return '<div class="muted" style="padding:40px;text-align:center">' + innerHtml + '</div>';
}
function renderSummaryHeader(title, controlsHtml, metaHtml) {
  return '<div class="dashboard-summary-header"><h2 class="dashboard-summary-title">' + esc(title) + '</h2>' + (controlsHtml ? '<div class="dashboard-summary-controls">' + controlsHtml + '</div>' : '') + (metaHtml ? '<div class="muted dashboard-summary-meta">' + metaHtml + '</div>' : '') + '</div>';
}
// 根据 Gerrit 查询结果刷新顶部连接告警条：只有明确网络/连接类错误才提示检查路由。
async function refreshConnBanner(data) {
  const banner = document.getElementById('connBanner');
  const msg = document.getElementById('connMsg');
  const detail = document.getElementById('connDetail');
  if (!banner) return;
  const errorText = [data && data.error, data && data.rest_error].filter(Boolean).join(' | ');
  const routeLike = /(timed?\s*out|timeout|unreachable|no route|network|connection refused|connection reset|name or service not known|could not resolve|无法|不可达|路由)/i.test(errorText);
  const authLike = /(401|403|auth|permission|forbidden|unauthorized|not permitted|认证|权限)/i.test(errorText);
  if (!errorText) { banner.classList.remove('show', 'ok'); return; }
  banner.classList.add('show'); banner.classList.remove('ok');
  if (routeLike && !authLike) {
    msg.textContent = '⚠️ Gerrit 查询失败，可能是服务器不可达，请检查路由。';
  } else if (authLike) {
    msg.textContent = '⚠️ Gerrit 查询失败，请检查账号、HTTP Password 或 SSH 权限。';
  } else {
    msg.textContent = '⚠️ Gerrit 查询失败，请检查查询条件或账号权限。';
  }
  detail.textContent = errorText;
}
// 嵌入主界面时复用测试界面的路由检查，保证弹框、校验、结果和
// 终端跳转都只有一份实现。独立打开 Gerrit 页时保留原有连通性探测。
async function checkGerritRoute() {
  try {
    if (window.parent !== window && typeof window.parent.checkRouting === 'function') {
      await window.parent.checkRouting();
      return;
    }
  } catch (e) {
    // Cross-origin embedding cannot access the parent implementation; use the
    // standalone Gerrit connectivity view below.
  }
  const body = document.getElementById('routeCheckBody');
  const banner = document.getElementById('connBanner');
  const msg = document.getElementById('connMsg');
  const detail = document.getElementById('connDetail');
  if (body) body.innerHTML = '<div class="muted">📡 正在探测 Gerrit 服务器可达性…</div>';
  showModal('routeCheckModal');
  try {
    const data = await api('/api/gerrit-dashboard/connectivity');
    if (body) {
      if (!data.configured) {
        body.innerHTML = '<div class="muted">Gerrit 未配置 ssh_host/base_url，请先在设置里填写。</div>';
        return;
      }
      const okLabel = function(ok, extra) { return ok ? '<span style="color:var(--ok)">✅ ' + (extra || '通') + '</span>' : '<span style="color:var(--bad)">❌ 不通</span>'; };
      var rows = [];
      rows.push('<div style="display:grid;gap:10px">');
      rows.push('<div style="font-size:15px;font-weight:600">' + (data.level === 'ok' ? '✅ ' : '⚠️ ') + esc(data.verdict || '') + '</div>');
      rows.push('<div style="border-top:1px solid var(--border);padding-top:10px;display:grid;gap:8px">');
      rows.push('<div>ICMP Ping：' + okLabel(data.ping_ok, data.ping_ok && data.latency ? '通 (' + esc(data.latency) + ')' : '通') + '</div>');
      rows.push('<div>SSH 端口 ' + esc(data.ssh_port || '?') + '：' + okLabel(data.ssh_port_ok, '开放') + '</div>');
      rows.push('<div>HTTPS：' + okLabel(data.https_ok, data.https_ok ? (/^\d{3}$/.test(data.https_code) ? esc(data.https_code) : '通') : '不通') + '</div>');
      rows.push('</div></div>');
      body.innerHTML = rows.join('');
    }
    // 同步更新顶部 banner
    if (msg) msg.textContent = (data.level === 'ok' ? '✅ ' : '⚠️ ') + (data.verdict || '');
    if (data.level === 'ok') { banner.classList.add('ok'); }
    const parts = [];
    parts.push('ICMP: ' + (data.ping_ok ? '通' + (data.latency ? ' (' + data.latency + ')' : '') : '不通'));
    parts.push('SSH端口 ' + (data.ssh_port || '?') + ': ' + (data.ssh_port_ok ? '开放' : '不通'));
    parts.push('HTTPS: ' + (data.https_ok ? data.https_code : '不通'));
    if (detail) detail.textContent = parts.join('  |  ');
  } catch (e) {
    if (body) body.innerHTML = '<div style="color:var(--bad)">⚠️ 探测失败: ' + esc(e.message) + '</div>';
    if (msg) msg.textContent = '⚠️ 探测失败: ' + e.message;
  }
}
const gerritInitPromise = init().finally(function() {
  window.GmsEmbeddedWorkspace && window.GmsEmbeddedWorkspace.markReady();
});
resolveGerritEntryReady();
async function saveChangeToWiki(changeId) {
  const item = (window.__changeCache || {})[String(changeId)];
  if (!item) {
    notifyUser('数据缺失', '未找到该补丁的缓存数据，请重新加载看板后再试', 'error');
    return;
  }
  const id = item._number || item.number || item.id || item.changeId || changeId;
  const subject = item.subject || ('Gerrit #' + id);
  const project = item.project || '';
  const owner = (item.owner || {}).name || (item.owner || {}).email || '-';
  const branch = item.branch || '-';
  const status = item.status || '-';
  const url = changeUrl(item);
  const parts = [];
  parts.push('# ' + subject);
  parts.push('');
  parts.push('- **Gerrit Change**: #' + id);
  if (project) parts.push('- **项目**: ' + project);
  if (branch && branch !== '-') parts.push('- **分支**: ' + branch);
  parts.push('- **状态**: ' + status);
  parts.push('- **Owner**: ' + owner);
  if (url) parts.push('- **链接**: ' + url);
  parts.push('');
  parts.push('## 说明');
  parts.push('');
  parts.push('（待补充：补丁背景、改动要点、风险与回归验证。）');
  parts.push('');
  const content = parts.join('\n');
  const payload = {
    content_md: content,
    space_id: 'issues',
    title: `Gerrit ${id} ${subject || ''}`.trim(),
    source: 'gerrit',
    tags: ['Gerrit补丁说明'].concat(project ? [project] : []),
    links: [
      {target_type: 'gerrit_change', target_id: String(id), title: String(id)},
      ...(project ? [{target_type: 'test_case', target_id: project, title: project}] : [])
    ]
  };
  try {
    await api('/api/knowledge/docs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    notifyUser('已存为Wiki', '已存入「Gerrit补丁说明」并关联 #' + id, 'success');
  } catch (e) {
    notifyUser('存为Wiki失败', (e && e.message) || String(e), 'error');
  }
}
