// Shell 模块：常用工具（从 shell.html 内联脚本尾部提取）。
// ==================== 常用工具 (Utility Tools) ====================
const UT_STORAGE_KEY = 'gms_utility_tools_categories';
const UT_CATEGORY_META_KEY = 'gms_utility_tools_category_meta';
const UT_DEFAULT_CATEGORIES = {
    'Gerrit工具': { icon: '📦', color: '#34a853' },
    '设备工具': { icon: '📱', color: '#4285f4' },
    '烧写工具': { icon: '🔥', color: '#ea4335' },
    '测试工具': { icon: '✅', color: '#7c3aed' },
    '报告工具': { icon: '📅', color: '#0ea5e9' },
    '其他工具': { icon: '🔧', color: '#8e8e93' },
};
const UT_BUILTIN_TOOLS = {
    '报告工具': [
        {
            icon: '📅',
            title: '周报总结',
            description: '基于 Redmine/Gerrit 个人看板数据生成上周周报（可自定义起止日期）',
            action: 'openWeeklyReport',
            builtin_id: 'builtin-weekly-report'
        }
    ],
    '烧写工具': [
        {
            icon: '📦',
            title: '共享固件',
            description: '登记编译服务器上的固件路径，其他客户端可直接从远端主机流式下载',
            action: 'shareFirmware',
            builtin_id: 'builtin-share-firmware'
        }
    ],
    '测试工具': [
        {
            icon: '📦',
            title: 'Mainline包豁免项',
            description: '查看Mainline包豁免项，支持按模块、用例和豁免ID搜索',
            url: '/mainline-known-issues',
            action: 'mainline-sync',
            builtin_id: 'mainline-known-issues'
        },
        {
            icon: '✅',
            title: '测试套件更新',
            description: '扫描Android 14+ CTS/VTS/GTS和GMS包更新，默认全量扫描',
            url: '/gms-update-monitor?tab=artifacts',
            action: 'gms-update-sync',
            builtin_id: 'gms-update-suite-monitor',
            sync_sources: ['cts_downloads', 'vts_downloads', 'gts_downloads', 'gms_downloads'],
            sync_title: '测试套件更新'
        },
        {
            icon: '📦',
            title: 'GMS包更新',
            description: '扫描并查看Android 14+ GMS包更新表格，默认全量扫描',
            url: '/gms-update-monitor?tab=packages',
            action: 'gms-update-sync',
            builtin_id: 'gms-package-monitor',
            sync_sources: ['gms_downloads'],
            sync_title: 'GMS包更新'
        },
        {
            icon: '🚆',
            title: 'Mainline包更新',
            description: '扫描并查看Mainline PRELOAD包更新表格，默认抓取近12个月',
            url: '/gms-update-monitor?tab=mainline',
            action: 'gms-update-sync',
            builtin_id: 'gms-mainline-monitor',
            sync_sources: ['mainline_preload'],
            sync_title: 'Mainline包更新'
        },
        {
            icon: '📋',
            title: 'GMS要求',
            description: '扫描GMS认证要求章节和表格更新，默认展示顶层主章节',
            url: '/gms-update-monitor?tab=requirements',
            action: 'gms-update-sync',
            builtin_id: 'gms-requirements-monitor',
            sync_sources: ['gms_requirements'],
            sync_title: 'GMS要求'
        }
    ]
};
let ut_categorizedTools = {};
let ut_categoryMeta = {};
let ut_editingCategory = null;
let ut_editingTool = null;

// ==================== 常用工具 UI 逻辑 ====================
// 原内联于 weekly-report.js 尾部的常用工具网格/编辑/同步逻辑，
// 与周报功能无关，移回本模块（file-size budget 拆分）。
// 依赖：本文件头部的 UT_* 常量与状态，以及全局 showToast/state。
function ut_getCategoryInfo(category) {
    return ut_categoryMeta[category] || UT_DEFAULT_CATEGORIES[category] || { icon: '📁', color: '#8e8e93' };
}

// Stable-ID 一次性迁移：早期版本把下载路径存在 file_path 字段；后端
// _resolve_tool_id 兼容历史文件名/真实路径，这里补写 tool_id 让旧卡片
// 恢复"点击即下载"（例如 Gerrit Patch 工具卡），避免
// "该工具未配置下载文件" 的误报。仅写 localStorage，无网络请求。
function ut_migrateLegacyFilePaths(categories) {
    let changed = false;
    Object.values(categories || {}).forEach((tools) => {
        (tools || []).forEach((tool) => {
            if (tool && !tool.tool_id && tool.file_path) {
                tool.tool_id = tool.file_path;
                changed = true;
            }
        });
    });
    return changed;
}

