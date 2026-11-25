// Single reveal controller for explicit password-input-wrap fields.
// All actions use the existing CSP-safe act-bridge; no network or storage.
(function () {
    'use strict';
    let active = null;
    let observer = null;

    function setVisible(input, button, visible) {
        input.type = visible ? 'text' : 'password';
        button.classList.toggle('is-visible', visible);
        const label = visible ? '隐藏密码' : '显示密码';
        button.setAttribute('aria-label', label);
        button.title = label;
    }

    function conceal() {
        if (observer) observer.disconnect();
        observer = null;
        if (!active) return;
        const {input, button} = active;
        active = null;
        setVisible(input, button, false);
    }

    function concealOutside(event) {
        if (active && !active.input.parentElement?.contains(event.target)) conceal();
    }

    window.togglePasswordVisibility = function (button) {
        const wrap = button.closest('.password-input-wrap');
        const input = wrap?.querySelector(':scope > input');
        if (!input || input.disabled || input.readOnly) return;
        if (active?.input === input) {
            conceal();
            return;
        }
        conceal();
        if (input.type !== 'password') {
            setVisible(input, button, false);
            return;
        }
        setVisible(input, button, true);
        active = {input, button};
        // Observe only the revealed field's ancestor chain. Closing/hiding a
        // modal or replacing a dynamic prompt must never leave it in plain text.
        observer = new MutationObserver(() => {
            if (!input.isConnected || !input.getClientRects().length
                    || getComputedStyle(input).visibility !== 'visible'
                    || input.disabled || input.readOnly) conceal();
        });
        observer.observe(input, {attributes: true, attributeFilter: ['disabled', 'readonly']});
        for (let node = wrap; node; node = node.parentElement) {
            observer.observe(node, {
                attributes: true, attributeFilter: ['hidden', 'style', 'class', 'open'],
                childList: true,
            });
        }
    };

    document.addEventListener('pointerdown', concealOutside, true);
    document.addEventListener('focusin', concealOutside, true);
    document.addEventListener('reset', event => {
        if (active && event.target.contains(active.input)) conceal();
    }, true);
    document.addEventListener('submit', event => {
        if (active && event.target.contains(active.input)) conceal();
    }, true);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) conceal();
    });
    window.addEventListener('pagehide', conceal);
})();
