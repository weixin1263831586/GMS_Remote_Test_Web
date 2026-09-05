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
