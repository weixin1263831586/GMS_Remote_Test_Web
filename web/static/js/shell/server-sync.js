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
    toolsBindGridActions(grid, {
        'open-tool': el => openToolLink(el.dataset.url || ''),
        'edit-category': (el, event) => editCategory(el.dataset.category, event),
        'add-tool': el => addNewToolToCategory(el.dataset.category),
        'delete-category': el => deleteCategory(el.dataset.category),
        'edit-tool': el => { const index = Number.parseInt(el.dataset.index || '', 10); if (Number.isInteger(index)) editTool(el.dataset.category, index); },
        'delete-tool': el => { const index = Number.parseInt(el.dataset.index || '', 10); if (Number.isInteger(index)) deleteTool(el.dataset.category, index); },
    });
}

function renderAllCategories(grid) {
    const categories = Object.keys(categorizedTools);

    if (categories.length === 0) {
        grid.innerHTML = toolsRenderEmptyGrid({
            icon: '📁', title: '还没有工具', hint: '点击下方按钮添加第一个工具',
        });
        return;
    }

    // 允许显示空分类，不跳过
    grid.innerHTML = categories.map((category, catIndex) => toolsRenderCategorySection({
        category,
        catIndex,
        icon: (DEFAULT_CATEGORIES[category] || { icon: '📁' }).icon,
        count: categorizedTools[category].length,
        cardsHtml: categorizedTools[category]
            .map((tool, index) => renderToolCard(tool, category, index, toolsCategoryColor(catIndex)))
            .join('\n'),
    })).join('');

    // 初始化拖拽事件
    setTimeout(() => initDragAndDrop(), 100);
}

function renderSingleCategory(grid, category) {
    const tools = categorizedTools[category] || [];
    const categoryInfo = DEFAULT_CATEGORIES[category] || { icon: '📁', color: '#8e8e93' };

    if (tools.length === 0) {
        grid.innerHTML = toolsRenderEmptyGrid({
            icon: categoryInfo.icon, title: `"${category}"分类下还没有工具`,
            hint: '点击右侧按钮添加工具到该分类',
        });
        return;
    }

    grid.innerHTML = toolsRenderCategorySection({
        category,
        catIndex: 0,
        icon: categoryInfo.icon,
        count: tools.length,
        cardsHtml: tools
            .map((tool, index) => renderToolCard(tool, category, index, toolsCategoryColor(0)))
            .join('\n'),
    });

    // 初始化拖拽事件
    setTimeout(() => initDragAndDrop(), 100);
}

function renderToolCard(tool, category, index, cardColor) {
    return toolsRenderToolCard({
        tool,
        category,
        index,
        cardColor,
        clickAttrs: `data-tools-action="open-tool" data-url="${escapeIconAttr(tool?.url || '')}"`,
        dragAttrs: 'data-dragstart="handleToolDragStart" data-r0="event"'
            + ' data-dragend="handleToolDragEnd" data-r1="event"'
            + ' data-dragover="handleToolDragOver" data-r2="event"'
            + ' data-drop="handleToolDrop" data-r3="event"',
        actionsHtml: `
                <button type="button" class="btn-act act-edit btn-edit" aria-label="编辑工具" data-tools-action="edit-tool" data-category="${escapeIconAttr(category)}" data-index="${index}">
                    ✏️
                </button>
                <button type="button" class="btn-act act-danger btn-delete" aria-label="删除工具" data-tools-action="delete-tool" data-category="${escapeIconAttr(category)}" data-index="${index}">
                    🗑️
                </button>`,
    });
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
