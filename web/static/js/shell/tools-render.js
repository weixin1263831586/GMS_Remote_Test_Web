// Shell 模块：常用工具/常用网址 卡片网格共用渲染器（两条渲染路径的唯一定义）。
// 网格列数/间距在 common-layout.css 的 .tools-grid；事件走 data-tools-action
// 委托（CSP 下无内联 onclick）；卡片拖拽沿用 act-bridge 的声明式属性。

function toolsRenderCategoryHeader({ category, icon, count, addButtonBackground }) {
    const safeCategory = escapeIconAttr(category);
    return `
            <div class="category-header">
                <div class="tools-category-title">
                    <span class="tools-category-icon">${escapeHtml(icon)}</span>
                    <button type="button" data-tools-action="edit-category" data-category="${safeCategory}" class="tools-category-name"><span class="category-name">${escapeHtml(category)}</span></button>
                    <span class="ui-status tools-category-count">${count}</span>
                </div>
                <div class="ui-action-group">
                    <button type="button" data-tools-action="add-tool" data-category="${safeCategory}" class="btn-xs btn-primary">
                        添加工具
                    </button>
                    <button type="button" data-tools-action="delete-category" data-category="${safeCategory}" class="btn-xs btn-danger"
                        title="删除分类">
                        🗑️
                    </button>
                </div>
            </div>`;
}

function toolsRenderCategorySection({ category, catIndex, icon, count, cardsHtml, addButtonBackground, sectionStyle = '' }) {
    const safeCategory = escapeIconAttr(category);
    return `
        <div class="category-section" data-category="${safeCategory}" data-category-index="${catIndex}" draggable="true"${sectionStyle ? ` style="${sectionStyle}"` : ''}>
${toolsRenderCategoryHeader({ category, icon, count, addButtonBackground })}
            <div class="tools-grid">
${cardsHtml}
            </div>
        </div>
    `;
}

function toolsRenderEmptyGrid({ icon, title, hint, padding = '60px 20px', iconSize = '64px' }) {
    return `
        <div class="ui-empty-state tools-empty-state">
            <div style="font-size: ${iconSize}; margin-bottom: 20px;">${escapeHtml(icon)}</div>
            <div style="font-size: var(--ui-type-size-dialog); margin-bottom: 8px;">${escapeHtml(title)}</div>
            <div style="font-size: var(--ui-type-size-body);">${escapeHtml(hint)}</div>
        </div>
    `;
}

function toolsRenderToolCard({ tool, category, index, cardColor, actionsHtml, cardClass = '', clickAttrs = '', dragAttrs = '', actionsStyle = '', tooltip = '' }) {
    const safeCategory = escapeIconAttr(category);
    const safeDescription = escapeIconAttr(tool?.description || '');
    const tooltipAttr = (tooltip || safeDescription) ? ` title="${tooltip ? escapeIconAttr(tooltip) : safeDescription}"` : '';
    return `
        <div class="tool-card ui-card${cardClass ? ` ${cardClass}` : ''}" data-tool-category="${safeCategory}" data-tool-index="${index}" draggable="true"${clickAttrs ? ` ${clickAttrs}` : ''} style="--card-accent: ${cardColor};"${tooltipAttr}${dragAttrs ? ` ${dragAttrs}` : ''}>

            <div class="tool-icon">
                ${toolsRenderToolIcon(tool?.icon, tool)}
            </div>

            <div class="tool-info">
                <div class="tool-title">
                    ${escapeHtml(tool?.title || '')}
                </div>
            </div>

            <div class="tool-actions"${actionsStyle ? ` style="${actionsStyle}"` : ''}>
                ${actionsHtml}
            </div>
        </div>
    `;
}

// 工具图标：支持 [img:url] / [favicon:url] / http(s) 图片 / Emoji，
// 品牌工具优先用浏览器/产品图标并带回退。全站唯一定义在此。
function toolsRenderToolIcon(icon, tool = null) {
    const browserIcon = tool?.title ? PRODUCT_BROWSER_ICON_CANDIDATES[tool.title] : '';
    const fallbackIcon = tool?.title ? PRODUCT_TOOL_ICON_OVERRIDES[tool.title] : '';
    if (browserIcon && fallbackIcon) {
        return renderIconImageWithFallback(browserIcon, fallbackIcon);
    }

    icon = getDisplayToolIcon(icon, tool);
    if (!icon) return '🌐';

    if (icon.startsWith('[img:') || icon.startsWith('[favicon:') || isImageIconUrl(icon)) {
        return renderIconImage(icon);
    }
    return escapeHtml(icon);
}

// data-tools-action 委托分发：open-tool 保留默认行为（不 preventDefault，
// 与既有行为一致），其余动作阻断冒泡后交给 handlers[action]。
function toolsBindGridActions(grid, handlers) {
    if (grid.dataset.toolsActionsBound === 'true') return;
    grid.dataset.toolsActionsBound = 'true';
    grid.addEventListener('click', event => {
        const actionEl = event.target.closest('[data-tools-action]');
        if (!actionEl || !grid.contains(actionEl)) return;

        const action = actionEl.dataset.toolsAction;
        if (action === 'open-tool') {
            handlers['open-tool'](actionEl, event);
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        if (handlers[action]) handlers[action](actionEl, event);
    });
}