function ut_loadToolsList() {
    try {
        ut_categoryMeta = JSON.parse(localStorage.getItem(UT_CATEGORY_META_KEY) || '{}') || {};
    } catch (e) {
        console.error('加载常用工具分类信息失败:', e);
        ut_categoryMeta = {};
    }

    const stored = localStorage.getItem(UT_STORAGE_KEY);
    if (stored) {
        try {
            ut_categorizedTools = JSON.parse(stored);
            if (ut_migrateLegacyFilePaths(ut_categorizedTools)) ut_saveCategories();
            ut_ensureBuiltInTools();
            ut_renderToolsGrid();
            return;
        } catch (e) { console.error('加载常用工具数据失败:', e); }
    }
    // 默认数据
    ut_categorizedTools = {
        '常用工具': [
            {
                icon: '📦',
                title: '共享固件',
                description: '登记编译服务器上的固件路径，其他客户端可直接从远端主机流式下载',
                action: 'shareFirmware',
                builtin_id: 'builtin-share-firmware'
            },
            {
                icon: '📦',
                title: 'Gerrit Patch导出与导入',
                description: '从Gerrit导出patch并应用到本地Android源码，支持批量导出Change的patch文件到指定目录，也支持将patch应用到本地代码',
                tool_id: 'gerrit-patch'
            }
        ],
        '测试工具': []
    };
    ut_ensureBuiltInTools();
    ut_saveCategories();
    ut_renderToolsGrid();
}

const UT_HIDDEN_BUILTINS_KEY = 'gms_utility_tools_hidden_builtins';

function ut_getHiddenBuiltins() {
    try {
        return new Set(JSON.parse(localStorage.getItem(UT_HIDDEN_BUILTINS_KEY) || '[]'));
    } catch (e) {
        return new Set();
    }
}

function ut_hideBuiltin(builtinId) {
    const hidden = ut_getHiddenBuiltins();
    hidden.add(builtinId);
    localStorage.setItem(UT_HIDDEN_BUILTINS_KEY, JSON.stringify([...hidden]));
}

function ut_ensureBuiltInTools() {
    let changed = false;
    const hidden = ut_getHiddenBuiltins();
    Object.entries(UT_BUILTIN_TOOLS).forEach(([category, tools]) => {
        if (!ut_categorizedTools[category]) {
            ut_categorizedTools[category] = [];
            changed = true;
        }
        tools.forEach((builtinTool) => {
            // Skip builtins the user has explicitly hidden/deleted.
            if (hidden.has(builtinTool.builtin_id)) return;
            const existingIndex = ut_categorizedTools[category].findIndex(tool => tool.builtin_id === builtinTool.builtin_id);
            if (existingIndex >= 0) {
                ut_categorizedTools[category][existingIndex] = {
                    ...ut_categorizedTools[category][existingIndex],
                    ...builtinTool
                };
            } else {
                ut_categorizedTools[category].unshift(builtinTool);
                changed = true;
            }
        });
    });
    if (changed) ut_saveCategories();
}

function ut_saveCategories() {
    localStorage.setItem(UT_STORAGE_KEY, JSON.stringify(ut_categorizedTools));
    localStorage.setItem(UT_CATEGORY_META_KEY, JSON.stringify(ut_categoryMeta));
}

function ut_renderToolsGrid() {
    const grid = document.getElementById('ut-grid');
    if (!grid) return;
    ut_renderAllCategories(grid);
    ut_restoreSyncButtonState();
}

async function ut_restoreSyncButtonState() {
    try {
        const checks = [
            {
                action: 'mainline-sync',
                url: '/api/mainline-known-issues/sync/status',
                parse: data => data.status || {},
                poll: (btn) => { ut_mainlineSyncButton = btn; ut_pollMainlineKnownIssuesSync(btn, '触发扫描'); }
            },
            {
                action: 'gms-update-sync',
                url: '/api/gms-update-monitor/sync/status',
                parse: data => (data.data && data.data.status) || {},
                poll: (btn, tool) => {
                    ut_gmsUpdateSyncButton = btn;
                    ut_pollGmsUpdateMonitorSync(
                        btn,
                        '触发扫描',
                        (tool && tool.sync_title) || 'GMS/CTS更新',
                        (tool && tool.sync_sources) || []
                    );
                }
            }
        ];
        for (const check of checks) {
            const response = await fetch(check.url);
            const result = await response.json();
            const status = check.parse(result);
            if (!status.running) continue;
            const runningSources = Array.isArray(status.source) ? status.source : [];
            const syncTool = ut_categorizedTools && Object.values(ut_categorizedTools)
                .flat().find(t => {
                    if (t.action !== check.action) return false;
                    if (check.action !== 'gms-update-sync' || runningSources.length === 0) return true;
                    const toolSources = Array.isArray(t.sync_sources) ? t.sync_sources : [];
                    return toolSources.length === runningSources.length &&
                        runningSources.every(source => toolSources.includes(source));
                });
            if (!syncTool) continue;
            const cards = document.querySelectorAll('#ut-grid .tool-card, #ut-grid [style*="cursor: pointer"]');
            for (const card of cards) {
                const titleEl = card.querySelector('div[style*="font-weight: 600"]');
                if (titleEl && titleEl.textContent === syncTool.title) {
                    const btn = card.querySelector('button');
                    if (btn) {
                        btn.disabled = true;
                        btn.textContent = '扫描中';
                        check.poll(btn, syncTool);
                    }
                    break;
                }
            }
        }
    } catch (e) {
        // 静默失败，不影响页面加载
    }
}

