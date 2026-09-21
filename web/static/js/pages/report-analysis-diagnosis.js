/**
 * 报告诊断 - 召回结果面板渲染（从 report-analysis.js 拆出）。
 *
 * ADR 0014：`system_background_results` 是 Android 系统机制背景知识分栏，
 * 与历史案例（knowledge_base_results）、源码检索（source_search_results）
 * 明确分开；每条强制展示 provenance 与"未验证"脚注。
 *
 * 依赖：report-analysis.js 主文件的全局 helper（$, escapeHtml, escapeJsAttr,
 * safeReportExternalUrl, renderDxEmpty）。加载顺序：本文件在主文件之前或
 * 之后均可（渲染函数在 diagnose 响应返回后才被调用）。
 */

function renderReportSourceCards(sourceResults) {
    const results = sourceResults || [];
    return results.length > 0
        ? results.map(item => {
            const itemUrl = safeReportExternalUrl(item.url);
            return `
            <div class="dx-list-item${itemUrl ? ' dx-clickable' : ''}" ${itemUrl ? `data-click="_actOpenUrlBlank" data-a0="${escapeJsAttr(itemUrl)}"` : ''}>
                <div class="dx-list-head">
                    <div class="dx-list-title">${escapeHtml(item.type || 'source')}</div>
                    ${itemUrl ? `<a class="dx-link" href="${escapeHtml(itemUrl)}" target="_blank" rel="noopener" data-click="_actStopPropagation">打开源码</a>` : ''}
                </div>
                <div class="dx-list-path dx-list-path-inline">${escapeHtml(item.path || item.display_path || '')}${item.line ? `<span>:${escapeHtml(String(item.line))}</span>` : ''}</div>
            </div>
        `;
        }).join('')
        : renderDxEmpty('未检索到源码结果');
}

function renderReportKbCards(kbResults) {
    const results = kbResults || [];
    return results.length > 0
        ? results.map(item => `
            <div class="dx-list-item">
                <div class="dx-list-title">#${escapeHtml(String(item.id || ''))} ${escapeHtml(item.subject || '')}</div>
                <div class="dx-list-meta">${escapeHtml(item.status_name || '')} | ${escapeHtml(item.updated_on || '')}</div>
                <div class="dx-list-text">${escapeHtml((item.solution_summary || item.description || '').slice(0, 260))}</div>
            </div>
        `).join('')
        : renderDxEmpty('未命中知识库');
}

/**
 * 系统机制背景知识面板（ADR 0014）。background-only：
 * 展示机制解释与 provenance，明确标注未经过设备/源码证据验证。
 */
function renderReportSystemBackgroundPanel(backgroundResults) {
    const results = Array.isArray(backgroundResults) ? backgroundResults : [];
    if (results.length === 0) return '';
    const cards = results.map(item => {
        const meta = [
            item.applicable_versions ? `适用: ${item.applicable_versions}` : '',
            item.confidence ? `置信: ${item.confidence}` : '',
            item.last_verified ? `验证于: ${item.last_verified}` : '',
        ].filter(Boolean).join(' | ');
        return `
            <div class="dx-list-item">
                <div class="dx-list-head">
                    <div class="dx-list-title">${escapeHtml(item.title || '机制条目')}${item.chapter ? `<span class="dx-list-meta"> · ${escapeHtml(String(item.chapter))}</span>` : ''}</div>
                </div>
                <div class="dx-list-text">${escapeHtml((item.snippet || '').slice(0, 300))}</div>
                <div class="dx-list-meta">${escapeHtml(meta || 'provenance 缺失')}</div>
                <div class="dx-list-meta">来源: ${escapeHtml(item.source || 'android_internals')} @ ${escapeHtml((item.source_revision || '').slice(0, 8))} · ${escapeHtml(item.source_path || '')} · ${escapeHtml(item.license || '')}</div>
            </div>
        `;
    }).join('');
    return `
        <section class="dx-section dx-background-section">
            <div class="dx-section-title">系统机制背景 <span>${results.length} 条</span></div>
            <div class="dx-list">${cards}</div>
            <div class="dx-list-meta dx-background-note">背景知识：解释系统机制，尚未通过设备/源码证据验证，不得作为已证实根因。</div>
        </section>
    `;
}