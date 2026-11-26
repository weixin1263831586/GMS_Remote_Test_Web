// Initialize only the settings view; no dashboard polling or business scans.
loadRedmineSettings();
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && window.parent !== window) window.parent.closeSidebarVisibilityModal?.();
});

const settingsFrame = window.frameElement;
if (settingsFrame?.id === 'system-redmine-settings-frame') {
  document.documentElement.classList.add('embedded-system-settings');
  const form = document.getElementById('redmine-system-settings');
  function syncSettingsFrameHeight() {
    const bounds = form.getBoundingClientRect();
    if (bounds.width > 0 && bounds.height > 0) {
      const height = Math.ceil(bounds.bottom + window.scrollY);
      if (settingsFrame.style.height !== height + 'px') settingsFrame.style.height = height + 'px';
    }
  }
  new ResizeObserver(syncSettingsFrameHeight).observe(form);
  window.addEventListener('load', syncSettingsFrameHeight, {once:true});
  syncSettingsFrameHeight();
}