function ut_renderAllCategories(grid) {
    grid.innerHTML = '';
    const categories = Object.keys(ut_categorizedTools);
    if (categories.length === 0) {
        grid.innerHTML = `
            <div style="text-align: center; padding: 60px 20px; color: var(--text-muted);">
                <div style="font-size: 64px; margin-bottom: 20px;">🧰</div>
                <div style="font-size: 16px; margin-bottom: 8px;">还没有工具</div>
                <div style="font-size: 13px;">点击下方按钮添加分类和工具</div>
            </div>`;
        return;
    }

    categories.forEach((category, catIndex) => {
        const tools = ut_categorizedTools[category];
        const catInfo = ut_getCategoryInfo(category);
        const section = document.createElement('div');
        section.className = 'category-section';
        section.dataset.category = category;
        section.dataset.categoryIndex = catIndex;
        section.draggable = true;
        section.style.marginBottom = '4px';
        section.addEventListener('dragstart', handleCategoryDragStart);
        section.addEventListener('dragend', handleCategoryDragEnd);
        section.addEventListener('dragover', handleCategoryDragOver);
        section.addEventListener('drop', ut_handleCategoryDrop);

        const header = document.createElement('div');
        header.style.cssText = 'display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border-color); cursor: move;';

        const titleWrap = document.createElement('div');
        titleWrap.style.cssText = 'display: flex; align-items: center;';

        const icon = document.createElement('span');
        icon.style.cssText = 'font-size: 16px; margin-right: 6px; cursor: grab;';
        icon.textContent = catInfo.icon || '📁';
        titleWrap.appendChild(icon);

        const title = document.createElement('span');
        title.style.cssText = 'font-size: 16px; font-weight: 600; color: var(--text-primary); cursor: pointer;';
        title.textContent = category;
        title.addEventListener('click', () => ut_editCategory(category));
        titleWrap.appendChild(title);

        const count = document.createElement('span');
        count.style.cssText = 'margin-left: 10px; padding: 2px 8px; background: var(--light-bg); border-radius: 10px; font-size: 11px; color: var(--text-muted);';
        count.textContent = String(tools.length);
        titleWrap.appendChild(count);

        const actions = document.createElement('div');
        actions.style.cssText = 'display: flex; gap: 6px;';

        const addBtn = document.createElement('button');
        addBtn.textContent = '添加工具';
        addBtn.style.cssText = `padding: 6px 16px; background: ${catInfo.color}; color: white; border: none; border-radius: 5px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.2s;`;
        addBtn.addEventListener('mouseenter', () => { addBtn.style.opacity = '0.85'; });
        addBtn.addEventListener('mouseleave', () => { addBtn.style.opacity = '1'; });
        addBtn.addEventListener('click', () => ut_addNewToolToCategory(category));
        actions.appendChild(addBtn);

        const deleteBtn = document.createElement('button');
        deleteBtn.textContent = '🗑️';
        deleteBtn.title = '删除分类';
        deleteBtn.style.cssText = 'padding: 6px 12px; background: var(--danger-color); color: white; border: none; border-radius: 5px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.2s;';
        deleteBtn.addEventListener('mouseenter', () => { deleteBtn.style.opacity = '0.85'; });
        deleteBtn.addEventListener('mouseleave', () => { deleteBtn.style.opacity = '1'; });
        deleteBtn.addEventListener('click', () => ut_deleteCategory(category));
        actions.appendChild(deleteBtn);

        header.appendChild(titleWrap);
        header.appendChild(actions);
        section.appendChild(header);

        const cards = document.createElement('div');
        cards.style.cssText = 'display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 16px;';
        tools.forEach((tool, index) => {
            cards.appendChild(ut_createToolCard(tool, category, index, catInfo.color));
        });
        section.appendChild(cards);
        grid.appendChild(section);
    });
}

