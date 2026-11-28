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

// 网址与工具卡片按分类位置共用强调色，分类图标保留各自颜色。
const TOOLS_CATEGORY_PALETTE = [
    '#4285f4', '#34a853', '#ea4335', '#7c3aed', '#0ea5e9', '#ff9500', '#8e8e93',
];
function toolsCategoryColor(catIndex) {
    const i = Number.isInteger(catIndex) && catIndex >= 0 ? catIndex : 0;
    return TOOLS_CATEGORY_PALETTE[i % TOOLS_CATEGORY_PALETTE.length];
}
const UT_BUILTIN_TOOLS = {
    '常用工具': [
        {
            icon: '🌐',
            title: 'vpn-proxy',
            description: '管理本机 VPN HTTP/HTTPS 代理服务、查看接入客户端与复制客户端部署命令',
            action: 'openVpnProxyPanel',
            builtin_id: 'builtin-vpn-proxy'
        }
    ],
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
        window.GmsUpdateRadarSettings.builtinTool,
        {
            icon: '📦',
            title: 'Mainline包豁免项',
            description: '查看Mainline包豁免项，支持按模块、用例和豁免ID搜索',
            url: '/gms-update-center?tab=exemptions',
            action: 'mainline-sync',
            builtin_id: 'mainline-known-issues'
        },
        {
            icon: '🩹',
            title: 'Critical_Patch',
            description: '查看GMS关键修复补充公告（Critical Fix Supplementary Bulletins），扫描入口在GMS 更新中心',
            url: '/gms-update-center?tab=critical',
            action: 'gms-update-sync',
            builtin_id: 'gms-bulletins-monitor',
            sync_sources: ['gms_bulletins'],
            sync_title: 'Critical_Patch'
        },
        {
            icon: '✅',
            title: '测试套件更新',
            description: '扫描Android 14+ CTS/VTS/GTS和GMS包更新，默认全量扫描',
            url: '/gms-update-center?tab=artifacts',
            action: 'gms-update-sync',
            builtin_id: 'gms-update-suite-monitor',
            sync_sources: ['cts_downloads', 'vts_downloads', 'gts_downloads', 'gms_downloads'],
            sync_title: '测试套件更新'
        },
        {
            icon: '📦',
            title: 'GMS包更新',
            description: '扫描并查看Android 14+ GMS包更新表格，默认全量扫描',
            url: '/gms-update-center?tab=packages',
            action: 'gms-update-sync',
            builtin_id: 'gms-package-monitor',
            sync_sources: ['gms_downloads'],
            sync_title: 'GMS包更新'
        },
        {
            icon: '🚆',
            title: 'Mainline包更新',
            description: '扫描并查看Mainline PRELOAD包更新表格，默认抓取近12个月',
            url: '/gms-update-center?tab=mainline',
            action: 'gms-update-sync',
            builtin_id: 'gms-mainline-monitor',
            sync_sources: ['mainline_preload'],
            sync_title: 'Mainline包更新'
        },
        {
            icon: '📋',
            title: 'GMS要求',
            description: '扫描GMS认证要求章节和表格更新，默认展示顶层主章节',
            url: '/gms-update-center?tab=requirements',
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
let ut_mainlinePollTimer = null;
let ut_mainlinePollGeneration = 0;
let ut_gmsUpdatePollTimer = null;
let ut_gmsUpdatePollGeneration = 0;

// ==================== 常用工具 UI 逻辑 ====================
// 原内联于 weekly-report.js 尾部的常用工具网格/编辑/同步逻辑，
// 与周报功能无关，移回本模块（file-size budget 拆分）。
// 依赖：本文件头部的 UT_* 常量与状态，以及全局 showToast/state。
function ut_getCategoryInfo(category) {
    return ut_categoryMeta[category] || UT_DEFAULT_CATEGORIES[category] || { icon: '📁', color: '#8e8e93' };
}

function ut_loadToolsList() {
    initToolsPageBody('tools');
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
            let migrated = false;
            for (const tools of Object.values(ut_categorizedTools)) {
                for (const tool of tools) {
                    if (tool && !tool.tool_id && tool.file_path) {
                        tool.tool_id = tool.file_path;
                        migrated = true;
                    }
                }
            }
            if (migrated) ut_saveCategories();
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
const UT_HIDDEN_CATEGORIES_KEY = 'gms_utility_tools_hidden_categories';

// Deleting a built-in category is a persistent user preference, not a reason
// to re-create the category during built-in tool reconciliation.
function ut_getHiddenCategories() {
    try {
        const values = JSON.parse(localStorage.getItem(UT_HIDDEN_CATEGORIES_KEY) || '[]');
        return new Set(Array.isArray(values) ? values.filter(name => typeof name === 'string') : []);
    } catch (_error) {
        return new Set();
    }
}

function ut_setCategoryHidden(category, hidden) {
    const categories = ut_getHiddenCategories();
    if (hidden) categories.add(category);
    else categories.delete(category);
    localStorage.setItem(UT_HIDDEN_CATEGORIES_KEY, JSON.stringify([...categories]));
}

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
    const hiddenCategories = ut_getHiddenCategories();
    Object.entries(UT_BUILTIN_TOOLS).forEach(([category, tools]) => {
        if (hiddenCategories.has(category)) return;
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
    // 动作按钮委托分发（与常用网址页同一套 data-tools-action 模型）。
    toolsBindGridActions(grid, {
        'open-tool': el => openToolLink(el.dataset.url || ''),
        'builtin-open': el => { const fn = window[el.dataset.builtinAction]; if (typeof fn === 'function') fn(); },
        'download-tool': el => ut_downloadTool(el.dataset.toolId, el.dataset.toolTitle),
        'edit-category': el => ut_editCategory(el.dataset.category),
        'add-tool': el => ut_addNewToolToCategory(el.dataset.category),
        'delete-category': el => ut_deleteCategory(el.dataset.category),
        'edit-tool': el => { const index = Number.parseInt(el.dataset.index || '', 10); if (Number.isInteger(index)) ut_editTool(el.dataset.category, index); },
        'delete-tool': el => { const index = Number.parseInt(el.dataset.index || '', 10); if (Number.isInteger(index)) ut_deleteTool(el.dataset.category, index); },
        'trigger-sync': el => {
            const index = Number.parseInt(el.dataset.index || '', 10);
            const tool = ut_categorizedTools[el.dataset.category]?.[index];
            if (!tool) return;
            if (el.dataset.syncKind === 'gms-update-sync') ut_startGmsUpdateMonitorSync(el, tool);
            else ut_startMainlineKnownIssuesSync(el);
        },
        'radar-edit': el => {
            el.disabled = true;
            window.GmsUpdateRadarSettings.open()
                .catch(error => window.showToast?.(error.message, 'error'))
                .finally(() => { el.disabled = false; });
        },
        'hide-builtin': el => {
            const category = el.dataset.category;
            const index = Number.parseInt(el.dataset.index || '', 10);
            const tool = ut_categorizedTools[category]?.[index];
            if (!tool) return;
            ut_hideBuiltin(el.dataset.builtinId);
            ut_categorizedTools[category].splice(index, 1);
            ut_saveCategories();
            ut_renderToolsGrid();
            showToast(`已隐藏「${tool.title}」`, 'info');
        },
    });
    ut_restoreSyncButtonState();
}

async function ut_restoreSyncButtonState() {
    try {
        const checks = [
            {
                action: 'mainline-sync',
                url: '/api/gms-update-center/scan/status',
                parse: data => (data.data && data.data.status) || {},
                poll: (btn) => { ut_mainlineSyncButton = btn; ut_pollMainlineKnownIssuesSync(btn, '触发扫描'); }
            },
            {
                action: 'gms-update-sync',
                url: '/api/gms-update-center/scan/status',
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
                const titleEl = card.querySelector('div[style*="font-weight: var(--ui-type-weight-semibold)"]');
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
    const categories = Object.keys(ut_categorizedTools);
    if (categories.length === 0) {
        grid.innerHTML = toolsRenderEmptyGrid({
            icon: '🧰', title: '还没有工具', hint: '点击下方按钮添加分类和工具',
        });
        return;
    }

    grid.innerHTML = categories.map((category, catIndex) => toolsRenderCategorySection({
        category,
        catIndex,
        icon: ut_getCategoryInfo(category).icon,
        count: ut_categorizedTools[category].length,
        cardsHtml: ut_categorizedTools[category]
            .map((tool, index) => ut_createToolCard(tool, category, index, toolsCategoryColor(catIndex)))
            .join('\n'),
    })).join('');

    // 分类节拖拽本页直绑；dnd-sort.js 的 initDragAndDrop 还会按
    // .category-section 全局叠加绑定（历史现状，见文件尾隐患标注）。
    grid.querySelectorAll('.category-section').forEach(section => {
        if (section.dataset.utDragBound === 'true') return;
        section.dataset.utDragBound = 'true';
        section.addEventListener('dragstart', handleCategoryDragStart);
        section.addEventListener('dragend', handleCategoryDragEnd);
        section.addEventListener('dragover', handleCategoryDragOver);
        section.addEventListener('drop', ut_handleCategoryDrop);
    });

}

function ut_createToolCard(tool, category, index, cardColor) {
    const isSyncTool = tool.action === 'mainline-sync' || tool.action === 'gms-update-sync';
    const isShareFirmwareTool = tool.action === 'shareFirmware';
    // 有 tool_id 且无 url 的下载型工具不显示下载按钮（点击卡片即下载）
    const isDownloadOnlyTool = !isSyncTool && tool.tool_id && !tool.url;

    const actions = [];
    if (tool.builtin_id === 'gms-update-radar') {
        actions.push(`
                <button type="button" class="btn-act act-edit" aria-label="修改" title="修改雷达执行时间、收件人、发送策略和监听任务" data-tools-action="radar-edit">
                    ✏️
                </button>`);
    }
    // sync 工具显示触发按钮
    if (isSyncTool) {
        actions.push(`
                <button type="button" class="btn-act act-primary" aria-label="触发扫描" title="${tool.action === 'gms-update-sync' ? '扫描 GMS/CTS 更新' : '扫描 Mainline release notes'}" data-tools-action="trigger-sync" data-sync-kind="${tool.action}" data-category="${escapeIconAttr(category)}" data-index="${index}">
                    触发扫描
                </button>`);
    }
    // 只有有 url 的非内置工具才显示下载按钮
    if (!tool.builtin_id && !isSyncTool && !isShareFirmwareTool && !isDownloadOnlyTool) {
        actions.push(`
                <button type="button" class="btn-act act-primary" aria-label="下载文件" title="下载文件" data-tools-action="download-tool" data-tool-id="${escapeIconAttr(tool.tool_id)}" data-tool-title="${escapeIconAttr(tool.title)}">
                    下载
                </button>`);
    }
    if (!tool.builtin_id) {
        actions.push(`
                <button type="button" class="btn-act act-edit" aria-label="编辑工具" data-tools-action="edit-tool" data-category="${escapeIconAttr(category)}" data-index="${index}">
                    ✏️
                </button>`);
        actions.push(`
                <button type="button" class="btn-act act-danger" aria-label="删除工具" data-tools-action="delete-tool" data-category="${escapeIconAttr(category)}" data-index="${index}">
                    🗑️
                </button>`);
    }
    // Built-in tools get a hide button so users can remove them.
    if (tool.builtin_id) {
        actions.push(`
                <button type="button" class="btn-act act-danger" aria-label="隐藏此内置工具" title="隐藏此内置工具" data-tools-action="hide-builtin" data-builtin-id="${escapeIconAttr(tool.builtin_id)}" data-category="${escapeIconAttr(category)}" data-index="${index}">
                    🗑️
                </button>`);
    }

    // 卡片点击语义与原逐节点绑定一致：内置动作 > url 打开 > tool_id 下载。
    let clickAttrs = '';
    if (['openWeeklyReport', 'shareFirmware', 'openVpnProxyPanel'].includes(tool.action)) {
        clickAttrs = `data-tools-action="builtin-open" data-builtin-action="${tool.action}"`;
    } else if (tool.url) {
        clickAttrs = `data-tools-action="open-tool" data-url="${escapeIconAttr(tool.url)}"`;
    } else if (tool.tool_id && !isSyncTool) {
        clickAttrs = `data-tools-action="download-tool" data-tool-id="${escapeIconAttr(tool.tool_id)}" data-tool-title="${escapeIconAttr(tool.title)}"`;
    }

    return toolsRenderToolCard({
        tool,
        category,
        index,
        cardColor,
        actionsHtml: actions.join(''),
        clickAttrs,
        dragAttrs: 'data-dragstart="handleToolDragStart" data-r0="event"'
            + ' data-dragend="handleToolDragEnd" data-r1="event"'
            + ' data-dragover="handleToolDragOver" data-r2="event"'
            + ' data-drop="ut_handleToolDrop" data-r3="event"',
    });
}

// 常用工具拖拽排序。
// 隐患标注（暂不改架构）：本页复用 dnd-sort.js 的共享拖拽状态
// （draggedTool/draggedCategory/draggedCategoryIndex）与
// dragstart/dragover 处理器，仅 drop 换成下面的 ut_* 版本操作
// ut_categorizedTools。dnd-sort.js 的 initDragAndDrop 用全局
// .category-section 选择器绑事件，不排除本页节点；两页网格又都
// 常驻 DOM，跨页拖拽或叠加绑定会把对方页面的索引/数据集混进
// 本页的 moveTool/moveCategory。改动任一侧需同步核对另一侧。

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
        const resp = await fetch('/api/gms-update-center/scan/status');
        const data = await resp.json();
        if (data.data?.status?.running) {
            showToast('扫描正在进行中，请稍候', 'warning');
            return;
        }
        if (!data.data?.status?.db_exists) {
            // db 不存在，弹框提示后直接全量扫描
            showConfirmDialog(
                '首次扫描提示',
                '首次使用需要全量扫描。扫描会读取本机 Firefox 中 docs.partner.android.com 的 Google 登录 Cookie（页面无需打开），登录已失效时请在 Firefox 中重新登录后重试。'
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
            '/api/gms-update-center/scan?source=mainline_preload&mode=' + encodeURIComponent(mode),
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

async function ut_pollMainlineKnownIssuesSync(button, originalText, generation = null) {
    if (generation === null) {
        if (ut_mainlinePollTimer) clearTimeout(ut_mainlinePollTimer);
        generation = ++ut_mainlinePollGeneration;
    }
    try {
        const response = await fetch('/api/gms-update-center/scan/status');
        const result = await response.json();
        if (generation !== ut_mainlinePollGeneration) return;
        const status = (result.data && result.data.status) || {};
        if (status.running) {
            ut_mainlinePollTimer = setTimeout(
                () => ut_pollMainlineKnownIssuesSync(button, originalText, generation),
                3000
            );
            return;
        }
        ut_mainlinePollTimer = null;
        button.disabled = false;
        button.textContent = originalText;
        if (status.error) {
            showToast('扫描失败: ' + status.error, 'error');
            if (typeof notifyOperationResult === 'function') {
                notifyOperationResult('Mainline包豁免项扫描失败', status.error, 'error', 'system');
            }
            return;
        }
        let duration = '';
        if (status.started_at && status.finished_at) {
            const sec = Math.round((new Date(status.finished_at) - new Date(status.started_at)) / 1000);
            duration = sec >= 60 ? `耗时 ${Math.floor(sec / 60)}分${sec % 60}秒` : `耗时 ${sec}秒`;
        }
        const msg = `Mainline包豁免项扫描完成${duration ? '（' + duration + '）' : ''}`;
        showToast(msg, 'success');
        if (typeof notifyOperationResult === 'function') {
            notifyOperationResult('Mainline包豁免项扫描完成', duration || '扫描已完成', 'success', 'system');
        }
    } catch (error) {
        if (generation !== ut_mainlinePollGeneration) return;
        ut_mainlinePollTimer = null;
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
        const resp = await fetch('/api/gms-update-center/scan/status');
        const data = await resp.json();
        const status = (data.data && data.data.status) || {};
        if (status.running) {
            showToast('更新扫描正在进行中，请稍候', 'warning');
            return;
        }
        if (!status.db_exists) {
            const confirmed = await showConfirmDialog(
                '首次扫描提示',
                '首次使用将执行全量扫描。扫描会读取本机 Firefox 中 docs.partner.android.com 的 Google 登录 Cookie（页面无需打开），登录已失效时请在 Firefox 中重新登录后重试。'
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
            '/api/gms-update-center/scan?' + params.toString(),
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

async function ut_pollGmsUpdateMonitorSync(
    button, originalText, syncTitle, sources, generation = null, failures = 0
) {
    syncTitle = syncTitle || 'GMS/CTS更新';
    sources = Array.isArray(sources) ? sources : [];
    if (generation === null) {
        if (ut_gmsUpdatePollTimer) clearTimeout(ut_gmsUpdatePollTimer);
        generation = ++ut_gmsUpdatePollGeneration;
    }
    try {
        const response = await fetch('/api/gms-update-center/scan/status');
        const result = await response.json();
        if (generation !== ut_gmsUpdatePollGeneration) return;
        const status = (result.data && result.data.status) || {};
        if (status.running) {
            ut_gmsUpdatePollTimer = setTimeout(
                () => ut_pollGmsUpdateMonitorSync(
                    button, originalText, syncTitle, sources, generation, 0
                ),
                3000
            );
            return;
        }
        ut_gmsUpdatePollTimer = null;
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
        if (generation !== ut_gmsUpdatePollGeneration) return;
        if (failures >= 5) {
            ut_gmsUpdatePollTimer = null;
            button.disabled = false;
            button.textContent = originalText;
            showToast('扫描状态查询失败: ' + error.message, 'error');
            return;
        }
        const delay = Math.min(30000, 3000 * (2 ** failures));
        ut_gmsUpdatePollTimer = setTimeout(
            () => ut_pollGmsUpdateMonitorSync(
                button, originalText, syncTitle, sources, generation, failures + 1
            ),
            delay
        );
    }
}

async function ut_promptNewSuiteDownloads(sources) {
    try {
        const params = new URLSearchParams({ limit: '20' });
        (Array.isArray(sources) ? sources : []).forEach(source => params.append('source_key', source));
        const response = await fetch('/api/gms-update-center/artifacts/new?' + params.toString());
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
            if (typeof refreshTestSuiteBrowser === 'function') runSuiteBrowserAction(() => refreshTestSuiteBrowser());
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
            if (typeof refreshTestSuiteBrowser === 'function') runSuiteBrowserAction(() => refreshTestSuiteBrowser());
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
    const encodedPath = encodeURIComponent(toolId);
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
    document.getElementById('ut-tool-filepath').value = tool.tool_id || '';
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

// 共享按钮按页面打开各自的分类模态框。
function toolsPageAddCategory(kind) {
    if (kind === 'tools') {
        ut_showAddCategoryModal();
        return;
    }
    showAddCategoryModal();
}

// 为共享片段设置各页原有 ID 与按钮目标；重复切页不重复初始化。
function initToolsPageBody(kind) {
    const host = document.querySelector(`[data-tools-page="${kind}"]`);
    if (!host || host.dataset.toolsPageReady === 'true') return;
    host.dataset.toolsPageReady = 'true';
    const scroll = host.querySelector('[data-tools-scroll]');
    const grid = host.querySelector('[data-tools-grid]');
    const addBtn = host.querySelector('[data-tools-add]');
    if (scroll) {
        scroll.id = host.dataset.scrollId || '';
        delete scroll.dataset.toolsScroll;
    }
    if (grid) {
        grid.id = host.dataset.gridId || '';
        delete grid.dataset.toolsGrid;
    }
    if (addBtn) {
        addBtn.dataset.click = 'toolsPageAddCategory';
        addBtn.dataset.r0 = 'dataset.toolsKind';
        addBtn.dataset.toolsKind = host.dataset.addKind || kind;
        delete addBtn.dataset.toolsAdd;
    }
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
        // Renaming away from a built-in category also suppresses the old name.
        ut_setCategoryHidden(ut_editingCategory, true);
    }
    if (!ut_categorizedTools[name]) {
        ut_categorizedTools[name] = [];
    }
    ut_categoryMeta[name] = {
        ...(ut_categoryMeta[name] || UT_DEFAULT_CATEGORIES[name] || {}),
        icon,
    };

    // Adding a deleted category re-enables its built-in tools.
    const wasHidden = ut_getHiddenCategories().has(name);
    ut_setCategoryHidden(name, false);
    if (wasHidden) ut_ensureBuiltInTools();
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
    _deleteCategory(ut_categorizedTools, categoryName, [
        (cat) => {
            delete ut_categoryMeta[cat];
            ut_setCategoryHidden(cat, true);
        }
    ], ut_saveCategories, ut_renderToolsGrid);
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
        const pathDisplay = document.getElementById('file-browser-current-path');
        pathDisplay.textContent = '可下载工具清单';
        pathDisplay.title = '可下载工具清单';
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
                size.style.cssText = 'color: var(--text-muted); font-size: var(--ui-type-size-compact);';
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
