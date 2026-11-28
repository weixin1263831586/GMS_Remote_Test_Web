        // ==================== 个人知识库 ====================
        const kbState = {
            initialized: false,
            policyImport: false,
            spaces: [],
            nodes: [],
            currentSpace: 'gms',
            currentParent: '',
            currentDocId: '',
            currentNodeId: '',
            currentFavorite: false,
            dirty: false,
            docsLoaded: false,
            docsRequestGeneration: 0,
        };

        function kbApiData(result) {
            if (result && result.success === false) throw new Error(result.error || '请求失败');
            return result && result.data !== undefined ? result.data : result;
        }

        async function kbFetch(url, options, retried = false) {
            const response = await fetch(url, {credentials: 'same-origin', ...(options || {})});
            const result = await response.json().catch(() => ({}));
            if (!retried && (url.startsWith('/api/knowledge/external/') || (url === '/api/knowledge/upload' && kbState.policyImport)) && response.status === 403 && result.detail?.elevation_required && typeof window.requestElevatedAccess === 'function') {
                if (await window.requestElevatedAccess('管理外部知识源')) return kbFetch(url, options, true);
                throw new Error('已取消管理员验证');
            }
            if (!response.ok || result.success === false) {
                if (response.status === 403 && url.startsWith('/api/knowledge/external/')) {
                    throw new Error('外部知识源管理需要管理员授权');
                }
                throw new Error(result.error || result.message || result.detail?.message || `HTTP ${response.status}`);
            }
            return kbApiData(result);
        }

        function kbEsc(text) {
            return String(text == null ? '' : text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }

        function kbSnippet(text, size = 120) {
            const value = String(text || '').replace(/\s+/g, ' ').trim();
            return value.length > size ? value.slice(0, size) + '...' : value;
        }

        async function initNotesPage() {
            if (kbState.initialized) return;
            kbState.initialized = true;
            const content = document.getElementById('kb-doc-content');
            const title = document.getElementById('kb-doc-title');
            const tags = document.getElementById('kb-doc-tags');
            [content, title, tags].forEach(el => { if (el) el.addEventListener('input', () => kbState.dirty = true); });
            kbInitExternalSources();
            await kbRefreshAll();
        }


        function kbInitExternalSources() {
            const page = document.getElementById('page-notes');
            const panel = page?.querySelector('[data-kb-external]');
            const personal = page?.querySelector('.kb-layout');
            if (!panel || !personal) return;
            const form = panel.querySelector('[data-kb-source-form]');
            const list = panel.querySelector('[data-kb-sources]');
            const refreshButton = panel.querySelector('[data-kb-refresh]');
            const api = '/api/knowledge/external/web-sources';
            const controls = form.elements;
            const submitButton = panel.querySelector('[data-kb-submit]');
            const resetButton = panel.querySelector('[data-kb-reset]');
            const partnerUrl = 'https://docs.partner.android.com/gms/policies/domains/reqs';
            const partnerApi = '/api/gms-update-center';
            let editing = null;
            let busy = false;

            function reset() {
                form.reset();
                editing = null;
                controls.namedItem('url').readOnly = false;
                submitButton.textContent = '添加';
                resetButton.hidden = true;
                resetButton.style.display = 'none';
            }
            function edit(source) {
                editing = source;
                controls.namedItem('name').value = source.name || '';
                controls.namedItem('url').value = source.url;
                controls.namedItem('url').readOnly = true;
                submitButton.textContent = '保存';
                resetButton.hidden = false;
                resetButton.style.display = '';
                controls.namedItem('name').focus();
            }
            function button(label, action) {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'btn-xs';
                b.textContent = label;
                if (label === '同步') b.dataset.busySize = 'short';
                if (label === '语义索引') b.dataset.busySize = 'plain';
                b.addEventListener('click', action);
                return b;
            }
            function openUrlButton(value) {
                let url;
                try {
                    url = new URL(value);
                } catch {
                    return null;
                }
                if (url.protocol !== 'https:' || url.username || url.password) return null;
                const action = button('打开网址', () => window.open(url.href, '_blank', 'noopener,noreferrer'));
                action.title = url.href;
                return action;
            }
            async function openPolicyImport() {
                if (await window.showConfirmDialog('授权规范导入', '仅导入你有权访问且允许当前已启用 AI 模型处理的 PDF/HTML 文件。文件仅供本账号的工单分析使用，内容或模型配置变更后需要重新授权。')) kbOpenUpload(true);
            }
            function sourceRequest(source, enabled) {
                return kbFetch(api + '/' + encodeURIComponent(source.id), {
                    method: 'PUT', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url: source.url, name: source.name || '', enabled})
                });
            }
            async function documentUnderstanding(trigger) {
                const approved = await showConfirmDialog('文档理解范围',
                    '是否处理本人已授权导入的规范？取消则处理本人普通文档。');
                const endpoint = '/api/knowledge/semantic-index?kind=understanding&approved_only=' + (approved ? 'true' : 'false');
                const before = await kbFetch(endpoint);
                const counts = value => (value.processed ?? 0) + '/' + (value.total ?? 0);
                trigger.title = 'LLM 文档理解：' + counts(before);
                if (before.status === 'disabled') {
                    showToast('尚未启用本地个人文档理解，请检查 Controller retrieval 配置。', 'warning');
                    return;
                }
                if (!await showConfirmDialog('本地 LLM 文档理解',
                    '已处理 ' + counts(before) + '。继续处理最多四个原文窗口？')) return;
                trigger.disabled = true;
                try {
                    const result = await kbFetch(endpoint + '&batch_size=4', {method: 'POST'});
                    const message = 'LLM 文档理解 ' + counts(result)
                        + '；有效 ' + (result.enriched ?? 0) + '；失败 ' + (result.errors ?? 0)
                        + (result.corpus_bounded ? '；当前语料范围受限' : '');
                    trigger.title = message;
                    showToast(message + '；' + result.status, result.coverage_complete ? 'success' : 'warning');
                } finally {
                    trigger.disabled = false;
                }
            }
            async function semanticIndex(endpoint, trigger) {
                const status = await kbFetch(endpoint);
                const counts = (value) => `${value.indexed ?? 0}/${value.total ?? 0}`;
                const understanding = status.document_understanding;
                const understandingInfo = understanding && understanding.status !== 'disabled'
                    ? '；LLM 文档理解：' + (understanding.processed ?? 0) + '/' + (understanding.total ?? 0)
                        + '（有效提取 ' + (understanding.enriched ?? 0) + '，失败 ' + (understanding.errors ?? 0)
                        + '，截断 ' + (understanding.bounded ?? 0) + '）'
                    : '';
                const scheduling = (status.auto_index_enabled === true
                    ? 'Controller 自动索引已配置' + (status.last_auto_index_attempt
                        ? '；最近调度：' + status.last_auto_index_attempt : '；尚无调度记录')
                    : '手动索引') + understandingInfo;
                trigger.title = '语义索引覆盖：' + counts(status) + '；' + scheduling;
                const reasons = {
                    disabled: 'Controller 尚未启用 retrieval.enabled，请配置本地语义检索。',
                    source_disabled: '当前知识源未加入 retrieval.enabled_sources。',
                    invalid_config: 'Controller retrieval 配置格式不正确。',
                    model_unconfigured: '尚未配置本地 Embedding 模型目录或模型修订版本。',
                    model_missing: 'Controller 本地 Embedding 模型目录不存在或不可读。',
                    dependency_missing: 'Controller 服务 Python 环境缺少 sentence-transformers。',
                    configuration_error: 'Controller 本地语义检索配置读取失败。'
                };
                if (status.configuration_issue && reasons[status.configuration_issue]) {
                    showToast(reasons[status.configuration_issue] + understandingInfo, 'warning');
                    return;
                }
                if (!['empty', 'partial', 'ready'].includes(status.status)) {
                    const message = {disabled: '当前知识源的语义索引未启用。',
                        unavailable: '当前知识源索引不可用，请检查原始索引或 Wiki 审批状态。',
                        error: '语义索引状态读取失败，请检查 Controller 日志。'}[status.status];
                    showToast(message || '语义索引暂不可用。', 'warning');
                    return;
                }
                if (status.coverage_complete) {
                    showToast('语义索引已完整：' + counts(status) + '；' + scheduling, 'success');
                    return;
                }
                if (!await window.showConfirmDialog('语义索引', '当前覆盖：' + counts(status) + '；' + scheduling + '。连续构建剩余批次？')) return;
                run(async () => {
                    let current = status;
                    // Keep each server request bounded; committed batches can be resumed.
                    for (let batch = 0; batch < 256; batch++) {
                        if (!trigger.isConnected || panel.hidden) break;
                        const result = await kbFetch(endpoint + '?batch_size=128', {method: 'POST'});
                        trigger.title = '语义索引覆盖：' + counts(result) + '；' + scheduling;
                        if (result.coverage_complete) {
                            showToast('语义索引覆盖：' + counts(result), 'success');
                            return;
                        }
                        if (result.status === 'busy') {
                            showToast('已有索引任务运行，请完成后继续。', 'info');
                            return;
                        }
                        if (!['empty', 'partial', 'ready'].includes(result.status)) {
                            showToast('语义索引未完成：' + counts(result) + '，请检查来源和模型配置。', 'warning');
                            return;
                        }
                        if (!Number.isInteger(result.indexed) || result.indexed <= (current.indexed ?? 0)) {
                            showToast('语义索引无新增进度：' + counts(result) + '，请检查语料范围或模型状态。', 'warning');
                            return;
                        }
                        current = result;
                    }
                    showToast('本轮索引结束：' + counts(current) + '，可继续构建剩余批次。', 'info');
                }, trigger, '索引中');
            }
            async function load() {
                const [data, partnerIndex, partnerScan] = await Promise.all([
                    kbFetch(api),
                    kbFetch(partnerApi + '/requirements/coverage', {cache: 'no-store'}).catch(() => null),
                    kbFetch(partnerApi + '/scan/status', {cache: 'no-store'}).catch(() => null)
                ]);
                const table = document.createElement('table');
                table.className = 'list-table kb-source-table';
                const columns = ['知识源名称', '网址', '版本', '索引条目', '状态', '操作'];
                const colgroup = document.createElement('colgroup');
                for (let i = 0; i < columns.length; i++) colgroup.append(document.createElement('col'));
                table.append(colgroup);
                const header = table.createTHead().insertRow();
                for (const title of columns) {
                    const th = document.createElement('th');
                    th.textContent = title;
                    th.scope = 'col';
                    header.append(th);
                }
                const body = table.createTBody();
                for (const source of data.sources || []) {
                    const row = body.insertRow();
                    for (const value of [source.name || source.url, source.url, source.android_version ?? '通用',
                        source.indexed_count ?? '—',
                        !source.enabled ? '已禁用' : source.error ? (source.revision ? '同步失败，保留旧索引' : '同步失败') : source.revision ? '已同步' : '待同步']) {
                        const cell = row.insertCell();
                        cell.textContent = String(value);
                        cell.title = String(value);
                    }
                    const actions = document.createElement('div');
                    actions.className = 'kb-source-actions ui-table-actions ui-table-actions--end';
                    row.insertCell().append(actions);
                    actions.append(button('编辑', () => edit(source)),
                        button('同步', event => run(async () => {
                            const result = await kbFetch(api + '/' + encodeURIComponent(source.id) + '/sync', {method: 'POST'});
                            showToast(result.status === 'in_progress' ? '知识源正在同步' : '知识源同步完成', 'info');
                        }, event.currentTarget, '同步中')),
                        button(source.enabled ? '禁用' : '启用', () => run(() => sourceRequest(source, !source.enabled))),
                        button('删除', async () => {
                            if (await window.showConfirmDialog('删除外部知识源', '删除此知识源及其索引和历史修订？')) {
                                run(async () => {
                                    await kbFetch(api + '/' + encodeURIComponent(source.id), {method: 'DELETE'});
                                    if (editing?.id === source.id) reset();
                                });
                            }
                        }));
                    if (source.enabled && source.revision) {
                        actions.append(button('语义索引', event => {
                            semanticIndex(api + '/' + encodeURIComponent(source.id) + '/semantic-index',
                                event.currentTarget).catch(error => showToast(error.message, 'warning'));
                        }));
                    }
                    const open = openUrlButton(source.url);
                    if (open) actions.append(open);
                }
                const wiki = body.insertRow();
                for (const value of ['Android Internals Wiki', '本地已批准索引', '多版本', '—', '支持混合检索']) {
                    const cell = wiki.insertCell();
                    cell.textContent = value;
                    cell.title = value;
                }
                const wikiActions = document.createElement('div');
                wikiActions.className = 'kb-source-actions ui-table-actions ui-table-actions--end';
                wiki.insertCell().append(wikiActions);
                wikiActions.append(button('语义索引', event => {
                    semanticIndex('/api/knowledge/external/wiki-semantic-index',
                        event.currentTarget).catch(error => showToast(error.message, 'warning'));
                }));
                const personal = body.insertRow();
                for (const value of ['个人知识库', '本账号普通文档', '当前修订', '—', '支持混合检索']) {
                    const cell = personal.insertCell();
                    cell.textContent = value;
                    cell.title = value;
                }
                const personalActions = document.createElement('div');
                personalActions.className = 'kb-source-actions ui-table-actions ui-table-actions--end';
                personal.insertCell().append(personalActions);
                personalActions.append(button('语义索引', event => {
                    semanticIndex('/api/knowledge/semantic-index', event.currentTarget)
                        .catch(error => showToast(error.message, 'warning'));
                }));
                personalActions.append(button('文档理解', event => {
                    documentUnderstanding(event.currentTarget).catch(error => showToast(error.message, 'warning'));
                }));
                const partner = body.insertRow();
                partner.dataset.kbSource = 'gms_requirements';
                const scan = partnerScan?.status;
                const coverage = partnerIndex?.coverage;
                const latest = partnerIndex?.latest_scan;
                const indexed = partnerIndex?.index_consistent === true;
                const scanning = scan?.running && (scan.source || []).includes('gms_requirements');
                const failed = ['failed', 'interrupted', 'partial'].includes(latest?.state)
                    || (!scan?.running && ['failed', 'interrupted', 'partial'].includes(scan?.state)
                        && (scan.source || []).includes('gms_requirements'));
                const pending = ['pending', 'running'].includes(latest?.state);
                const count = partnerIndex === null ? '—' : indexed ? String(coverage.clause_candidates)
                    : coverage ? '—' : '0';
                const status = scanning ? '同步中' : !partnerIndex ? '状态不可用'
                    : pending && scan?.running ? '同步中'
                    : failed || (pending && scan && !scan.running) ? (indexed ? '同步失败，保留旧索引' : '同步失败')
                    : partnerIndex.latest_confirmed ? '已同步（待核验）'
                    : indexed ? '索引待更新' : coverage ? '索引异常' : '待同步';
                for (const value of ['GMS Certification Requirements', partnerUrl, '多版本', count, status]) {
                    const cell = partner.insertCell();
                    cell.textContent = value;
                    cell.title = value;
                }
                partner.cells[3].title = indexed
                    ? `条款候选：${coverage.clause_candidates}；章节：${coverage.sections ?? 0}；表格行：${coverage.table_rows ?? 0}`
                    : count;
                partner.cells[4].title = status + (partnerIndex?.indexed_at ? '；索引时间：' + partnerIndex.indexed_at : '');
                const actions = document.createElement('div');
                actions.className = 'kb-source-actions ui-table-actions ui-table-actions--end';
                partner.insertCell().append(actions);
                const sync = button('同步', event => {
                    run(async () => {
                        const result = await kbFetch(partnerApi + '/scan?mode=full&source=gms_requirements', {method: 'POST'});
                        showToast(result.status?.running ? 'GMS Requirements 同步已启动，完成后刷新状态'
                            : '已提交同步请求，请刷新核查结果', 'info');
                    }, event.currentTarget, '同步中');
                });
                sync.disabled = Boolean(scan?.running);
                sync.title = scan?.running ? '已有 GMS 更新扫描运行中' : '使用现有授权来源重新扫描 GMS Requirements';
                actions.append(sync, button('GMS 更新中心', () => { window.location.href = '/gms-update-center'; }),
                    button('导入授权文档', openPolicyImport), openUrlButton(partnerUrl));
                list.replaceChildren(table);
            }
            async function run(action, trigger = refreshButton, label = '刷新中') {
                if (busy) return;
                busy = true;
                setActionButtonBusy(trigger, true, label);
                refreshButton.disabled = true;
                submitButton.disabled = true;
                panel.setAttribute('aria-busy', 'true');
                try {
                    await action();
                    await load();
                } catch (error) {
                    showToast(error.message, 'warning');
                } finally {
                    busy = false;
                    setActionButtonBusy(trigger, false);
                    refreshButton.disabled = false;
                    submitButton.disabled = false;
                    submitButton.textContent = editing ? '保存' : '添加';
                    panel.setAttribute('aria-busy', 'false');
                }
            }
            form.addEventListener('submit', event => {
                event.preventDefault();
                run(async () => {
                    const values = new FormData(form);
                    const url = String(values.get('url') || '').trim();
                    const name = String(values.get('name') || '').trim();
                    const parsed = new URL(url);
                    if (!editing && parsed.protocol === 'https:' && parsed.hostname === 'docs.partner.android.com'
                            && !parsed.username && !parsed.password && !parsed.port
                            && parsed.pathname.replace(/\/$/, '') === '/gms/policies/domains/reqs') {
                        await openPolicyImport();
                        reset();
                        return;
                    }
                    const payload = {url, name};
                    if (!editing && !(parsed.protocol === 'https:' && parsed.hostname === 'source.android.com'
                            && parsed.pathname.startsWith('/docs/'))) {
                        if (!await window.showConfirmDialog('确认知识源使用权限', '请确认此公开文档允许在本系统中建立共享索引，并允许已配置的 AI 模型处理相关内容。')) return;
                        payload.authorized_for_ai_processing = true;
                    }
                    if (!editing) setActionButtonBusy(submitButton, true, '入库中');
                    const result = await kbFetch(api + (editing ? '/' + encodeURIComponent(editing.id) : ''), {
                        method: editing ? 'PUT' : 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    reset();
                    if (result.sync?.status === 'error') {
                        showToast('知识源已添加，首次同步失败，可重试同步', 'warning');
                    } else if (result.sync?.status === 'in_progress') {
                        showToast('知识源正在同步', 'info');
                    } else {
                        showToast(result.sync?.status === 'ready' ? '知识源已同步入库' : '知识源已保存', 'success');
                    }
                }, submitButton, editing ? '保存中' : '添加中');
            });
            resetButton.addEventListener('click', reset);
            refreshButton.addEventListener('click', () => run(async () => {}));
            const tabs = [...page.querySelectorAll('[data-kb-view]')];
            function switchView(view) {
                const selected = view === 'external' ? 'external' : 'personal';
                const external = selected === 'external';
                panel.hidden = !external;
                panel.style.display = external ? '' : 'none';
                personal.style.display = external ? 'none' : '';
                for (const tab of tabs) {
                    const active = tab.dataset.kbView === selected;
                    tab.classList.toggle('active', active);
                    tab.setAttribute('aria-selected', String(active));
                }
                try { sessionStorage.setItem('gms_knowledge_view', selected); } catch (_error) {}
                if (external) run(async () => {});
            }
            for (const tab of tabs) {
                tab.addEventListener('click', () => switchView(tab.dataset.kbView));
            }
            let saved = 'personal';
            try { saved = sessionStorage.getItem('gms_knowledge_view'); } catch (_error) {}
            switchView(saved === 'external' && state.elevated ? 'external' : 'personal');
        }

        async function kbRefreshAll() {
            // 三个加载互不依赖各自的返回值，并发跑省一次往返。
            await Promise.all([kbLoadSpaces(), kbLoadTree(), kbLoadDocs()]);
        }

        async function kbLoadSpaces() {
            const data = await kbFetch('/api/knowledge/spaces');
            kbState.spaces = data.spaces || [];
            if (!kbState.spaces.find(s => s.space_id === kbState.currentSpace) && kbState.spaces.length) kbState.currentSpace = kbState.spaces[0].space_id;
            const box = document.getElementById('kb-space-list');
            if (!box) return;
            box.innerHTML = kbState.spaces.map(sp => `
                <button class="btn-xs kb-nav-button${sp.space_id === kbState.currentSpace ? ' is-selected' : ''}" data-click="kbSelectSpace" data-a0="${kbEsc(sp.space_id)}">
                    ${kbEsc(sp.name)} <span style="color:var(--text-secondary)">(${sp.doc_count || 0})</span>
                </button>
            `).join('');
        }

        async function kbLoadTree() {
            const data = await kbFetch('/api/knowledge/tree?space_id=' + encodeURIComponent(kbState.currentSpace));
            kbState.nodes = data.nodes || [];
            const tree = document.getElementById('kb-tree');
            if (!tree) return;
            const children = {};
            kbState.nodes.forEach(n => {
                const pid = n.parent_id || '';
                (children[pid] = children[pid] || []).push(n);
            });
            function render(pid, depth) {
                return (children[pid] || []).map(n => {
                    const isDoc = n.type === 'doc';
                    const active = (isDoc && n.doc_id === kbState.currentDocId) || (!isDoc && n.node_id === kbState.currentParent);
                    const click = isDoc ? 'kbOpenDoc' : 'kbSelectFolder';
                    const clickArg = isDoc ? kbEsc(n.doc_id) : kbEsc(n.node_id);
                    return `
                        <div>
                            <button class="btn-xs kb-nav-button kb-nav-node${active ? ' is-selected' : ''}" style="--kb-indent:${6 + depth * 14}px" data-click="${click}" data-a0="${clickArg}">
                                ${isDoc ? '📄' : '📁'} ${kbEsc(n.title)}
                            </button>
                            ${!isDoc ? render(n.node_id, depth + 1) : ''}
                        </div>
                    `;
                }).join('');
            }
            tree.innerHTML = `<button class="btn-xs kb-nav-button${kbState.currentParent ? '' : ' is-selected'}" data-click="kbSelectFolder" data-a0="">全部文档</button>` + (render('', 0) || '<div class="ui-empty-state ui-empty-state--compact" data-ui-type-size="body">暂无目录</div>');
        }

        async function kbLoadDocs() {
            const box = document.getElementById('kb-doc-list');
            const requestGeneration = ++kbState.docsRequestGeneration;
            const hadRenderedDocs = kbState.docsLoaded;
            const searchButton = document.querySelector('#page-notes [data-click="kbSearch"]');
            setActionButtonBusy(searchButton, true, '搜索中');
            if (box) {
                box.setAttribute('aria-busy', 'true');
                if (!hadRenderedDocs) {
                    box.innerHTML = '<div class="ui-empty-state">加载中...</div>';
                }
            }
            const q = (document.getElementById('kb-search-input') || {}).value || '';
            const params = new URLSearchParams();
            params.set('space_id', kbState.currentSpace);
            if (q.trim()) params.set('q', q.trim());
            if (!q.trim() && kbState.currentParent) params.set('parent_id', kbState.currentParent);
            const url = q.trim() ? '/api/knowledge/search?' + params.toString() : '/api/knowledge/docs?' + params.toString();
            try {
                const data = await kbFetch(url);
                if (requestGeneration !== kbState.docsRequestGeneration) return;
                const docs = data.docs || data.items || [];
                if (!box) return;
                if (!docs.length) {
                    box.innerHTML = '<div class="ui-empty-state">暂无文档</div>';
                    kbState.docsLoaded = true;
                    return;
                }
                box.innerHTML = docs.map(doc => `
                    <div class="kb-doc-card ui-card${doc.doc_id === kbState.currentDocId ? ' is-selected' : ''}" data-doc-id="${kbEsc(doc.doc_id)}" data-click="kbOpenDoc" data-a0="${kbEsc(doc.doc_id)}">
                        <div class="kb-doc-title">${doc.favorite ? '★ ' : ''}${kbEsc(doc.title || '无标题')}</div>
                        <div class="kb-doc-summary">${kbEsc(doc.summary || kbSnippet(doc.content_md || doc.raw_content, 110))}</div>
                        <div class="kb-doc-tags">${(doc.tags || []).map(t => `<span class="ui-status is-neutral">${kbEsc(t)}</span>`).join(' ')}</div>
                    </div>
                `).join('');
                kbState.docsLoaded = true;
            } catch (error) {
                if (requestGeneration !== kbState.docsRequestGeneration) return;
                if (hadRenderedDocs) showToast('文档列表刷新失败: ' + error.message, 'error');
                else if (box) box.innerHTML = `<div class="ui-empty-state is-error">加载失败: ${kbEsc(error.message)}</div>`;
            } finally {
                if (requestGeneration === kbState.docsRequestGeneration) {
                    box?.setAttribute('aria-busy', 'false');
                    setActionButtonBusy(searchButton, false);
                }
            }
        }

        function kbSelectSpace(spaceId) {
            kbState.currentSpace = spaceId || 'gms';
            kbState.currentParent = '';
            kbState.currentDocId = '';
            kbClearEditor();
            kbRefreshAll();
        }

        function kbSelectFolder(nodeId) {
            kbState.currentParent = nodeId || '';
            kbLoadTree();
            kbLoadDocs();
        }

        function kbClearEditor() {
            const title = document.getElementById('kb-doc-title');
            const tags = document.getElementById('kb-doc-tags');
            const content = document.getElementById('kb-doc-content');
            if (title) title.value = '';
            if (tags) tags.value = '';
            if (content) content.value = '';
            kbState.currentDocId = '';
            kbState.currentNodeId = '';
            kbState.currentFavorite = false;
            kbState.dirty = false;
            const info = document.getElementById('kb-side-info');
            if (info) info.innerHTML = '选择或新建一篇文档。';
        }

        async function kbOpenDoc(docId) {
            const doc = await kbFetch('/api/knowledge/docs/' + encodeURIComponent(docId));
            kbState.currentDocId = doc.doc_id;
            kbState.currentNodeId = doc.node_id;
            kbState.currentFavorite = !!doc.favorite;
            kbState.currentParent = doc.parent_id || '';
            document.getElementById('kb-doc-title').value = doc.title || '';
            document.getElementById('kb-doc-tags').value = (doc.tags || []).join(', ');
            document.getElementById('kb-doc-content').value = doc.content_md || '';
            kbState.dirty = false;
            kbRenderInfo(doc);
            kbLoadTree();
            kbLoadDocs();
        }

        function kbRenderInfo(doc) {
            const info = document.getElementById('kb-side-info');
            if (!info) return;
            const links = doc.links || [];
            const attachments = doc.attachments || [];
            info.innerHTML = `
                <div><b>更新：</b>${kbEsc(doc.updated_at || '')}</div>
                <div><b>来源：</b>${kbEsc(doc.source || 'manual')}</div>
                ${doc.summary ? `<div style="margin-top:6px;"><b>摘要：</b>${kbEsc(doc.summary)}</div>` : ''}
                ${links.length ? `<div style="margin-top:6px;"><b>关联：</b><div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;">${links.map(l => `<button type="button" class="btn-xs" data-link-type="${kbEsc(l.target_type)}" data-link-id="${kbEsc(l.target_id)}" data-click="kbOpenKnowledgeLink" data-r0="dataset.linkType" data-r1="dataset.linkId">${kbEsc(kbKnowledgeLinkLabel(l))}</button>`).join('')}</div></div>` : ''}
                ${attachments.length ? `<div style="margin-top:6px;"><b>附件：</b><div style="margin-top:4px;">${attachments.map(a => `<a class="btn-xs" style="display:inline-block;margin:0 4px 4px 0;text-decoration:none;" href="/api/knowledge/docs/${encodeURIComponent(doc.doc_id)}/attachments/${encodeURIComponent(a.attachment_id)}/download">${kbEsc(a.original_name)}</a>`).join('')}</div></div>` : ''}
            `;
        }

        function kbKnowledgeLinkLabel(link) {
            const labels = {test_report:'测试报告', redmine_issue:'Redmine', gerrit_change:'Gerrit', test_case:'测试用例'};
            return `${labels[link.target_type] || link.target_type}: ${link.title || link.target_id}`;
        }

        function kbOpenKnowledgeLink(type, id) {
            const value = String(id || '').trim();
            if (!value) return;
            if (type === 'test_report') {
                if (typeof analyzeReport === 'function') analyzeReport(value);
                else window.GmsWorkspace?.navigate('reports', {report_timestamp:value, origin_page:'notes'});
                return;
            }
            if (type === 'redmine_issue') {
                window.GmsWorkspace?.navigate('redmine-agent', {redmine_issue_id:value, origin_page:'notes'});
                return;
            }
            if (type === 'gerrit_change') {
                window.GmsWorkspace?.navigate('gerrit-dashboard', {gerrit_change_id:value, origin_page:'notes'});
                return;
            }
            if (type === 'test_case') {
                window.GmsWorkspace?.navigate('test', {origin_page:'notes'});
            }
        }

        async function kbNewSpace() {
            const result = await GmsDialogs.form('知识库名称', [{name: 'value', label: '知识库名称', required: true}]);
            const name = result?.value;
            if (!name || !name.trim()) return;
            const sp = await kbFetch('/api/knowledge/spaces', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name.trim()})});
            kbState.currentSpace = sp.space_id;
            await kbRefreshAll();
        }

        async function kbNewFolder() {
            const result = await GmsDialogs.form('目录名称', [{name: 'value', label: '目录名称', required: true}]);
            const title = result?.value;
            if (!title || !title.trim()) return;
            await kbFetch('/api/knowledge/folders', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({space_id:kbState.currentSpace, parent_id:kbState.currentParent, title:title.trim()})});
            await kbLoadTree();
        }

        async function kbNewDoc() {
            const doc = await kbFetch('/api/knowledge/docs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({space_id:kbState.currentSpace, parent_id:kbState.currentParent, title:'新文档', content_md:'# 新文档\n\n'})});
            await kbRefreshAll();
            await kbOpenDoc(doc.doc_id);
        }

        async function kbSaveDoc() {
            const title = (document.getElementById('kb-doc-title') || {}).value || '';
            const tags = (document.getElementById('kb-doc-tags') || {}).value || '';
            const content = (document.getElementById('kb-doc-content') || {}).value || '';
            if (!content.trim()) { showToast('文档内容不能为空', 'warning'); return; }
            let doc;
            if (kbState.currentDocId) {
                doc = await kbFetch('/api/knowledge/docs/' + encodeURIComponent(kbState.currentDocId), {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title, tags, content_md:content, raw_content:content})});
            } else {
                doc = await kbFetch('/api/knowledge/docs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({space_id:kbState.currentSpace, parent_id:kbState.currentParent, title, tags, content_md:content})});
            }
            kbState.dirty = false;
            showToast('已保存', 'success');
            await kbRefreshAll();
            await kbOpenDoc(doc.doc_id);
        }

        async function kbToggleFavorite() {
            if (!kbState.currentDocId) return;
            const doc = await kbFetch('/api/knowledge/docs/' + encodeURIComponent(kbState.currentDocId), {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({favorite: kbState.currentFavorite ? 0 : 1})});
            await kbOpenDoc(doc.doc_id);
        }

        async function kbShowHistory() {
            if (!kbState.currentDocId) { showToast('请先选择文档', 'warning'); return; }
            const data = await kbFetch('/api/knowledge/docs/' + encodeURIComponent(kbState.currentDocId) + '/versions');
            const versions = data.versions || [];
            const info = document.getElementById('kb-side-info');
            if (!info) return;
            info.innerHTML = `<div style="font-weight:var(--ui-type-weight-semibold);margin-bottom:8px;">版本历史</div>` + (versions.length
                ? versions.map(version => `<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;padding-bottom:6px;border-bottom:1px solid var(--border-color);">
                    <span style="flex:1;">v${version.version_no} · ${kbEsc(version.created_at)}<br>${kbEsc(version.title)}</span>
                    <button class="btn-xs" data-version-id="${kbEsc(version.version_id)}" data-click="kbRestoreVersion" data-r0="dataset.versionId">恢复</button>
                  </div>`).join('')
                : '<div>暂无历史版本</div>');
        }

        async function kbRestoreVersion(versionId) {
            if (!kbState.currentDocId || !versionId) return;
            if (!await showConfirmDialog(
                '恢复历史版本',
                '恢复这个历史版本？\n当前内容也会保留为新版本。'
            )) return;
            const doc = await kbFetch('/api/knowledge/docs/' + encodeURIComponent(kbState.currentDocId) + '/versions/' + encodeURIComponent(versionId) + '/restore', {method:'POST'});
            await kbOpenDoc(doc.doc_id);
            showToast('历史版本已恢复', 'success');
        }

        async function kbDeleteCurrent() {
            if (!kbState.currentNodeId) return;
            if (!await showConfirmDialog(
                '删除知识库文档',
                '确定删除当前文档？此操作不可恢复。'
            )) return;
            await kbFetch('/api/knowledge/nodes/' + encodeURIComponent(kbState.currentNodeId), {method:'DELETE'});
            kbClearEditor();
            await kbRefreshAll();
        }

        function kbSearch() { kbLoadDocs(); }

        function kbOpenUpload(policy = false) {
            kbState.policyImport = policy === true;
            const input = document.getElementById('kb-upload-input');
            if (!input.dataset.defaultAccept) input.dataset.defaultAccept = input.accept || '*/*';
            input.accept = kbState.policyImport ? '.pdf,.html,.htm' : input.dataset.defaultAccept;
            input.value = '';
            document.getElementById('kb-upload-status').textContent = '';
            ModalManager.open('kb-upload-modal');
        }
        function kbCloseUpload() { ModalManager.close('kb-upload-modal'); }

        async function kbHandleFileSelect(input) {
            const files = Array.from(input.files || []);
            const status = document.getElementById('kb-upload-status');
            if (!files.length) return;
            try {
                for (let i = 0; i < files.length; i++) {
                    if (status) status.textContent = `上传解析中 ${i + 1}/${files.length}: ${files[i].name}`;
                    const form = new FormData();
                    form.append('file', files[i]);
                    form.append('space_id', kbState.currentSpace);
                    form.append('parent_id', kbState.currentParent);
                    if (kbState.policyImport) form.append('tags', 'external-policy-ai-approved');
                    const doc = await kbFetch('/api/knowledge/upload', {method:'POST', body:form});
                    kbState.currentDocId = doc.doc_id;
                }
                kbCloseUpload();
                await kbRefreshAll();
                if (kbState.currentDocId) await kbOpenDoc(kbState.currentDocId);
            } catch (e) {
                if (status) status.textContent = e.message;
            }
        }

        async function kbAsk() {
            const result = await GmsDialogs.form('智能知识问答', [
                {name: 'value', label: '问题', required: true, multiline: true}
            ], {message: '本地 LLM 将主动检索、阅读原文及复核结论。'});
            const question = result?.value;
            if (!question || !question.trim()) return;
            const includeImports = await window.showConfirmDialog('授权知识源',
                '是否同时检索本人已授权导入的规范及公开网页？取消则仅检索当前个人知识空间。');
            const info = document.getElementById('kb-side-info');
            if (info) info.textContent = '本地 RAG 正在规划、检索和复核，请以实际完成结果为准。';
            const data = await kbFetch('/api/knowledge/ask', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({question: question.trim(), space_id: includeImports ? '' : kbState.currentSpace,
                    include_external: !!includeImports, include_authorized_imports: !!includeImports})
            });
            if (info) {
                const contexts = Array.isArray(data.contexts) ? data.contexts : [];
                const steps = Array.isArray(data.rag?.steps) ? data.rag.steps : [];
                const labels = {plan: '问题规划', search: '检索', read: '原文阅读', inspect: '证据检查',
                    draft: '综合分析', review: '结论复核', model: '模型调用'};
                const trace = steps.length ? `
                    <details><summary>执行记录（${steps.length}）</summary>
                    ${steps.map(step => `<div>${kbEsc(labels[step.stage] || step.stage)}：
                        ${kbEsc(step.source || '')} ${kbEsc(step.status || '')}
                        ${Number.isInteger(step.new_evidence) ? ' · 新证据 ' + step.new_evidence : ''}</div>`).join('')}
                    </details>` : '';
                const sources = contexts.length ? `
                    <div style="font-weight:var(--ui-type-weight-semibold);margin-top:10px;margin-bottom:6px;">来源</div>
                    ${contexts.map((ctx, i) => {
                        const title = '[' + (ctx.evidence_id || i + 1) + '] ' + (ctx.title || '无标题');
                        const heading = ctx.doc_id
                            ? `<button class="btn-xs" data-click="kbOpenDoc" data-a0="${kbEsc(ctx.doc_id)}">${kbEsc(title)}</button>`
                            : `<strong>${kbEsc(title)}</strong>`;
                        return `<details style="border:1px solid var(--border-color);border-radius:6px;padding:6px;margin-bottom:6px;">
                            <summary>${heading}</summary>
                            <div style="color:var(--text-secondary);white-space:pre-wrap;overflow-wrap:anywhere;">${kbEsc(ctx.snippet || '')}</div>
                            <div style="color:var(--text-secondary);overflow-wrap:anywhere;">${kbEsc(ctx.reference || '')} · ${kbEsc(ctx.source_revision || '')}</div>
                            <div style="color:var(--text-secondary);overflow-wrap:anywhere;">${kbEsc(ctx.source_url || '')}</div>
                        </details>`;
                    }).join('')}
                ` : '';
                info.innerHTML = `
                    <div style="font-weight:var(--ui-type-weight-semibold);margin-bottom:6px;">智能问答
                    ${data.provider ? `<span style="font-weight:normal;color:var(--text-secondary);">(${kbEsc(data.provider)})</span>` : ''}</div>
                    <div style="white-space:pre-wrap;color:var(--text-color);line-height:1.6;">${kbEsc(data.answer || '')}</div>
                    ${trace}${sources}
                `;
            }
        }

        Object.assign(window, {initNotesPage, kbNewSpace, kbSelectSpace, kbSelectFolder, kbNewFolder, kbNewDoc, kbOpenDoc, kbSaveDoc, kbToggleFavorite, kbShowHistory, kbRestoreVersion, kbDeleteCurrent, kbSearch, kbOpenUpload, kbCloseUpload, kbHandleFileSelect, kbAsk, kbOpenKnowledgeLink});

        async function saveToWiki(payload) {
            const notebookSpaces = {
                '测试问题库': 'issues', 'Redmine问题沉淀': 'issues',
                'Gerrit补丁说明': 'issues', '设备接入文档': 'devices',
                '固件烧录文档': 'devices', 'FAQ': 'gms'
            };
            const tags = payload.tags || (payload.notebook ? [payload.notebook] : '');
            const body = {
                space_id: payload.space_id || notebookSpaces[payload.notebook] || 'issues',
                title: payload.title || '',
                content_md: payload.content || payload.content_md || '',
                tags,
                source: payload.source || 'diagnosis',
                links: Array.isArray(payload.links) ? payload.links : [],
            };
            const resp = await fetch('/api/knowledge/docs', {method:'POST', headers:{'Content-Type':'application/json'}, credentials:'same-origin', body:JSON.stringify(body)});
            const result = await resp.json().catch(() => ({}));
            if (!resp.ok || result.success === false) throw new Error(result.error || result.message || '存入知识库失败');
            return result.data !== undefined ? result.data : result;
        }
        Object.assign(window, {saveToWiki});

