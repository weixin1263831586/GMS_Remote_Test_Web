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
        }
    });

    document.addEventListener('visibilitychange', dispatchVisibility);

    window.GmsEmbeddedWorkspace = Object.freeze({
        ready, get: snapshot, update, navigate, markReady, isVisible
    });
    send('workspace-context-request');
    window.addEventListener('DOMContentLoaded', () => send('workspace-context-request'), {once: true});
})();