function ut_createToolCard(tool, category, index, cardColor) {
    const card = document.createElement('div');
    card.className = 'tool-card';
    card.title = tool.description || '';
    card.dataset.toolCategory = category;
    card.dataset.toolIndex = index;
    card.draggable = true;
    card.style.cssText = `background: var(--card-bg); border-radius: 6px; padding: 10px; display: flex; flex-direction: column; align-items: center; gap: 6px; border: 1px solid var(--border-color); border-top: 2px solid ${cardColor}; transition: all 0.2s; cursor: grab; min-width: 100px; max-width: 130px; min-height: 118px;`;
    if (tool.action === 'openWeeklyReport') {
        card.addEventListener('click', () => openWeeklyReport());
    } else if (tool.action === 'shareFirmware') {
        card.addEventListener('click', () => shareFirmware());
    } else if (tool.url) {
        card.addEventListener('click', () => openToolLink(tool.url));
    } else if (tool.tool_id && tool.action !== 'mainline-sync') {
        card.addEventListener('click', () => ut_downloadTool(tool.tool_id, tool.title));
    }
    card.addEventListener('mouseenter', () => {
        card.style.transform = 'translateY(-2px)';
        card.style.boxShadow = 'var(--shadow-md)';
        card.style.borderColor = cardColor;
    });
    card.addEventListener('mouseleave', () => {
        card.style.transform = 'translateY(0)';
        card.style.boxShadow = 'none';
        card.style.borderColor = 'var(--border-color)';
    });
    card.addEventListener('dragstart', handleToolDragStart);
    card.addEventListener('dragend', handleToolDragEnd);
    card.addEventListener('dragover', handleToolDragOver);
    card.addEventListener('drop', ut_handleToolDrop);

    const icon = document.createElement('div');
    icon.style.cssText = 'width: 40px; height: 40px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; background: var(--light-bg); border-radius: 6px; font-size: 24px; pointer-events: none;';
    icon.textContent = tool.icon || '🔧';
    card.appendChild(icon);

    const textWrap = document.createElement('div');
    textWrap.style.cssText = 'text-align: center; width: 100%; pointer-events: none;';
    const title = document.createElement('div');
    title.style.cssText = 'font-size: 12px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%;';
    title.textContent = tool.title || '';
    textWrap.appendChild(title);
    card.appendChild(textWrap);

    const actions = document.createElement('div');
    actions.style.cssText = 'display: flex; gap: 3px; width: 100%; margin-top: auto;';

    const isSyncTool = tool.action === 'mainline-sync' || tool.action === 'gms-update-sync';
    const isShareFirmwareTool = tool.action === 'shareFirmware';
    // sync 工具显示触发按钮
    if (isSyncTool) {
        const downloadBtn = document.createElement('button');
        downloadBtn.textContent = '触发扫描';
        downloadBtn.title = tool.action === 'gms-update-sync' ? '扫描 GMS/CTS 更新' : '扫描 Mainline release notes';
        downloadBtn.style.cssText = 'flex: 2; padding: 2px 3px; background: var(--success-color); color: white; border: none; border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;';
        downloadBtn.addEventListener('mouseenter', () => { downloadBtn.style.opacity = '0.8'; });
        downloadBtn.addEventListener('mouseleave', () => { downloadBtn.style.opacity = '1'; });
        downloadBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            if (tool.action === 'gms-update-sync') {
                ut_startGmsUpdateMonitorSync(downloadBtn, tool);
            } else {
                ut_startMainlineKnownIssuesSync(downloadBtn);
            }
        });
        actions.appendChild(downloadBtn);
    }
    // 有 tool_id 且无 url 的下载型工具不显示下载按钮（点击卡片即下载）
    // 只有有 url 的非内置工具才显示下载按钮
    const isDownloadOnlyTool = !isSyncTool && tool.tool_id && !tool.url;
    if (!tool.builtin_id && !isSyncTool && !isShareFirmwareTool && !isDownloadOnlyTool) {
        const downloadBtn = document.createElement('button');
        downloadBtn.textContent = '下载';
        downloadBtn.title = '下载文件';
        downloadBtn.style.cssText = 'flex: 2; padding: 2px 3px; background: var(--success-color); color: white; border: none; border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;';
        downloadBtn.addEventListener('mouseenter', () => { downloadBtn.style.opacity = '0.8'; });
        downloadBtn.addEventListener('mouseleave', () => { downloadBtn.style.opacity = '1'; });
        downloadBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            ut_downloadTool(tool.tool_id, tool.title);
        });
        actions.appendChild(downloadBtn);
    }

    if (!tool.builtin_id) {
        const editBtn = document.createElement('button');
        editBtn.textContent = '✏️';
        editBtn.style.cssText = 'flex: 1; padding: 2px 3px; background: transparent; color: var(--text-secondary); border: 1px solid var(--border-color); border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;';
        editBtn.addEventListener('mouseenter', () => {
            editBtn.style.background = 'var(--primary-color)';
            editBtn.style.color = 'white';
            editBtn.style.borderColor = 'var(--primary-color)';
        });
        editBtn.addEventListener('mouseleave', () => {
            editBtn.style.background = 'transparent';
            editBtn.style.color = 'var(--text-secondary)';
            editBtn.style.borderColor = 'var(--border-color)';
        });
        editBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            ut_editTool(category, index);
        });
        actions.appendChild(editBtn);

        const deleteBtn = document.createElement('button');
        deleteBtn.textContent = '🗑️';
        deleteBtn.style.cssText = 'flex: 1; padding: 2px 3px; background: transparent; color: var(--text-muted); border: 1px solid var(--border-color); border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;';
        deleteBtn.addEventListener('mouseenter', () => {
            deleteBtn.style.background = 'var(--danger-color)';
            deleteBtn.style.color = 'white';
            deleteBtn.style.borderColor = 'var(--danger-color)';
        });
        deleteBtn.addEventListener('mouseleave', () => {
            deleteBtn.style.background = 'transparent';
            deleteBtn.style.color = 'var(--text-muted)';
            deleteBtn.style.borderColor = 'var(--border-color)';
        });
        deleteBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            ut_deleteTool(category, index);
        });
        actions.appendChild(deleteBtn);
    }

    // Built-in tools get a hide button so users can remove them.
    if (tool.builtin_id) {
        const hideBtn = document.createElement('button');
        hideBtn.textContent = '🗑️';
        hideBtn.title = '隐藏此内置工具';
        hideBtn.style.cssText = 'flex: 1; padding: 2px 3px; background: transparent; color: var(--text-muted); border: 1px solid var(--border-color); border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;';
        hideBtn.addEventListener('mouseenter', () => {
            hideBtn.style.background = 'var(--danger-color)';
            hideBtn.style.color = 'white';
            hideBtn.style.borderColor = 'var(--danger-color)';
        });
        hideBtn.addEventListener('mouseleave', () => {
            hideBtn.style.background = 'transparent';
            hideBtn.style.color = 'var(--text-muted)';
            hideBtn.style.borderColor = 'var(--border-color)';
        });
        hideBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            ut_hideBuiltin(tool.builtin_id);
            ut_categorizedTools[category].splice(index, 1);
            ut_saveCategories();
            ut_renderToolsGrid();
            showToast(`已隐藏「${tool.title}」`, 'info');
        });
        actions.appendChild(hideBtn);
    }

    card.appendChild(actions);
    return card;
}

