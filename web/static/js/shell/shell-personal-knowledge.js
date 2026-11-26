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
            const partnerApi = '/api/knowledge/external/partner-policy-grant';
            const partnerUrl = 'https://docs.partner.android.com/gms/policies/domains/reqs';
            let editing = null;
            let busy = false;
            let partnerEnabled = null;

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
                b.addEventListener('click', action);
                return b;
            }
            async function grantPartner(enabled) {
                if (enabled && !await window.showConfirmDialog('授权 Google Partner 政策分析', '确认当前账号有权访问 Google Partner 文档，并允许当前已启用的 AI 模型处理相关政策片段。模型目录改变后须重新授权。')) return;
                await kbFetch(partnerApi, {method: 'PUT', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({enabled, authorized_for_ai_processing: enabled})});
            }
            function sourceRequest(source, enabled) {
                return kbFetch(api + '/' + encodeURIComponent(source.id), {
                    method: 'PUT', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url: source.url, name: source.name || '', enabled})
                });
            }
            async function load() {
                const data = await kbFetch(api);
                try {
                    const grant = await kbFetch(partnerApi);
                    partnerEnabled = Boolean(grant.enabled);
                } catch (error) {
                    partnerEnabled = null;
                    showToast(error.message, 'warning');
                }
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
                    actions.className = 'kb-source-actions';
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
                }
                const partner = body.insertRow();
                for (const value of ['GMS Certification Requirements', partnerUrl, '通用', '—',
                    partnerEnabled === null ? '授权状态未知' : partnerEnabled ? '已授权' : '待授权']) {
                    const cell = partner.insertCell();
                    cell.textContent = value;
                    cell.title = value;
                }
                const actions = document.createElement('div');
                actions.className = 'kb-source-actions';
                partner.insertCell().append(actions);
                actions.append(button(partnerEnabled ? '撤销授权' : '授权', () => run(() => grantPartner(!partnerEnabled))),
                    button('GMS 更新中心', () => { window.location.href = '/gms-update-center'; }),
                    button('导入授权文档', async () => {
                        if (await window.showConfirmDialog('授权规范导入', '仅导入你有权访问且允许当前已启用 AI 模型处理的 PDF/HTML 文件。文档只属于当前账号，修改后需要重新授权。')) kbOpenUpload(true);
                    }));
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
                        if (!partnerEnabled) await grantPartner(true);
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
                <button class="btn-xs" style="width:100%;margin-bottom:6px;text-align:left;${sp.space_id === kbState.currentSpace ? 'border-color:var(--primary-color);' : ''}" data-click="kbSelectSpace" data-a0="${kbEsc(sp.space_id)}">
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
                            <button class="btn-xs" style="width:100%;margin-bottom:4px;text-align:left;padding-left:${6 + depth * 14}px;${active ? 'border-color:var(--primary-color);' : ''}" data-click="${click}" data-a0="${clickArg}">
                                ${isDoc ? '📄' : '📁'} ${kbEsc(n.title)}
                            </button>
                            ${!isDoc ? render(n.node_id, depth + 1) : ''}
                        </div>
                    `;
                }).join('');
            }
            tree.innerHTML = `<button class="btn-xs" style="width:100%;margin-bottom:6px;text-align:left;${kbState.currentParent ? '' : 'border-color:var(--primary-color);'}" data-click="kbSelectFolder" data-a0="">全部文档</button>` + (render('', 0) || '<div class="suite-empty" data-ui-type-size="body" style="padding:8px;">暂无目录</div>');
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
                    box.innerHTML = '<div class="suite-empty" style="padding:20px;text-align:center;">加载中...</div>';
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
                    box.innerHTML = '<div class="suite-empty" style="padding:28px;text-align:center;">暂无文档</div>';
                    kbState.docsLoaded = true;
                    return;
                }
                box.innerHTML = docs.map(doc => `
                    <div class="report-failure-card" data-doc-id="${kbEsc(doc.doc_id)}" style="cursor:pointer;margin-bottom:8px;${doc.doc_id === kbState.currentDocId ? 'border-color:var(--primary-color);' : ''}" data-click="kbOpenDoc" data-a0="${kbEsc(doc.doc_id)}">
                        <div class="kb-doc-title" style="margin-bottom:4px;">${doc.favorite ? '★ ' : ''}${kbEsc(doc.title || '无标题')}</div>
                        <div class="kb-doc-summary" style="color:var(--text-secondary);line-height:1.5;">${kbEsc(doc.summary || kbSnippet(doc.content_md || doc.raw_content, 110))}</div>
                        <div style="margin-top:6px;">${(doc.tags || []).map(t => `<span class="badge">${kbEsc(t)}</span>`).join(' ')}</div>
                    </div>
                `).join('');
                kbState.docsLoaded = true;
            } catch (error) {
                if (requestGeneration !== kbState.docsRequestGeneration) return;
                if (hadRenderedDocs) showToast('文档列表刷新失败: ' + error.message, 'error');
                else if (box) box.innerHTML = `<div class="suite-empty" style="padding:20px;text-align:center;">加载失败: ${kbEsc(error.message)}</div>`;
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
            const result = await GmsDialogs.form('全库提问', [{name: 'value', label: '全库提问', required: true, multiline: true}]);
            const question = result?.value;
            if (!question || !question.trim()) return;
            const data = await kbFetch('/api/knowledge/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question:question.trim(), space_id:kbState.currentSpace})});
            const info = document.getElementById('kb-side-info');
            if (info) {
                const contexts = Array.isArray(data.contexts) ? data.contexts : [];
                const sources = contexts.length ? `
                    <div style="font-weight:var(--ui-type-weight-semibold);margin-top:10px;margin-bottom:6px;">来源</div>
                    ${contexts.map((ctx, i) => `
                        <div style="border:1px solid var(--border-color);border-radius:6px;padding:6px;margin-bottom:6px;">
                            <button class="btn-xs" data-click="kbOpenDoc" data-a0="${kbEsc(ctx.doc_id)}" style="margin-bottom:4px;">[${i + 1}] ${kbEsc(ctx.title || '无标题')}</button>
                            <div style="color:var(--text-secondary);line-height:1.5;">${kbEsc(kbSnippet(ctx.snippet || ctx.summary || '', 180))}</div>
                        </div>
                    `).join('')}
                ` : '';
                info.innerHTML = `
                    <div style="font-weight:var(--ui-type-weight-semibold);margin-bottom:6px;">问答结果 ${data.mode === 'ai' && data.provider ? `<span style="font-weight:normal;color:var(--text-secondary);">(${kbEsc(data.provider)})</span>` : ''}</div>
                    <div style="white-space:pre-wrap;color:var(--text-color);line-height:1.6;">${kbEsc(data.answer || '')}</div>
                    ${sources}
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

