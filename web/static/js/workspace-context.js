(function workspaceContextBootstrap() {
    'use strict';

    const DEFAULT_CONTEXT = Object.freeze({
        scope_mode: 'single', worker_id: 'ats-worker-controller', device_ids: [],
        suite_key: '', suite_path: '', cluster_job_id: '', attempt_id: '',
        automation_run_id: '', report_id: '', report_timestamp: '', artifact_id: '',
        gerrit_change_id: '', gerrit_patchset: '', redmine_issue_id: '', origin_page: 'test'
    });
    const EMBEDDED_FRAMES = Object.freeze({
        automation: 'automation-frame', cluster: 'cluster-frame',
        'redmine-agent': 'redmine-agent-frame', 'gerrit-dashboard': 'gerrit-dashboard-frame'
    });
    let persistTimer = null;
    let persistPromise = null;
    let persistQueued = false;
    let revision = 0;
    let localWorkerId = 'ats-worker-controller';
    let context = {...DEFAULT_CONTEXT};
    let activePage = String(window.__targetPage || 'test');
    let resolveReady;
    const ready = new Promise(resolve => { resolveReady = resolve; });

    function normalize(raw) {
        const next = {...DEFAULT_CONTEXT, ...(raw || {})};
        next.scope_mode = next.scope_mode === 'cluster' ? 'cluster' : 'single';
        const requestedWorker = String(raw?.worker_id || '');
        next.worker_id = requestedWorker || localWorkerId;
        next.device_ids = Array.from(new Set((Array.isArray(next.device_ids) ? next.device_ids : [])
            .map(value => String(value || '').trim()).filter(Boolean))).slice(0, 32);
        if (next.scope_mode === 'single') {
            next.worker_id = localWorkerId;
            next.device_ids = next.device_ids.filter(value => !value.includes(':') || value.startsWith(`${localWorkerId}:`));
        }
        return next;
    }

    function snapshot() {
        return {...context, device_ids: [...context.device_ids], revision};
    }

    function frameForPage(page) {
        const id = EMBEDDED_FRAMES[page];
        return id ? document.getElementById(id) : null;
    }

    function postToFrame(page, type = 'workspace-context') {
        const frame = frameForPage(page);
        if (!frame?.contentWindow) return;
        frame.contentWindow.postMessage({type, context: snapshot()}, window.location.origin);
    }

    function postVisibilityToFrame(page) {
        const frame = frameForPage(page);
        if (!frame?.contentWindow) return;
        frame.contentWindow.postMessage({
            type: 'embedded-surface-visibility',
            visible: page === activePage
        }, window.location.origin);
    }

    function setActivePage(page) {
        activePage = String(page || 'test');
        Object.keys(EMBEDDED_FRAMES).forEach(postVisibilityToFrame);
    }

    function broadcast() {
        Object.keys(EMBEDDED_FRAMES).forEach(page => postToFrame(page));
    }

    async function persist() {
        persistTimer = null;
        if (persistPromise) {
            persistQueued = true;
            return persistPromise;
        }

        const persistedRevision = revision;
        const persistedContext = {...context, device_ids: [...context.device_ids]};
        persistPromise = (async () => {
            try {
                const response = await fetch('/api/users/workspace-context', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(persistedContext)
                });
                if (!response.ok) {
                    if (response.status === 401 && typeof window.showAuthGate === 'function') {
                        window.showAuthGate(false);
                    }
                    throw new Error(`HTTP ${response.status}`);
                }
                const payload = await response.json();
                const serverContext = payload?.data?.context;
                // A response for an older host selection is only an ACK. It
                // must never roll the browser context back after another
                // Worker has already been selected.
                if (serverContext && revision === persistedRevision) {
                    context = normalize(serverContext);
                }
            } catch (error) {
                console.debug('[WorkspaceContext] persist failed:', error);
            }
        })();

        try {
            await persistPromise;
        } finally {
            persistPromise = null;
            if (persistQueued || revision !== persistedRevision) {
                persistQueued = false;
                schedulePersist();
            }
        }
    }

    function schedulePersist() {
        if (persistTimer) clearTimeout(persistTimer);
        persistTimer = setTimeout(persist, 120);
    }

    function update(patch, options = {}) {
        const previous = snapshot();
        const next = normalize({...context, ...(patch || {})});
        const changed = Object.keys(DEFAULT_CONTEXT).some(key =>
            key === 'device_ids'
                ? JSON.stringify(next.device_ids) !== JSON.stringify(context.device_ids)
                : next[key] !== context[key]
        );
        if (!changed) return previous;
        context = next;
        revision += 1;
        const detail = {context: snapshot(), previous, source: options.source || 'shell'};
        window.dispatchEvent(new CustomEvent('gms:workspace-context', {detail}));
        if (options.persist !== false) schedulePersist();
        if (options.broadcast !== false) broadcast();
        return snapshot();
    }

    function navigate(page, patch = {}) {
        update({...patch, origin_page: page}, {source: 'navigate'});
        if (typeof window.switchPage === 'function') window.switchPage(page, null);
        postToFrame(page, 'workspace-context-navigate');
    }

    async function initialize() {
        try {
            const [response, clusterResponse] = await Promise.all([
                fetch('/api/users/workspace-context', {cache: 'no-store'}),
                fetch('/api/cluster/status', {cache: 'no-store'}).catch(() => null)
            ]);
            if (clusterResponse?.ok) {
                const clusterStatus = await clusterResponse.json();
                localWorkerId = String(clusterStatus.local_worker_id || localWorkerId);
            }
            if (!response.ok && response.status === 401 && typeof window.showAuthGate === 'function') {
                window.showAuthGate(false);
            }
            const payload = await response.json();
            if (response.ok && payload?.data?.context) context = normalize(payload.data.context);
        } catch (error) {
            console.debug('[WorkspaceContext] load failed; using local default:', error);
        }
        revision += 1;
        resolveReady(snapshot());
        window.dispatchEvent(new CustomEvent('gms:workspace-context-ready', {detail: {context: snapshot()}}));
        broadcast();
        return snapshot();
    }

    function sourcePageForWindow(source) {
        return Object.keys(EMBEDDED_FRAMES).find(page => frameForPage(page)?.contentWindow === source) || '';
    }

    window.addEventListener('message', event => {
        if (event.origin !== window.location.origin || !event.data || typeof event.data !== 'object') return;
        const page = sourcePageForWindow(event.source);
        if (!page) return;
        if (event.data.type === 'embedded-surface-ready') {
            window.markLazyFrameReady?.(frameForPage(page));
        } else if (event.data.type === 'workspace-context-request') {
            postToFrame(page);
            postVisibilityToFrame(page);
        } else if (event.data.type === 'workspace-context-update') {
            update(event.data.context || {}, {source: page});
        } else if (event.data.type === 'workspace-navigate' && event.data.page) {
            navigate(String(event.data.page), event.data.context || {});
        }
    });

    Object.entries(EMBEDDED_FRAMES).forEach(([page, id]) => {
        window.addEventListener('DOMContentLoaded', () => {
            document.getElementById(id)?.addEventListener('load', () => {
                postToFrame(page);
                postVisibilityToFrame(page);
            });
        }, {once: true});
    });

    window.GmsWorkspace = Object.freeze({
        ready, get: snapshot, update, navigate, postToFrame,
        initialize, setActivePage, localWorkerId: () => localWorkerId
    });
    initialize();
})();