// 常用工具拖拽排序。

function ut_handleToolDrop(e) {
    e.preventDefault();
    const card = e.target.closest('.tool-card');
    if (!card || !draggedTool) return;
    const targetCategory = card.dataset.toolCategory;
    const targetIndex = parseInt(card.dataset.toolIndex);
    if (targetCategory === draggedTool.category &&
        targetIndex === draggedTool.index) return;
    moveTool(ut_categorizedTools, draggedTool.category, draggedTool.index, targetCategory, targetIndex, ut_saveCategories);
    ut_renderToolsGrid();
}

function ut_handleCategoryDrop(e) {
    e.preventDefault();
    const section = e.target.closest('.category-section');
    if (!section || draggedCategoryIndex === null) return;
    const targetIndex = parseInt(section.dataset.categoryIndex);
    if (targetIndex === draggedCategoryIndex) return;
    moveCategory(ut_categorizedTools, draggedCategoryIndex, targetIndex, (newData) => { ut_categorizedTools = newData; ut_saveCategories(); });
    ut_renderToolsGrid();
}

let ut_mainlineSyncButton = null;
let ut_gmsUpdateSyncButton = null;

async function ut_startMainlineKnownIssuesSync(button) {
    ut_mainlineSyncButton = button;
    // 检查 db 是否存在，不存在则直接全量扫描
    try {
        const resp = await fetch('/api/mainline-known-issues/sync/status');
        const data = await resp.json();
        if (data.status && data.status.running) {
            showToast('扫描正在进行中，请稍候', 'warning');
            return;
        }
        if (!data.status || !data.status.db_exists) {
            // db 不存在，弹框提示后直接全量扫描
            showConfirmDialog(
                '首次扫描提示',
                '首次使用需要全量扫描，请确保本机浏览器已打开 https://docs.partner.android.com/mainline/release/release-notes 并可正常访问。'
            ).then(confirmed => {
                if (confirmed) {
                    ut_confirmMainlineKnownIssuesSync('full');
                }
            });
            return;
        }
    } catch (e) {
        // 查询失败，继续弹框选择
    }
    // db 存在，弹框选择扫描方式（ModalManager 是 modal 状态唯一真源）
    ModalManager.open('mainline-sync-modal');
}

function ut_closeMainlineSyncModal() {
    ModalManager.close('mainline-sync-modal');
}

async function ut_confirmMainlineKnownIssuesSync(mode) {
    const button = ut_mainlineSyncButton;
    ut_closeMainlineSyncModal();
    if (!button) {
        showToast('未找到扫描按钮，请刷新页面后重试', 'error');
        return;
    }
    const modeText = mode === 'full' ? '全量扫描' : '增量扫描';
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = '扫描中';
    try {
        const result = await apiCall(
            '/api/mainline-known-issues/sync?mode=' + encodeURIComponent(mode),
            'POST'
        );
        if (!result.success) {
            throw new Error(result.error || '启动扫描失败');
        }
        showToast('Mainline包豁免项' + modeText + '已启动', 'info');
        ut_pollMainlineKnownIssuesSync(button, originalText);
    } catch (error) {
        button.disabled = false;
        button.textContent = originalText;
        showToast('启动扫描失败: ' + error.message, 'error');
    }
}

async function ut_pollMainlineKnownIssuesSync(button, originalText) {
    try {
        const response = await fetch('/api/mainline-known-issues/sync/status');
        const result = await response.json();
        const status = result.status || {};
        if (status.running) {
            setTimeout(() => ut_pollMainlineKnownIssuesSync(button, originalText), 3000);
            return;
        }
        button.disabled = false;
        button.textContent = originalText;
        if (status.error) {
            showToast('扫描失败: ' + status.error, 'error');
            if (typeof notifyOperationResult === 'function') {
                notifyOperationResult('Mainline包豁免项扫描失败', status.error, 'error', 'system');
            }
            return;
        }
        // 显示扫描完成详情
        let duration = '';
        if (status.started_at && status.finished_at) {
            const sec = Math.round((new Date(status.finished_at) - new Date(status.started_at)) / 1000);
            duration = sec >= 60 ? `耗时 ${Math.floor(sec / 60)}分${sec % 60}秒` : `耗时 ${sec}秒`;
        }
        const msg = `Mainline包豁免项扫描完成${duration ? '（' + duration + '）' : ''}`;
        showToast(msg, 'success');
        // 发送 Windows 系统通知
        if (typeof notifyOperationResult === 'function') {
            notifyOperationResult('Mainline包豁免项扫描完成', duration || '扫描已完成', 'success', 'system');
        }
    } catch (error) {
        button.disabled = false;
        button.textContent = originalText;
        showToast('扫描状态查询失败: ' + error.message, 'error');
    }
}

