// Shell 模块：服务器同步（从 shell.html 内联脚本尾部提取）。
// ==================== 服务器同步功能 ====================
let syncInProgress = false;

async function syncToolsToServer() {
    if (syncInProgress) return;

    syncInProgress = true;
    try {
        const response = await fetch('/api/websites/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                tools: categorizedTools,
                timestamp: new Date().toISOString()
            })
        });

        const result = await response.json();
        if (result.success) {
            debugLog('[ToolsSync] Data synced to server successfully');
        } else {
            console.error('[ToolsSync] Failed to sync data to server:', result.error);
        }
    } catch (error) {
        console.error('[ToolsSync] Error syncing data to server:', error);
    } finally {
        syncInProgress = false;
    }
}

async function loadToolsFromServer() {
    try {
        const response = await fetch('/api/websites/load');
        const result = await response.json();

        if (result.success && result.tools) {
            debugLog('[ToolsSync] Data loaded from server:', result.last_updated);

            if (result.tools && Object.keys(result.tools).length > 0) {
                categorizedTools = filterWebsiteOnlyTools(result.tools);
                localStorage.setItem(CATEGORIES_STORAGE_KEY, JSON.stringify(categorizedTools));
                renderToolsGrid();
            }
        }
    } catch (error) {
        console.error('[ToolsSync] Error loading data from server:', error);
    }
}

function filterWebsiteOnlyTools(toolsByCategory) {
    const filtered = {};
    Object.entries(toolsByCategory || {}).forEach(([category, tools]) => {
        const list = Array.isArray(tools) ? tools : [];
        const websiteTools = list.filter(tool => tool && tool.url);
        if (websiteTools.length > 0) {
            filtered[category] = websiteTools;
        }
    });
    return filtered;
}


function switchCategory(category) {
    currentCategory = category;
    renderToolsGrid();
}

function renderToolsGrid() {
    const grid = document.getElementById('tools-grid');
    if (!grid) return;
    bindToolsGridActions(grid);

    // 如果选择"全部"，按分类显示所有工具
    if (currentCategory === 'all') {
        renderAllCategories(grid);
    } else {
        // 显示特定分类的工具
        renderSingleCategory(grid, currentCategory);
    }
}

function bindToolsGridActions(grid) {
    if (grid.dataset.toolsActionsBound === 'true') return;
    grid.dataset.toolsActionsBound = 'true';
    grid.addEventListener('click', event => {
        const actionEl = event.target.closest('[data-tools-action]');
        if (!actionEl || !grid.contains(actionEl)) return;

        const action = actionEl.dataset.toolsAction;
        const category = actionEl.dataset.category || '';
        const index = Number.parseInt(actionEl.dataset.index || '', 10);

        if (action === 'open-tool') {
            openToolLink(actionEl.dataset.url || '');
            return;
        }

        event.preventDefault();
        event.stopPropagation();
        if (action === 'edit-category') editCategory(category, event);
        if (action === 'add-tool') addNewToolToCategory(category);
        if (action === 'delete-category') deleteCategory(category);
        if (action === 'edit-tool' && Number.isInteger(index)) editTool(category, index);
        if (action === 'delete-tool' && Number.isInteger(index)) deleteTool(category, index);
    });
}

function renderAllCategories(grid) {
    const categories = Object.keys(categorizedTools);

    if (categories.length === 0) {
        grid.innerHTML = `
            <div style="text-align: center; padding: 60px 20px; color: var(--text-muted);">
                <div style="font-size: 64px; margin-bottom: 20px;">📁</div>
                <div style="font-size: 16px; margin-bottom: 8px;">还没有工具</div>
                <div style="font-size: 13px;">点击下方按钮添加第一个工具</div>
            </div>
        `;
        return;
    }

    let html = '';
    categories.forEach((category, catIndex) => {
        const tools = categorizedTools[category];
        // 允许显示空分类，不跳过

        const categoryInfo = DEFAULT_CATEGORIES[category] || { icon: '📁', color: '#8e8e93' };

        html += `
            <div class="category-section" data-category="${escapeIconAttr(category)}" data-category-index="${catIndex}" draggable="true" style="margin-bottom: 4px;">
							<div class="category-header" style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border-color); cursor: move;">
                    <div style="display: flex; align-items: center;">
                        <span style="font-size: 16px; margin-right: 6px; cursor: grab;">${escapeHtml(categoryInfo.icon)}</span>
                        <button type="button" data-tools-action="edit-category" data-category="${escapeIconAttr(category)}" style="padding: 0; border: 0; background: transparent; font-size: 16px; font-weight: 600; color: var(--text-primary); cursor: pointer;"><span class="category-name">${escapeHtml(category)}</span></button>
                        <span style="margin-left: 10px; padding: 2px 8px; background: var(--light-bg); border-radius: 10px; font-size: 11px; color: var(--text-muted);">${tools.length}</span>
                    </div>
                    <div style="display: flex; gap: 6px;">
                        <button type="button" data-tools-action="add-tool" data-category="${escapeIconAttr(category)}" style="padding: 6px 16px; background: ${categoryInfo.color}; color: white; border: none; border-radius: 5px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.2s;"
                            onmouseover="this.style.opacity='0.85';"
                            onmouseout="this.style.opacity='1';">
                            添加工具
                        </button>
                        <button type="button" data-tools-action="delete-category" data-category="${escapeIconAttr(category)}" style="padding: 6px 12px; background: var(--danger-color); color: white; border: none; border-radius: 5px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.2s;"
                            onmouseover="this.style.opacity='0.85';"
                            onmouseout="this.style.opacity='1';"
                            title="删除分类">
                            🗑️
                        </button>
                    </div>
                </div>
                <div class="tools-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 16px;">
        `;

        tools.forEach((tool, index) => {
            const cardColor = categoryInfo.color;
            html += renderToolCard(tool, category, index, cardColor);
        });

        html += `
                </div>
            </div>
        `;
    });

    grid.innerHTML = html;

    // 初始化拖拽事件
    setTimeout(() => initDragAndDrop(), 100);
}

