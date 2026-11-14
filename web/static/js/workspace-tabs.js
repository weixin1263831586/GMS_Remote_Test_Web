// Reveal focused tabs within their row without scrolling the iframe or shell.
document.addEventListener('focusin', event => {
    const tab = event.target.closest('.workspace-tabs > .workspace-tab');
    if (!tab) return;
    const row = tab.parentElement;
    const bounds = row.getBoundingClientRect();
    const target = tab.getBoundingClientRect();
    if (target.left < bounds.left) row.scrollLeft += target.left - bounds.left;
    else if (target.right > bounds.right) row.scrollLeft += target.right - bounds.right;
});