async function ut_startGmsUpdateMonitorSync(button, tool) {
    ut_gmsUpdateSyncButton = button;
    const syncTitle = (tool && tool.sync_title) || 'GMS/CTS更新';
    const sources = (tool && Array.isArray(tool.sync_sources)) ? tool.sync_sources : [];
    try {
        const resp = await fetch('/api/gms-update-monitor/sync/status');
        const data = await resp.json();
        const status = (data.data && data.data.status) || {};
        if (status.running) {
            showToast('更新扫描正在进行中，请稍候', 'warning');
            return;
        }
        if (!status.db_exists) {
            const confirmed = await showConfirmDialog(
                '首次扫描提示',
                '首次使用将执行全量扫描，请确保本机浏览器已可正常访问 docs.partner.android.com。'
            );
            if (confirmed) {
                ut_confirmGmsUpdateMonitorSync('full', sources, syncTitle);
            }
            return;
        }
    } catch (e) {
        // 查询失败时继续确认全量扫描
    }
    const confirmed = await showConfirmDialog(
        syncTitle + '扫描',
        '确定执行全量扫描吗？'
    );
    if (confirmed) {
        ut_confirmGmsUpdateMonitorSync('full', sources, syncTitle);
    }
}

async function ut_confirmGmsUpdateMonitorSync(mode, sources, syncTitle) {
    const button = ut_gmsUpdateSyncButton;
    if (!button) {
        showToast('未找到扫描按钮，请刷新页面后重试', 'error');
        return;
    }
    sources = Array.isArray(sources) ? sources : [];
    syncTitle = syncTitle || 'GMS/CTS更新';
    const modeText = mode === 'full' ? '全量扫描' : '增量扫描';
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = '扫描中';
    try {
        const params = new URLSearchParams({ mode });
        sources.forEach(source => params.append('source', source));
        const result = await apiCall(
            '/api/gms-update-monitor/sync?' + params.toString(),
            'POST'
        );
        if (!result.success) {
            throw new Error(result.error || '启动扫描失败');
        }
        showToast(syncTitle + modeText + '已启动', 'info');
        ut_pollGmsUpdateMonitorSync(button, originalText, syncTitle, sources);
    } catch (error) {
        button.disabled = false;
        button.textContent = originalText;
        showToast('启动扫描失败: ' + error.message, 'error');
    }
}

async function ut_pollGmsUpdateMonitorSync(button, originalText, syncTitle, sources) {
    syncTitle = syncTitle || 'GMS/CTS更新';
    sources = Array.isArray(sources) ? sources : [];
    try {
        const response = await fetch('/api/gms-update-monitor/sync/status');
        const result = await response.json();
        const status = (result.data && result.data.status) || {};
        if (status.running) {
            setTimeout(() => ut_pollGmsUpdateMonitorSync(button, originalText, syncTitle, sources), 3000);
            return;
        }
        button.disabled = false;
        button.textContent = originalText;
        if (status.error) {
            showToast(syncTitle + '扫描失败: ' + status.error, 'error');
            if (typeof notifyOperationResult === 'function') {
                notifyOperationResult(syncTitle + '扫描失败', status.error, 'error', 'system');
            }
            return;
        }
        let duration = '';
        if (status.started_at && status.finished_at) {
            const sec = Math.round((new Date(status.finished_at) - new Date(status.started_at)) / 1000);
            duration = sec >= 60 ? `耗时 ${Math.floor(sec / 60)}分${sec % 60}秒` : `耗时 ${sec}秒`;
        }
        showToast(syncTitle + '扫描完成' + (duration ? '，' + duration : ''), 'success');
        if (typeof notifyOperationResult === 'function') {
            notifyOperationResult(syncTitle + '扫描完成', (status.stdout || '').trim() || '扫描完成', 'success', 'system');
        }
        if (syncTitle === '测试套件更新') {
            ut_promptNewSuiteDownloads(sources);
        }
    } catch (error) {
        setTimeout(() => ut_pollGmsUpdateMonitorSync(button, originalText, syncTitle, sources), 5000);
    }
}

async function ut_promptNewSuiteDownloads(sources) {
    try {
        const params = new URLSearchParams({ limit: '20' });
        (Array.isArray(sources) ? sources : []).forEach(source => params.append('source_key', source));
        const response = await fetch('/api/gms-update-monitor/artifacts/new?' + params.toString());
        const result = await response.json();
        const items = result && result.data && Array.isArray(result.data.items) ? result.data.items : [];
        if (!items.length) return;
        const first = items[0];
        const confirmed = await showConfirmDialog(
            '发现新测试套件',
            `本次扫描发现 ${items.length} 个新测试套件。是否跳转到“测试套件”页面并填入下载地址？`
        );
        if (!confirmed) return;
        switchPage('test-suites');
        setTimeout(() => {
            const input = document.getElementById('suite-download-url');
            if (input) {
                input.value = first.download_url || '';
                input.focus();
                input.select();
            }
            showToast('已填入最新套件下载地址，可点击“⬇️ 下载套件”开始下载', 'info');
        }, 120);
    } catch (error) {
        console.warn('检查新增测试套件失败:', error);
    }
}