function renderSingleCategory(grid, category) {
    const tools = categorizedTools[category] || [];
    const categoryInfo = DEFAULT_CATEGORIES[category] || { icon: '📁', color: '#8e8e93' };

    if (tools.length === 0) {
        grid.innerHTML = `
            <div style="text-align: center; padding: 40px 20px; color: var(--text-muted);">
                <div style="font-size: 48px; margin-bottom: 16px;">${escapeHtml(categoryInfo.icon)}</div>
                <div style="font-size: 14px; margin-bottom: 6px;">"${escapeHtml(category)}"分类下还没有工具</div>
                <div style="font-size: 12px;">点击右侧按钮添加工具到该分类</div>
            </div>
        `;
        return;
    }

    let html = `
        <div class="category-section" data-category="${escapeIconAttr(category)}" data-category-index="0" draggable="true">
            <div class="category-header" style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border-color); cursor: move;">
                <div style="display: flex; align-items: center;">
                    <span style="font-size: 16px; margin-right: 6px; cursor: grab;">${escapeHtml(categoryInfo.icon)}</span>
                    <button type="button" data-tools-action="edit-category" data-category="${escapeIconAttr(category)}" style="padding: 0; border: 0; background: transparent; font-size: 16px; font-weight: 600; color: var(--text-primary); cursor: pointer;"><span class="category-name">${escapeHtml(category)}</span></button>
                    <span style="margin-left: 10px; padding: 2px 8px; background: var(--light-bg); border-radius: 10px; font-size: 11px; color: var(--text-muted);">${tools.length}</span>
                </div>
                <div style="display: flex; gap: 6px;">
                    <button type="button" data-tools-action="add-tool" data-category="${escapeIconAttr(category)}" style="padding: 6px 16px; background: ${categoryInfo.color}; color: white; border: none; border-radius: 5px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.2s;"
                        onmouseover="this.style.opacity='0.85';"
                        onmouseout="this.style.opacity='1';">
                        添加工具
                    </button>
                    <button type="button" data-tools-action="delete-category" data-category="${escapeIconAttr(category)}" style="padding: 6px 12px; background: var(--danger-color); color: white; border: none; border-radius: 5px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.2s;"
                        onmouseover="this.style.opacity='0.85';"
                        onmouseout="this.style.opacity='1';"
                        title="删除分类">
                        🗑️
                    </button>
                </div>
            </div>
            <div class="tools-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 16px;">
    `;

    tools.forEach((tool, index) => {
        const cardColor = categoryInfo.color;
        html += renderToolCard(tool, category, index, cardColor);
    });

    html += `
            </div>
        </div>
    `;

    grid.innerHTML = html;

    // 初始化拖拽事件
    setTimeout(() => initDragAndDrop(), 100);
}

