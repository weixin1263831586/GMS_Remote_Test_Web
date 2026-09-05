// Shell 模块：常用网址管理（从 shell.html 内联脚本尾部提取）。
// ==================== 常用网址管理（支持分类）====================
let categorizedTools = {};
let currentCategory = 'all';
const TOOLS_STORAGE_KEY = 'gms_tools_list';
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
            const localTools = JSON.parse(categorizedStored);

            // 清理"其他"分类
            if (localTools['其他']) {
                // 将旧的"其他"分类归入当前公司分类。
                if (localTools[COMPANY_CATEGORY]) {
                    localTools[COMPANY_CATEGORY] = localTools[COMPANY_CATEGORY].concat(localTools['其他']);
                } else {
                    localTools[COMPANY_CATEGORY] = localTools['其他'];
                }
                delete localTools['其他'];
                saveCategories();
            }

            categorizedTools = localTools;
            renderToolsGrid();
            return;
        } catch (e) {
            console.error('加载分类数据失败:', e);
        }
    }

    // 将未分类存储转换为分类结构。
    const stored = localStorage.getItem(TOOLS_STORAGE_KEY);
    if (stored) {
        try {
            const oldTools = JSON.parse(stored);
            categorizedTools = migrateToCategories(oldTools);
            saveCategories();
            renderToolsGrid();
            return;
        } catch (e) {
            console.error('加载工具列表失败:', e);
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

    saveCategories();
    renderToolsGrid();
}

function migrateToCategories(oldTools) {
    const categorized = {};
    const defaultCategories = Object.keys(DEFAULT_CATEGORIES);

    // 初始化分类
    defaultCategories.forEach(cat => {
        categorized[cat] = [];
    });

    // 智能分类
    oldTools.forEach(tool => {
        const category = guessCategory(tool);
        if (!categorized[category]) {
            categorized[category] = [];
        }
        categorized[category].push(tool);
    });

    return categorized;
}

function guessCategory(tool) {
    const url = tool.url.toLowerCase();
    const title = tool.title.toLowerCase();

    // 归类到配置的公司分类。
    if (COMPANY_KEYWORDS.some(keyword => url.includes(keyword) || title.includes(keyword))) {
        return COMPANY_CATEGORY;
    }

    // Google 相关
    if (url.includes('google') || url.includes('gmail') || url.includes('youtube')) {
        return 'Google';
    }

    // 开发工具
    if (url.includes('github') || url.includes('gitlab') || url.includes('stackoverflow')) {
        return '开发工具';
    }


    // 默认归到当前公司分类。
    return COMPANY_CATEGORY;
}

function saveCategories() {
    // 保存到本地 localStorage
    localStorage.setItem(CATEGORIES_STORAGE_KEY, JSON.stringify(categorizedTools));

    // 同步到服务器
    syncToolsToServer();
}