async function downloadTestSuite() {
    const input = document.getElementById('suite-download-url');
    const url = input ? input.value.trim() : '';
    if (!url) {
        showToast('请输入测试套件下载地址', 'warning');
        return;
    }
    const button = document.getElementById('btn-download-suite');
    const progressWrap = document.getElementById('suite-download-progress');
    const statusEl = document.getElementById('suite-progress-status');
    const barEl = document.getElementById('suite-progress-bar');
    const percentEl = document.getElementById('suite-progress-percent');
    if (button) button.disabled = true;
    if (progressWrap) progressWrap.style.display = 'block';
    if (statusEl) statusEl.textContent = '准备下载...';
    if (barEl) barEl.style.width = '0%';
    if (percentEl) percentEl.textContent = '0%';
    try {
        const response = await fetch('/api/test/suites/download-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.error || result.message || '启动下载失败');
        }
        showToast(result.message || '下载任务已启动', 'success');
        if (result.task_id) {
            pollTestSuiteDownload(result.task_id, button);
        } else {
            if (statusEl) statusEl.textContent = result.message || '下载完成';
            if (barEl) barEl.style.width = '100%';
            if (percentEl) percentEl.textContent = '100%';
            if (button) button.disabled = false;
            if (typeof refreshTestSuiteBrowser === 'function') refreshTestSuiteBrowser();
        }
    } catch (error) {
        if (button) button.disabled = false;
        if (statusEl) statusEl.textContent = '下载失败';
        showToast('下载套件失败: ' + error.message, 'error');
    }
}

async function pollTestSuiteDownload(taskId, button) {
    const statusEl = document.getElementById('suite-progress-status');
    const barEl = document.getElementById('suite-progress-bar');
    const percentEl = document.getElementById('suite-progress-percent');
    try {
        const response = await fetch('/api/test/suites/download-status/' + encodeURIComponent(taskId));
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.error || '下载状态查询失败');
        }
        const task = result.task || {};
        const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
        if (statusEl) statusEl.textContent = task.message || task.status || '下载中...';
        if (barEl) barEl.style.width = progress.toFixed(0) + '%';
        if (percentEl) percentEl.textContent = progress.toFixed(0) + '%';
        if (task.status === 'completed') {
            if (button) button.disabled = false;
            showToast('测试套件下载完成', 'success');
            if (typeof refreshTestSuiteBrowser === 'function') refreshTestSuiteBrowser();
            return;
        }
        if (task.status === 'error') {
            if (button) button.disabled = false;
            showToast('测试套件下载失败: ' + (task.error || '未知错误'), 'error');
            return;
        }
        setTimeout(() => pollTestSuiteDownload(taskId, button), 1500);
    } catch (error) {
        if (button) button.disabled = false;
        showToast('下载状态查询失败: ' + error.message, 'error');
    }
}

function ut_downloadTool(toolId, title) {
    if (!toolId) {
        showToast('该工具未配置下载文件', 'warning');
        return;
    }
    const encodedPath = String(toolId).split('/').map(encodeURIComponent).join('/');
    const a = document.createElement('a');
    a.href = `/api/tools/download/${encodedPath}`;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    showToast(`正在下载: ${title}`, 'info');
}

// --- Tool CRUD ---
function ut_addNewToolToCategory(category) {
    ut_editingTool = null;
    ut_updateCategorySelect();
    document.getElementById('ut-tool-modal-title').textContent = '添加工具到 ' + category;
    document.getElementById('ut-tool-category').value = category;
    document.getElementById('ut-tool-title').value = '';
    document.getElementById('ut-tool-icon').value = '';
    document.getElementById('ut-tool-desc').value = '';
    document.getElementById('ut-tool-filepath').value = '';
    ModalManager.open('ut-tool-modal');
}

function ut_editTool(category, index) {
    const tool = ut_categorizedTools[category][index];
    ut_editingTool = { category, index };
    ut_updateCategorySelect();
    document.getElementById('ut-tool-modal-title').textContent = '✏️ 编辑工具';
    document.getElementById('ut-tool-category').value = category;
    document.getElementById('ut-tool-title').value = tool.title || '';
    document.getElementById('ut-tool-icon').value = tool.icon || '';
    document.getElementById('ut-tool-desc').value = tool.description || '';
    document.getElementById('ut-tool-filepath').value = tool.tool_id || tool.file_path || '';
    ModalManager.open('ut-tool-modal');
}

function ut_saveTool() {
    const category = document.getElementById('ut-tool-category').value.trim();
    const title = document.getElementById('ut-tool-title').value.trim();
    const icon = document.getElementById('ut-tool-icon').value.trim() || '🔧';
    const description = document.getElementById('ut-tool-desc').value.trim();
    const filePath = document.getElementById('ut-tool-filepath').value.trim();

    if (!category) { showToast('请选择分类', 'warning'); return; }
    if (!title) { showToast('请输入工具名称', 'warning'); return; }

    if (!ut_categorizedTools[category]) {
        ut_categorizedTools[category] = [];
    }

    const toolData = { icon, title, description, tool_id: filePath };

    if (ut_editingTool) {
        // 编辑模式
        const oldCat = ut_editingTool.category;
        const oldIdx = ut_editingTool.index;
        if (oldCat === category) {
            ut_categorizedTools[category][oldIdx] = toolData;
        } else {
            ut_categorizedTools[oldCat].splice(oldIdx, 1);
            if (ut_categorizedTools[oldCat].length === 0) delete ut_categorizedTools[oldCat];
            ut_categorizedTools[category].push(toolData);
        }
    } else {
        ut_categorizedTools[category].push(toolData);
    }

    ut_saveCategories();
    ut_renderToolsGrid();
    const isEdit = !!ut_editingTool;
    ut_closeToolModal();
    showToast(isEdit ? '工具已更新' : '工具已添加', 'success');
}

