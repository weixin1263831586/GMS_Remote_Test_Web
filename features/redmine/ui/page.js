// Redmine page; loaded by page.html in dependency order.
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
