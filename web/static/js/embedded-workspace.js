(function embeddedWorkspaceBootstrap() {
    'use strict';

    let context = {};
    let resolveReady;
    const ready = new Promise(resolve => { resolveReady = resolve; });
    let initialized = false;
    let parentSurfaceVisible = true;

    function snapshot() {
        return {
            ...context,
            device_ids: Array.isArray(context.device_ids) ? [...context.device_ids] : []
        };
    }

    function accept(next, type) {
        context = {...context, ...(next || {})};
        if (!initialized) {
            initialized = true;
            resolveReady(snapshot());
        }
        window.dispatchEvent(new CustomEvent('gms:embedded-workspace', {
            detail: {context: snapshot(), type}
        }));
    }

    function send(type, payload = {}) {
        if (window.parent === window) return;
        window.parent.postMessage({type, ...payload}, window.location.origin);
    }

    function update(patch) {
        context = {...context, ...(patch || {})};
        send('workspace-context-update', {context: patch || {}});
        return snapshot();
    }

    function navigate(page, patch = {}) {
        context = {...context, ...patch};
        send('workspace-navigate', {page, context: patch});
    }

    let surfaceReadySent = false;
    function markReady() {
        if (surfaceReadySent) return;
        surfaceReadySent = true;
        send('embedded-surface-ready');
    }

    function isVisible() {
        return parentSurfaceVisible && !document.hidden;
    }

    function dispatchVisibility() {
        window.dispatchEvent(new CustomEvent('gms:embedded-visibility', {
            detail: {visible: isVisible()}
        }));
    }

    window.addEventListener('message', event => {
        if (
            event.origin !== window.location.origin
            || event.source !== window.parent
            || !event.data
            || typeof event.data !== 'object'
        ) return;
        if (['workspace-context', 'workspace-context-navigate'].includes(event.data.type)) {
            accept(event.data.context || {}, event.data.type);
        } else if (event.data.type === 'embedded-surface-visibility') {
            const nextVisible = event.data.visible === true;
            if (nextVisible !== parentSurfaceVisible) {
                parentSurfaceVisible = nextVisible;
                dispatchVisibility();
            }
        } else if (event.data.type === 'embedded-focus-active-tab') {
            // 外层侧栏使用上下键切换到此页面时，直接进入当前页签。所有
            // 内嵌看板都以 ARIA tab 暴露当前选中页签，因此无需让各页面
            // 重复实现同一套跨 iframe 焦点逻辑。
            const activeTab = document.querySelector(
                '[role="tab"][aria-selected="true"]:not([disabled])'
            );
            activeTab?.focus({preventScroll: true});
        }
    });

    // 键盘事件不会跨 iframe 冒泡。外层侧栏将焦点交给页签后，仍须把
    // 上下键交还给父页面的侧栏导航；仅在页签自身拥有焦点时转发，避免
    // 影响子页输入框、选择器或终端的原有上下键行为。
    document.addEventListener('keydown', event => {
        if (!['ArrowUp', 'ArrowDown'].includes(event.key)) return;
        const target = event.target;
        if (!(target instanceof Element) || target.getAttribute('role') !== 'tab') return;
        event.preventDefault();
        send('embedded-sidebar-arrow', {key: event.key});
    }, true);

    document.addEventListener('visibilitychange', dispatchVisibility);

    window.GmsEmbeddedWorkspace = Object.freeze({
        ready, get: snapshot, update, navigate, markReady, isVisible
    });
    send('workspace-context-request');
    window.addEventListener('DOMContentLoaded', () => send('workspace-context-request'), {once: true});
})();
