// Shell 模块：常用工具/常用网址 卡片网格共用渲染器（两条渲染路径的唯一定义）。
// 网格列数/间距在 common-layout.css 的 .tools-grid；事件走 data-tools-action
// 委托（CSP 下无内联 onclick）；卡片拖拽沿用 act-bridge 的声明式属性。

function toolsRenderCategoryHeader({ category, icon, count, addButtonBackground }) {
    const safeCategory = escapeIconAttr(category);
    return `
            <div class="category-header" style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 5px; padding-bottom: 4px; border-bottom: 1px solid var(--border-color); cursor: move;">
                <div style="display: flex; align-items: center;">
                    <span style="font-size: 16px; margin-right: 6px; cursor: grab;">${escapeHtml(icon)}</span>
                    <button type="button" data-tools-action="edit-category" data-category="${safeCategory}" style="padding: 0; border: 0; background: transparent; font-size: 16px; font-weight: 600; color: var(--text-primary); cursor: pointer;"><span class="category-name">${escapeHtml(category)}</span></button>
                    <span style="margin-left: 10px; padding: 2px 8px; background: var(--light-bg); border-radius: 10px; font-size: 11px; color: var(--text-muted);">${count}</span>
                </div>
                <div style="display: flex; gap: 6px;">
                    <button type="button" data-tools-action="add-tool" data-category="${safeCategory}" style="padding: 6px 16px; background: ${addButtonBackground}; color: white; border: none; border-radius: 5px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.2s;"
                        class="hover-dim">
                        添加工具
                    </button>
                    <button type="button" data-tools-action="delete-category" data-category="${safeCategory}" style="padding: 6px 12px; background: var(--danger-color); color: white; border: none; border-radius: 5px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.2s;"
                        class="hover-dim"
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
        <div style="text-align: center; padding: ${padding}; color: var(--text-muted);">
            <div style="font-size: ${iconSize}; margin-bottom: 20px;">${escapeHtml(icon)}</div>
            <div style="font-size: 16px; margin-bottom: 8px;">${escapeHtml(title)}</div>
            <div style="font-size: 13px;">${escapeHtml(hint)}</div>
        </div>
    `;
}

function toolsRenderToolCard({ tool, category, index, cardColor, actionsHtml, cardClass = '', clickAttrs = '', dragAttrs = '', actionsStyle = '', tooltip = '' }) {
    const safeCategory = escapeIconAttr(category);
    const safeDescription = escapeIconAttr(tool?.description || '');
    const tooltipAttr = (tooltip || safeDescription) ? ` title="${tooltip ? escapeIconAttr(tooltip) : safeDescription}"` : '';
    return `
        <div class="tool-card${cardClass ? ` ${cardClass}` : ''}" data-tool-category="${safeCategory}" data-tool-index="${index}" draggable="true"${clickAttrs ? ` ${clickAttrs}` : ''} style="--card-accent: ${cardColor}; background: var(--card-bg); border-radius: 6px; padding: 10px; display: flex; flex-direction: column; align-items: center; gap: 6px; border: 1px solid var(--border-color); border-top: 2px solid ${cardColor}; transition: all 0.2s; cursor: grab; min-width: 100px; max-width: 130px; min-height: 118px;"${tooltipAttr}${dragAttrs ? ` ${dragAttrs}` : ''}>

            <div class="tool-icon" style="width: 40px; height: 40px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; background: var(--light-bg); border-radius: 6px; font-size: 24px; pointer-events: none;">
                ${toolsRenderToolIcon(tool?.icon, tool)}
            </div>

            <div class="tool-info" style="text-align: center; width: 100%; pointer-events: none;">
                <div class="tool-title" style="font-size: 12px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%; margin-bottom: 0;">
                    ${escapeHtml(tool?.title || '')}
                </div>
            </div>

            <div class="tool-actions" style="display: flex; gap: 3px; width: 100%;${actionsStyle ? ` ${actionsStyle}` : ''}">
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
