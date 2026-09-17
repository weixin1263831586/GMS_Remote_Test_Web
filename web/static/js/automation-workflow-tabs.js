(() => {
    'use strict';

    function tabsIn(container) {
        return Array.from(container.querySelectorAll(':scope > [role="tab"][data-workflow]'))
            .filter(tab => !tab.disabled && tab.offsetParent !== null);
    }

    function syncRovingTabIndex(container) {
        const tabs = tabsIn(container);
        if (!tabs.length) return;
        let selected = tabs.find(tab => tab.getAttribute('aria-selected') === 'true');
        if (!selected) selected = tabs[0];
        tabs.forEach(tab => {
            tab.tabIndex = tab === selected ? 0 : -1;
        });
    }

    function bindWorkflowTabs() {
        const container = document.querySelector('.workflow-tabs[role="tablist"]');
        if (!container || container.dataset.keyboardBound === 'true') return;
        container.dataset.keyboardBound = 'true';
        syncRovingTabIndex(container);

        container.addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            const tabs = tabsIn(container);
            if (!tabs.length) return;
            const currentIndex = Math.max(0, tabs.indexOf(document.activeElement));
            let nextIndex = currentIndex;
            if (event.key === 'Home') nextIndex = 0;
            else if (event.key === 'End') nextIndex = tabs.length - 1;
            else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
            else nextIndex = (currentIndex + 1) % tabs.length;
            event.preventDefault();
            const next = tabs[nextIndex];
            next.focus();
            next.click();
        });

        // switchWorkflowPane can also be invoked by buttons outside the tablist
        // (for example “选择运行”). Keep roving tabindex in sync with those
        // programmatic tab changes without coupling this helper to page.js.
        const observer = new MutationObserver(() => syncRovingTabIndex(container));
        tabsIn(container).forEach(tab => observer.observe(tab, {
            attributes: true,
            attributeFilter: ['aria-selected', 'class'],
        }));
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindWorkflowTabs, {once: true});
    } else {
        bindWorkflowTabs();
    }
})();
