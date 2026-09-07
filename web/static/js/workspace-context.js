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
    // 已知 Worker ID 集合（Cluster Status 加载后回填）：用于区分
    // "worker:serial" 前缀与本身就含 ":" 的 serial（ip:5555 等）。
    const knownWorkerIds = new Set();
    let context = {...DEFAULT_CONTEXT};
    let activePage = String(window.__targetPage || 'test');
    let resolveReady;
    const ready = new Promise(resolve => { resolveReady = resolve; });
    let resolveClusterStatusReady;
    const clusterStatusReady = new Promise(resolve => { resolveClusterStatusReady = resolve; });
    let initializePromise = null;

    function normalize(raw) {
        const next = {...DEFAULT_CONTEXT, ...(raw || {})};
        next.scope_mode = next.scope_mode === 'cluster' ? 'cluster' : 'single';
        const requestedWorker = String(raw?.worker_id || '');
        next.worker_id = requestedWorker || localWorkerId;
        next.device_ids = Array.from(new Set((Array.isArray(next.device_ids) ? next.device_ids : [])
            .map(value => String(value || '').trim()).filter(Boolean))).slice(0, 32);
        if (next.scope_mode === 'single') {
            next.worker_id = localWorkerId;
            // 只剥离已知 localWorkerId 前缀；serial 本身可能含 ":"
            // （ADB TCP "ip:5555"、ADB Proxy "localhost:port"），不能按
            // "包含冒号" 判断为跨 Worker 设备而误删——只有以其它已知
            // Worker ID 前缀开头的条目才是跨 Worker 残留。
            const knownWorkerPrefix = prefix => prefix
                && prefix !== localWorkerId
                && knownWorkerIds.has(prefix);
            next.device_ids = next.device_ids.filter(value => {
                const separator = value.indexOf(':');
                if (separator <= 0) return true;
                return !knownWorkerPrefix(value.slice(0, separator));
            });
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

    async function loadClusterStatus() {
        const status = await clusterStatusReady;
        if (status) return status;
        const response = await fetch('/api/cluster/status', {cache: 'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
    }

    async function loadClusterWorkers(forceRefresh = false) {
        const cached = window.clusterWorkersSnapshot;
        if (!forceRefresh && Array.isArray(cached?.workers)
                && Date.now() - Number(cached.loadedAt || 0) < 5000) {
            return cached.workers;
        }
        const response = await fetch('/api/cluster/workers', {cache: 'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        const workers = Array.isArray(payload.workers) ? payload.workers : [];
        window.clusterWorkersSnapshot = {workers, loadedAt: Date.now()};
        return workers;
    }

    async function loadInitialTestData(clusterModeReady) {
        await Promise.all([clusterModeReady, ready]);
        return Promise.allSettled([
            window.loadClusterWorkers(),
            window.loadDevices(false),
        ]);
    }

    async function initialize() {
        if (initializePromise) return initializePromise;
        initializePromise = initializeFromServer();
        return initializePromise;
    }

    async function initializeFromServer() {
        let clusterStatus = null;
        if (window.state?.authRequired && !window.state?.authReady) {
            resolveClusterStatusReady(clusterStatus);
            revision += 1;
            resolveReady(snapshot());
            window.dispatchEvent(new CustomEvent('gms:workspace-context-ready', {detail: {context: snapshot()}}));
            broadcast();
            return snapshot();
        }
        try {
            const [response, clusterResponse] = await Promise.all([
                fetch('/api/users/workspace-context', {cache: 'no-store'}),
                fetch('/api/cluster/status', {cache: 'no-store'}).catch(() => null)
            ]);
            if (clusterResponse?.ok) {
                clusterStatus = await clusterResponse.json();
                localWorkerId = String(clusterStatus.local_worker_id || localWorkerId);
                knownWorkerIds.clear();
                knownWorkerIds.add(localWorkerId);
                if (Array.isArray(clusterStatus.workers)) {
                    clusterStatus.workers.forEach(worker => {
                        const id = String(worker?.id || '').trim();
                        if (id) knownWorkerIds.add(id);
                    });
                    window.clusterWorkersSnapshot = {
                        workers: clusterStatus.workers,
                        loadedAt: Date.now(),
                    };
                }
            }
            if (!response.ok && response.status === 401 && typeof window.showAuthGate === 'function') {
                window.showAuthGate(false);
            }
            const payload = await response.json();
            if (response.ok && payload?.data?.context) context = normalize(payload.data.context);
        } catch (error) {
            console.debug('[WorkspaceContext] load failed; using local default:', error);
        } finally {
            resolveClusterStatusReady(clusterStatus);
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
        initialize, setActivePage, loadClusterStatus,
        loadClusterWorkers, loadInitialTestData,
        localWorkerId: () => localWorkerId,
        // 供 Cluster Status 加载方在拿到真实 local_worker_id 后回填；
        // 传入空值时保留当前值（初始默认仅作为 Cluster Status 加载前的
        // fallback，不允许其它调用点硬编码具体 Worker ID）。
        setLocalWorkerId: workerId => {
            const next = String(workerId || '').trim();
            if (next) localWorkerId = next;
            return localWorkerId;
        }
    });
    if (window.state?.authReady) {
        initialize();
    } else {
        window.addEventListener('gms:auth-ready', initialize, {once: true});
    }
})();