function ut_deleteTool(category, index) {
    _deleteTool(ut_categorizedTools, category, index, ut_saveCategories, ut_renderToolsGrid, { showName: true });
}

function ut_closeToolModal() {
    _closeModal('ut-tool-modal', () => { ut_editingTool = null; });
}

// --- Category CRUD ---
function ut_showAddCategoryModal() {
    _showAddCategoryModal('ut-category-modal', { name: 'ut-category-name', icon: 'ut-category-icon' }, () => { ut_editingCategory = null; });
}

function ut_closeCategoryModal() {
    _closeModal('ut-category-modal', () => { ut_editingCategory = null; });
}

function ut_saveCategory() {
    const name = document.getElementById('ut-category-name').value.trim();
    const icon = document.getElementById('ut-category-icon').value.trim() || '📁';

    if (!name) { showToast('请输入分类名称', 'warning'); return; }
    if (ut_categorizedTools[name] && !ut_editingCategory) {
        showToast('分类已存在', 'warning'); return;
    }

    if (ut_editingCategory && ut_editingCategory !== name) {
        // 重命名
        ut_categorizedTools[name] = ut_categorizedTools[ut_editingCategory];
        ut_categoryMeta[name] = ut_categoryMeta[ut_editingCategory] || UT_DEFAULT_CATEGORIES[ut_editingCategory] || {};
        delete ut_categorizedTools[ut_editingCategory];
        delete ut_categoryMeta[ut_editingCategory];
    }
    if (!ut_categorizedTools[name]) {
        ut_categorizedTools[name] = [];
    }
    ut_categoryMeta[name] = {
        ...(ut_categoryMeta[name] || UT_DEFAULT_CATEGORIES[name] || {}),
        icon,
    };

    ut_saveCategories();
    ut_renderToolsGrid();
    ut_closeCategoryModal();
    showToast(`分类"${name}"已保存`, 'success');
}

function ut_editCategory(oldName) {
    ut_editingCategory = oldName;
    document.getElementById('ut-category-name').value = oldName;
    const catInfo = ut_getCategoryInfo(oldName);
    document.getElementById('ut-category-icon').value = catInfo.icon;
    ModalManager.open('ut-category-modal');
}

function ut_deleteCategory(categoryName) {
    _deleteCategory(ut_categorizedTools, categoryName, [(cat) => delete ut_categoryMeta[cat]], ut_saveCategories, ut_renderToolsGrid);
}

function ut_updateCategorySelect() {
    const select = document.getElementById('ut-tool-category');
    if (!select) return;
    select.innerHTML = '';
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = '-- 选择分类 --';
    select.appendChild(empty);
    Object.keys(ut_categorizedTools).forEach(cat => {
        const option = document.createElement('option');
        option.value = cat;
        option.textContent = cat;
        select.appendChild(option);
    });
}

// --- Utility Tool File Browser (reuses #file-browser-modal) ---
async function ut_browseFiles() {
    state.fileBrowser.mode = 'utility-tool';
    state.fileBrowser.targetInputId = 'ut-tool-filepath';
    state.fileBrowser.selectedFile = null;
    state.fileBrowser.currentPath = '';
    document.getElementById('file-browser-title').textContent = '选择可下载工具';
    ModalManager.open('file-browser-modal');
    await ut_loadToolDir('');
}

async function ut_loadToolDir(subpath) {
    try {
        const r = await fetch('/api/tools/browse', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: subpath })
        });
        const data = await r.json();
        if (!data.success) { showToast('加载失败: ' + (data.error || ''), 'error'); return; }
        state.fileBrowser.currentPath = data.path || '';
        // 复用 app.js 中的 renderFileList 来渲染文件列表
        const pathDisplay = document.getElementById('file-browser-current-path');
        pathDisplay.textContent = '可下载工具清单';
        const listContainer = document.getElementById('file-browser-list');
        if (data.files.length === 0) {
            listContainer.innerHTML = '<div class="file-browser-item" style="cursor: default; color: var(--text-muted);">空目录</div>';
            return;
        }
        listContainer.innerHTML = '';
        data.files.forEach(file => {
            const item = document.createElement('div');
            item.className = 'file-browser-item';
            item.addEventListener('click', (event) => selectFileForSelection(file.name, file.type, event));
            item.addEventListener('dblclick', () => ut_openToolFileOrDir(file.name, file.type, file.tool_id));

            const icon = document.createElement('span');
            icon.className = 'file-browser-icon';
            icon.textContent = file.type === 'directory' ? '📁' : '📄';
            item.appendChild(icon);

            const name = document.createElement('span');
            name.className = 'file-browser-name';
            name.textContent = file.name;
            item.appendChild(name);

            if (file.type === 'file') {
                const size = document.createElement('span');
                size.style.cssText = 'color: var(--text-muted); font-size: 11px;';
                size.textContent = formatBytes(file.size, true);
                item.appendChild(size);
            }
            listContainer.appendChild(item);
        });
    } catch (e) {
        showToast('加载失败: ' + e.message, 'error');
    }
}

function ut_openToolFileOrDir(name, type, toolId) {
    if (type === 'directory') {
        const current = state.fileBrowser.currentPath;
        const newPath = current ? current + '/' + name : name;
        ut_loadToolDir(newPath);
    } else {
        selectFileForSelection(name, type);
        if (toolId) state.fileBrowser.selectedFile.tool_id = toolId;
    }
}