function renderToolCard(tool, category, index, cardColor) {
    const safeCategory = escapeIconAttr(category);
    const safeUrl = escapeIconAttr(tool?.url || '');
    return `
        <div class="tool-card" data-tool-category="${safeCategory}" data-tool-index="${index}" data-tools-action="open-tool" data-url="${safeUrl}" draggable="true"
             style="background: var(--card-bg); border-radius: 6px; padding: 10px; display: flex; flex-direction: column; align-items: center; gap: 6px; border: 1px solid var(--border-color); border-top: 2px solid ${cardColor}; transition: all 0.2s; cursor: grab; min-width: 100px; max-width: 130px; min-height: 118px;"
             onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='var(--shadow-md)'; this.style.borderColor='${cardColor}';"
             onmouseout="this.style.transform='translateY(0)'; this.style.boxShadow='none'; this.style.borderColor='var(--border-color)';"
             ondragstart="handleToolDragStart(event)"
             ondragend="handleToolDragEnd(event)"
             ondragover="handleToolDragOver(event)"
             ondrop="handleToolDrop(event)">

            <div class="tool-icon" style="width: 40px; height: 40px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; background: var(--light-bg); border-radius: 6px; font-size: 24px; pointer-events: none;">
                ${renderToolIcon(tool.icon, tool)}
            </div>

            <div class="tool-info" style="text-align: center; width: 100%; pointer-events: none;">
                <div class="tool-title" style="font-size: 12px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%;">
                    ${escapeHtml(tool?.title || '')}
                </div>
            </div>

            <div class="tool-actions" style="display: flex; gap: 3px; width: 100%;">
                <button type="button" class="btn-edit" aria-label="编辑工具" data-tools-action="edit-tool" data-category="${safeCategory}" data-index="${index}"
                        style="flex: 1; padding: 2px 3px; background: transparent; color: var(--text-secondary); border: 1px solid var(--border-color); border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;"
                        onmouseover="this.style.background='var(--primary-color)'; this.style.color='white'; this.style.borderColor='var(--primary-color)';"
                        onmouseout="this.style.background='transparent'; this.style.color='var(--text-secondary)'; this.style.borderColor='var(--border-color)';">
                    ✏️
                </button>
                <button type="button" class="btn-delete" aria-label="删除工具" data-tools-action="delete-tool" data-category="${safeCategory}" data-index="${index}"
                        style="flex: 1; padding: 2px 3px; background: transparent; color: var(--text-muted); border: 1px solid var(--border-color); border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;"
                        onmouseover="this.style.background='var(--danger-color)'; this.style.color='white'; this.style.borderColor='var(--danger-color)';"
                        onmouseout="this.style.background='transparent'; this.style.color='var(--text-muted)'; this.style.borderColor='var(--border-color)';">
                    🗑️
                </button>
            </div>
        </div>
    `;
}

function renderToolIcon(icon, tool = null) {
    const browserIcon = tool?.title ? PRODUCT_BROWSER_ICON_CANDIDATES[tool.title] : '';
    const fallbackIcon = tool?.title ? PRODUCT_TOOL_ICON_OVERRIDES[tool.title] : '';
    if (browserIcon && fallbackIcon) {
        return renderIconImageWithFallback(browserIcon, fallbackIcon);
    }

    icon = getDisplayToolIcon(icon, tool);
    if (!icon) return '🌐';

    // 检查是否是图片格式 [img:url]
    if (icon.startsWith('[img:')) {
        return renderIconImage(icon);
    }

    // 检查是否是 http/https 开头的图片链接
    if (isImageIconUrl(icon)) {
        return renderIconImage(icon);
    }

    // 检查是否是 favicon 格式 [favicon:url]
    if (icon.startsWith('[favicon:')) {
        return renderIconImage(icon);
    }

    // 否则返回原始的 Emoji
    return escapeHtml(icon);
}

// 通用工具管理函数（websites 与 tools 共用）。
function _closeModal(modalId, resetFn) {
    ModalManager.close(modalId);
    if (resetFn) resetFn();
}

function _showAddCategoryModal(modalId, fieldIds, resetFn) {
    if (resetFn) resetFn();
    document.getElementById(fieldIds.name).value = '';
    document.getElementById(fieldIds.icon).value = fieldIds.iconDefault || '';
    ModalManager.open(modalId);
}

function _deleteTool(dataObj, category, index, saveFn, renderFn, opts) {
    const tool = dataObj[category][index];
    const msg = opts && opts.showName ? `确定要删除工具"${tool.title}"吗？` : '确定要删除这个工具吗？';
    showConfirmDialog('确认删除', msg).then(confirmed => {
        if (confirmed) {
            dataObj[category].splice(index, 1);
            if (dataObj[category].length === 0) {
                delete dataObj[category];
                if (opts && opts.onCategoryEmpty) opts.onCategoryEmpty(category);
            }
            saveFn();
            renderFn();
            showToast(opts && opts.showName ? `工具"${tool.title}"已删除` : '删除成功', 'success');
        }
    });
}

function _deleteCategory(dataObj, categoryName, extraCleanups, saveFn, renderFn) {
    const tools = dataObj[categoryName] || [];
    let message = `确定要删除分类"${categoryName}"吗？`;
    if (tools.length > 0) {
        message += ` 该分类下有 ${tools.length} 个工具，删除后这些工具也会被删除。`;
    }
    showConfirmDialog('确认删除', message).then(confirmed => {
        if (confirmed) {
            delete dataObj[categoryName];
            if (extraCleanups) extraCleanups.forEach(fn => fn(categoryName));
            saveFn();
            renderFn();
            showToast(`分类"${categoryName}"已删除`, 'success');
        }
    });
}

function openToolLink(url) {
    if (!url) return;
    try {
        const rawUrl = String(url).trim();
        if (!rawUrl || rawUrl.startsWith('//') || rawUrl.includes('\\')) throw new Error('invalid URL');
        const finalUrl = rawUrl.startsWith('/') ? rawUrl : (/^https?:\/\//i.test(rawUrl) ? rawUrl : `https://${rawUrl}`);
        const parsed = new URL(finalUrl, window.location.origin);
        if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('unsupported protocol');
        window.open(parsed.href, '_blank', 'noopener,noreferrer');
    } catch (_error) {
        showToast('无法打开：网址格式或协议不受支持', 'error');
    }
}
