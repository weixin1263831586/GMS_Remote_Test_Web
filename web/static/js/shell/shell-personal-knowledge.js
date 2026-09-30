        // ==================== 个人知识库 ====================
        const kbState = {
            initialized: false,
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

        async function kbFetch(url, options) {
            const response = await fetch(url, {credentials: 'same-origin', ...(options || {})});
            const result = await response.json().catch(() => ({}));
            if (!response.ok || result.success === false) throw new Error(result.error || result.message || `HTTP ${response.status}`);
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
            await kbRefreshAll();
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
            tree.innerHTML = `<button class="btn-xs" style="width:100%;margin-bottom:6px;text-align:left;${kbState.currentParent ? '' : 'border-color:var(--primary-color);'}" data-click="kbSelectFolder" data-a0="">全部文档</button>` + (render('', 0) || '<div class="suite-empty" style="font-size:12px;padding:8px;">暂无目录</div>');
        }

        async function kbLoadDocs() {
            const box = document.getElementById('kb-doc-list');
            const requestGeneration = ++kbState.docsRequestGeneration;
            const hadRenderedDocs = kbState.docsLoaded;
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
                        <div style="font-weight:600;margin-bottom:4px;">${doc.favorite ? '★ ' : ''}${kbEsc(doc.title || '无标题')}</div>
                        <div style="font-size:12px;color:var(--text-secondary);line-height:1.5;">${kbEsc(doc.summary || kbSnippet(doc.content_md || doc.raw_content, 110))}</div>
                        <div style="margin-top:6px;">${(doc.tags || []).map(t => `<span class="badge">${kbEsc(t)}</span>`).join(' ')}</div>
                    </div>
                `).join('');
                kbState.docsLoaded = true;
            } catch (error) {
                if (requestGeneration !== kbState.docsRequestGeneration) return;
                if (hadRenderedDocs) showToast('文档列表刷新失败: ' + error.message, 'error');
                else if (box) box.innerHTML = `<div class="suite-empty" style="padding:20px;text-align:center;">加载失败: ${kbEsc(error.message)}</div>`;
            } finally {
                if (requestGeneration === kbState.docsRequestGeneration && box) {
                    box.setAttribute('aria-busy', 'false');
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
            const name = prompt('知识库名称');
            if (!name || !name.trim()) return;
            const sp = await kbFetch('/api/knowledge/spaces', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name.trim()})});
            kbState.currentSpace = sp.space_id;
            await kbRefreshAll();
        }

        async function kbNewFolder() {
            const title = prompt('目录名称');
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
            info.innerHTML = `<div style="font-weight:600;margin-bottom:8px;">版本历史</div>` + (versions.length
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

        function kbOpenUpload() {
            document.getElementById('kb-upload-input').value = '';
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
            const question = prompt('全库提问');
            if (!question || !question.trim()) return;
            const data = await kbFetch('/api/knowledge/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question:question.trim(), space_id:kbState.currentSpace})});
            const info = document.getElementById('kb-side-info');
            if (info) {
                const contexts = Array.isArray(data.contexts) ? data.contexts : [];
                const sources = contexts.length ? `
                    <div style="font-weight:600;margin-top:10px;margin-bottom:6px;">来源</div>
                    ${contexts.map((ctx, i) => `
                        <div style="border:1px solid var(--border-color);border-radius:6px;padding:6px;margin-bottom:6px;">
                            <button class="btn-xs" data-click="kbOpenDoc" data-a0="${kbEsc(ctx.doc_id)}" style="margin-bottom:4px;">[${i + 1}] ${kbEsc(ctx.title || '无标题')}</button>
                            <div style="color:var(--text-secondary);line-height:1.5;">${kbEsc(kbSnippet(ctx.snippet || ctx.summary || '', 180))}</div>
                        </div>
                    `).join('')}
                ` : '';
                info.innerHTML = `
                    <div style="font-weight:600;margin-bottom:6px;">问答结果 ${data.mode === 'ai' && data.provider ? `<span style="font-weight:normal;color:var(--text-secondary);">(${kbEsc(data.provider)})</span>` : ''}</div>
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

