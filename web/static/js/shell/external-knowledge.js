/**
 * 外部知识库（Android Internals Wiki）管理面板（ADR 0014）。
 *
 * revision 三态：available = 本地 clone HEAD / approved = 管理员批准 /
 * source_revision = 实际索引内容。批准与重建是显式管理员动作，前端只
 * 调用 /api/knowledge/external/*，不在本地维护任何状态。
 *
 * 页面元素全部由本文件按需注入（shell.html 不携带静态标记，避免挤占
 * 其字节预算与 UI 契约快照）；由 shell-late-init.js 在 notes 页存在时
 * 动态加载本文件。
 */

(function () {
    'use strict';

    const EK_MODAL_ID = 'external-knowledge-modal';

    function esc(text) {
        return String(text == null ? '' : text).replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function short(sha) {
        const text = String(sha || '');
        return text ? text.slice(0, 12) : '—';
    }

    async function ekFetch(url, options) {
        const response = await fetch(url, { credentials: 'same-origin', ...(options || {}) });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.success === false) {
            throw new Error(result.error || result.message || `HTTP ${response.status}`);
        }
        return result && result.data !== undefined ? result.data : result;
    }

    function renderStatus(sources) {
        const body = document.getElementById('ek-status-body');
        if (!body) return;
        const rows = Array.isArray(sources) ? sources : [];
        if (!rows.length) {
            body.textContent = '外部知识源未配置（fail-closed 禁用）。';
            return;
        }
        body.innerHTML = rows.map(s => {
            const badges = {
                ready: '<span style="color:var(--success-color)">● 就绪</span>',
                empty: '<span style="color:var(--warning-color)">● 索引为空</span>',
                error: '<span style="color:var(--danger-color)">● 异常</span>',
                disabled: '<span style="color:var(--text-secondary)">● 已禁用</span>',
                not_configured: '<span style="color:var(--text-secondary)">● 未配置</span>',
            };
            const statusBadge = badges[s.status] || `<span>${esc(s.status || '')}</span>`;
            const pending = s.available_revision && s.approved_revision
                && s.available_revision !== s.approved_revision;
            return `
                <div style="border:1px solid var(--border-color);border-radius:6px;padding:8px;margin-bottom:8px;line-height:1.8;">
                    <div><strong>${esc(s.source || '')}</strong> ${statusBadge}${s.doc_count != null ? ` · 文档 ${esc(String(s.doc_count))}` : ''}</div>
                    <div style="color:var(--text-secondary);">索引 revision: <code>${esc(short(s.source_revision))}</code></div>
                    <div style="color:var(--text-secondary);">已批准: <code>${esc(short(s.approved_revision))}</code> · 上游可用: <code>${esc(short(s.available_revision))}</code>${pending ? ' <span style="color:var(--warning-color)">（上游有新 revision，待批准后重建）</span>' : ''}</div>
                    ${s.last_sync_at ? `<div style="color:var(--text-secondary);">上次同步: ${esc(s.last_sync_at)}</div>` : ''}
                    ${s.license ? `<div style="color:var(--text-secondary);">License: ${esc(s.license)}</div>` : ''}
                    ${s.detail ? `<div style="color:var(--text-secondary);">${esc(s.detail)}</div>` : ''}
                </div>
            `;
        }).join('');
    }

    async function loadStatus() {
        const body = document.getElementById('ek-status-body');
        if (body) body.textContent = '加载中...';
        try {
            const data = await ekFetch('/api/knowledge/external/sources');
            renderStatus(data.sources);
        } catch (e) {
            if (body) body.textContent = `加载失败: ${e.message}`;
        }
    }

    function buildModal() {
        if (document.getElementById(EK_MODAL_ID)) return;
        const root = document.createElement('div');
        root.id = EK_MODAL_ID;
        root.className = 'modal';
        root.style.zIndex = '10092';
        root.innerHTML = `
            <div class="modal-content modal-s">
                <div class="modal-header">
                    <span class="modal-title">🔗 外部知识库 · Android Internals Wiki</span>
                    <button type="button" class="modal-close" aria-label="关闭" data-click="ekClose">&times;</button>
                </div>
                <div class="modal-body">
                    <div id="ek-status-body" class="modal-info-text" style="min-height:80px;">加载中...</div>
                    <div style="display:flex;gap:8px;margin-top:12px;">
                        <button class="btn-xs" data-click="ekRefreshStatus">刷新</button>
                        <button class="btn-xs" data-click="ekApproveRevision">批准当前上游 revision</button>
                        <button class="btn-xs btn-primary" data-click="ekReindex">重建索引</button>
                    </div>
                    <div class="modal-info-text" style="margin-top:10px;">
                        批准只更新目标 revision；重建索引才把已批准的上游内容真正索引进平台。上游 master 是移动目标，不建议自动跟随。
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(root);
    }

    function injectTitleButton() {
        const page = document.getElementById('page-notes');
        if (!page) return;
        const title = page.querySelector('.section-title');
        if (!title || title.closest('[data-ek-title-row]')) return;
        // 参照 adb-title-row 等页面惯例：标题行 flex 化（space-between），
        // 标题居左、动作按钮居右。flex 容器内 margin 不塌陷，.section-title
        // 自带的 6px margin-bottom 仍留在行盒内部，"标题→内容"间距与
        // 其他页面一致（runtime UI smoke 的 titleToContent/frame-top 契约）。
        const row = document.createElement('div');
        row.setAttribute('data-ek-title-row', '');
        row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;';
        title.parentNode.insertBefore(row, title);
        row.appendChild(title);
        // 保持 data-click 为源码中的声明式字面量，使 act-bridge 的冻结
        // UI 契约扫描与运行时实际属性使用同一锚点。
        row.insertAdjacentHTML(
            'beforeend',
            '<button type="button" class="btn-xs" data-click="ekOpen"></button>',
        );
        const actionButton = row.lastElementChild;
        // 高度压在标题行盒内（19px）：不撑高标题行，避免整体下移破坏
        // frame-top 几何断言。
        actionButton.style.cssText = 'height:19px;min-height:19px;line-height:17px;padding:0 8px;font-size:10px;flex:0 0 auto;';
        actionButton.title = '外部知识库（Android Internals Wiki）revision 状态与管理';
        actionButton.textContent = '🔗 外部知识库';
    }

    function open() { buildModal(); ModalManager.open(EK_MODAL_ID); loadStatus(); }
    function close() { ModalManager.close(EK_MODAL_ID); }
    function refreshStatus() { loadStatus(); }

    async function approveRevision() {
        if (!await showConfirmDialog('批准上游 revision', '将当前 clone HEAD 设为索引目标？批准后需执行「重建索引」才会生效。')) return;
        try {
            await ekFetch('/api/knowledge/external/approve-revision', { method: 'POST' });
            showToast('已批准当前 revision，执行「重建索引」以应用', 'success');
            loadStatus();
        } catch (e) {
            showToast(`批准失败: ${e.message}`, 'error');
        }
    }

    async function reindex() {
        if (!await showConfirmDialog('重建外部知识索引', '将按上游发布策略全量重建索引，期间检索可能短暂为空。继续？')) return;
        showToast('索引重建中...', 'info');
        try {
            const data = await ekFetch('/api/knowledge/external/reindex', { method: 'POST' });
            showToast(`重建完成：${(data && data.doc_count != null) ? `${data.doc_count} 篇` : 'OK'}`, 'success');
            loadStatus();
        } catch (e) {
            showToast(`重建失败: ${e.message}`, 'error');
        }
    }

    injectTitleButton();
    // 逐个 window.* 赋值导出（act-bridge 完整性测试按
    // window\.name= 模式识别惰性全局，Object.assign 形式不在识别范围）。
    window.ekOpen = open;
    window.ekClose = close;
    window.ekRefreshStatus = refreshStatus;
    window.ekApproveRevision = approveRevision;
    window.ekReindex = reindex;
})();
