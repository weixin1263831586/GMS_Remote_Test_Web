// Initialize only the settings view; no dashboard polling or business scans.
loadRedmineSettings();
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && window.parent !== window) window.parent.closeSidebarVisibilityModal?.();
});
