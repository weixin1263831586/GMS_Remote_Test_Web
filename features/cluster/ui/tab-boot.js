// Resolve the initial cluster tab before the first paint (CSP-safe script).
// This key is shared with state.js/page.js in this same-origin iframe.
(function () {
  const activeKey = 'gms_cluster_active_tab';
  const resetKey = 'gms_cluster_force_dashboard_once';
  const url = new URL(window.location.href);
  let saved = '';
  let forceFirst = false;
  try {
    forceFirst = sessionStorage.getItem(resetKey) === '1';
    sessionStorage.removeItem(resetKey);
    if (forceFirst) sessionStorage.removeItem(activeKey);
    else saved = sessionStorage.getItem(activeKey) || '';
  } catch (_error) { /* Storage unavailable: respect URL or default. */ }
  if (forceFirst && url.searchParams.has('tab')) {
    url.searchParams.delete('tab');
    window.history.replaceState(window.history.state, '', url.toString());
  }
  const requested = forceFirst ? 'dashboard' : (url.searchParams.get('tab') || saved);
  const initial = requested === 'management' ? 'management' : 'dashboard';
  window.__clusterInitialTab = initial;
  document.documentElement.dataset.clusterInitialTab = initial;

  // Keyboard events inside an iframe do not bubble to the host shell.
  window.addEventListener('keydown', event => {
    const key = String(event.key || '').toLowerCase();
    const forceReload = ((event.ctrlKey || event.metaKey) && event.shiftKey && key === 'r')
      || (event.ctrlKey && key === 'f5');
    if (!forceReload) return;
    try {
      for (const item of [
        'gms_devices_view', 'gms_users_view', 'gms_audit_view', activeKey
      ]) sessionStorage.removeItem(item);
      sessionStorage.setItem(resetKey, '1');
    } catch (_error) { /* Storage unavailable. */ }
  }, true);
})();
