// Shell 模块：常用网址管理（从 shell.html 内联脚本尾部提取）。
// ==================== 常用网址管理（支持分类）====================
let categorizedTools = {};
let currentCategory = 'all';
const CATEGORIES_STORAGE_KEY = 'gms_tools_categories';

// 默认分类
const DEFAULT_CATEGORIES = {
    [COMPANY_CATEGORY]: { icon: COMPANY_ICON, color: '#ff6b6b' },
    'Google': { icon: '🔍', color: '#4285f4' },
    '开发工具': { icon: '🛠️', color: '#00d26a' },
    '社交媒体': { icon: '💬', color: '#ff9500' }
};

// 常用 Emoji 列表
const commonEmojis = [
    '🌐', '📚', '🎵', '🎮', '📊', '📈', '🔧', '⚙️',
    '💻', '🖥️', '📱', '⌨️', '🖱️', '🔌', '💾', '💿',
    '🎨', '🎬', '📷', '🎥', '📺', '📻', '🎧', '🎙️',
    '⚡', '🔥', '💡', '🌟', '✨', '🎯', '🏆', '🎪',
    '🌍', '🌎', '🌏', '🗺️', '🧭', '📍', '🔱', '⛰️',
    '🚀', '✈️', '🛸', '🚁', '🚂', '🚗', '🏠', '🏢',
    '📧', '📨', '📩', '📤', '📥', '📦', '📫', '📪',
    '🔒', '🔓', '🔑', '🗝️', '🔐', '🔎', '🔍', '🔬'
];

function loadToolsList() {
    initToolsPageBody('websites');
    // 首先尝试从服务器同步数据
    loadToolsFromServer().then(() => {
        // 服务器同步完成后，继续处理本地数据
        continueLoadingLocalData();
    }).catch(() => {
        // 如果服务器同步失败，继续处理本地数据
        continueLoadingLocalData();
    });
}

function continueLoadingLocalData() {
    // 检查本地是否有数据
    const categorizedStored = localStorage.getItem(CATEGORIES_STORAGE_KEY);
    if (categorizedStored) {
        try {
            categorizedTools = JSON.parse(categorizedStored);
            renderToolsGrid();
            return;
        } catch (e) {
            console.error('加载分类数据失败:', e);
        }
    }

    // 使用默认数据
    categorizedTools = {
        [COMPANY_CATEGORY]: COMPANY_HOME_URL
            ? [{ icon: COMPANY_ICON, title: `${COMPANY_CATEGORY} 官网`, url: COMPANY_HOME_URL }]
            : [],
        'Google': [
            { icon: '🔍', title: 'Google', url: 'https://www.google.com' },
            { icon: '📧', title: 'Gmail', url: 'https://gmail.com' }
        ],
        '开发工具': [
            { icon: '📦', title: 'GitHub', url: 'https://github.com' },
            ...(EXTERNAL_SERVICES.grafana_url
                ? [{ icon: '📊', title: 'Grafana', url: EXTERNAL_SERVICES.grafana_url }]
                : [])
        ]
    };

    saveCategories({ sync: false });
    renderToolsGrid();
}

function saveCategories({ sync = true } = {}) {
    // Persist locally; synchronize only explicit user changes.
    localStorage.setItem(CATEGORIES_STORAGE_KEY, JSON.stringify(categorizedTools));

    if (sync) {
        void syncToolsToServer();
    }
}
