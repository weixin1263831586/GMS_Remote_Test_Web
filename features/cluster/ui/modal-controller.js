// Focus-safe modal stack for the standalone Cluster workspace.
(function () {
    'use strict';

    const FOCUSABLE_SELECTOR = [
        'input:not([type="hidden"])',
        'select',
        'textarea',
        'button',
        '[href]',
        '[contenteditable="true"]',
        '[tabindex]:not([tabindex="-1"])'
    ].join(', ');
    const stack = [];
    const focusOrigins = new Map();

    function isVisibleFocusTarget(element) {
        if (!element || element.disabled || element.hidden
            || element.getAttribute('tabindex') === '-1'
            || element.getAttribute('aria-hidden') === 'true'
            || element.getAttribute('aria-disabled') === 'true'
            || element.closest('[hidden], [inert]')) return false;
        const style = window.getComputedStyle(element);
        return style.display !== 'none' && style.visibility !== 'hidden'
            && element.getClientRects().length > 0;
    }

    function isCloseControl(element) {
        const text = String(element.textContent || '').trim();
        return element.id.startsWith('close-') || text === '×' || text === '关闭';
    }

    function modalContent(modal) {
        return modal.querySelector('.onboarding-modal') || modal;
    }

    function focusInitial(modalId) {
        const modal = document.getElementById(modalId);
        if (!modal || stack[stack.length - 1] !== modalId) return;
        const controls = Array.from(modal.querySelectorAll(FOCUSABLE_SELECTOR))
            .filter(isVisibleFocusTarget);
        const target = controls.find(element => (
            element.hasAttribute('data-modal-initial-focus')
        )) || controls.find(element => element.matches('input, select, textarea'))
            || controls.find(element => !isCloseControl(element))
            || modalContent(modal);
        if (!target.hasAttribute('tabindex') && target === modalContent(modal)) {
            target.setAttribute('tabindex', '-1');
        }
        target.focus({preventScroll: true});
    }

    function scheduleInitialFocus(modalId) {
        window.requestAnimationFrame(() => focusInitial(modalId));
    }

    function syncClusterModalState() {
        const previous = stack.slice();
        const visible = Array.from(document.querySelectorAll('.modal-backdrop:not([hidden])'));
        const visibleIds = new Set(visible.map(modal => modal.id));
        const active = previous.filter(id => visibleIds.has(id));
        const added = [];
        visible.forEach(modal => {
            if (active.includes(modal.id)) return;
            active.push(modal.id);
            added.push(modal.id);
            if (!focusOrigins.has(modal.id)) {
                focusOrigins.set(modal.id, document.activeElement);
            }
        });
        const removed = previous.filter(id => !visibleIds.has(id));
        stack.length = 0;
        active.forEach(id => stack.push(id));
        const topIndex = stack.length - 1;
        stack.forEach((id, index) => {
            const modal = document.getElementById(id);
            if (!modal) return;
            modal.style.zIndex = String(10000 + index * 20);
            modal.inert = index !== topIndex;
            modal.setAttribute('role', 'dialog');
            modal.setAttribute('aria-hidden', index === topIndex ? 'false' : 'true');
            if (index === topIndex) modal.setAttribute('aria-modal', 'true');
            else modal.removeAttribute('aria-modal');
        });
        document.querySelectorAll('.modal-backdrop[hidden]').forEach(modal => {
            modal.inert = false;
            modal.setAttribute('aria-hidden', 'true');
            modal.removeAttribute('aria-modal');
            modal.style.removeProperty('z-index');
        });
        document.body.classList.toggle('modal-open', stack.length > 0);

        if (added.length) {
            scheduleInitialFocus(stack[stack.length - 1]);
            return;
        }
        if (!removed.length) return;
        const topModal = document.getElementById(stack[stack.length - 1]);
        let origin = null;
        for (let index = previous.length - 1; index >= 0; index -= 1) {
            const id = previous[index];
            if (!removed.includes(id)) continue;
            const candidate = focusOrigins.get(id);
            if (candidate?.isConnected && (!topModal || topModal.contains(candidate))) {
                origin = candidate;
                break;
            }
        }
        if (!topModal && !origin) {
            for (const id of previous) {
                const candidate = focusOrigins.get(id);
                if (removed.includes(id) && candidate?.isConnected) {
                    origin = candidate;
                    break;
                }
            }
        }
        removed.forEach(id => focusOrigins.delete(id));
        if (origin) {
            window.requestAnimationFrame(() => origin.focus({preventScroll: true}));
        } else if (topModal) {
            scheduleInitialFocus(topModal.id);
        }
    }

    function closeTopClusterModal() {
        const id = stack[stack.length - 1];
        const modal = id && document.getElementById(id);
        if (!modal) return;
        modal.hidden = true;
        syncClusterModalState();
    }

    document.addEventListener('keydown', event => {
        if (!stack.length) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            closeTopClusterModal();
            return;
        }
        if (event.key !== 'Tab') return;
        const modal = document.getElementById(stack[stack.length - 1]);
        if (!modal) return;
        const controls = Array.from(modal.querySelectorAll(FOCUSABLE_SELECTOR))
            .filter(isVisibleFocusTarget);
        if (!controls.length) {
            event.preventDefault();
            focusInitial(modal.id);
            return;
        }
        const first = controls[0];
        const last = controls[controls.length - 1];
        const active = document.activeElement;
        if (event.shiftKey && (!modal.contains(active) || active === first)) {
            event.preventDefault();
            last.focus({preventScroll: true});
        } else if (!event.shiftKey && (!modal.contains(active) || active === last)) {
            event.preventDefault();
            first.focus({preventScroll: true});
        }
    });
    document.addEventListener('click', event => {
        const modal = event.target?.classList?.contains('modal-backdrop')
            ? event.target : null;
        if (modal && stack[stack.length - 1] === modal.id) {
            modal.hidden = true;
            syncClusterModalState();
        }
    });
    new MutationObserver(syncClusterModalState).observe(
        document.body,
        {subtree: true, attributes: true, attributeFilter: ['hidden']}
    );

    window.ClusterModalController = {stack, sync: syncClusterModalState, closeTop: closeTopClusterModal};
    window.syncClusterModalState = syncClusterModalState;
    window.closeTopClusterModal = closeTopClusterModal;
    syncClusterModalState();
})();
