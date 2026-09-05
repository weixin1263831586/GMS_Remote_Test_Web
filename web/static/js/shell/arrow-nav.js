// Shell 模块：方向键导航（从 shell.html 内联脚本尾部提取）。
// ==================== 方向键导航支持 ====================
document.addEventListener('keydown', function(e) {
    // 只处理方向键
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;

    // 在输入框、文本域等元素中不拦截
    const target = e.target;
    if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' ||
        target.tagName === 'SELECT' || target.isContentEditable) {
        return;
    }

    // 在终端页面，检查终端是否获得焦点
    if (currentPage === 'terminal') {
        const terminalElement = document.getElementById('terminal');
        // 如果点击的是终端区域或者终端内有焦点，则不拦截方向键
        if (terminalElement && (target === terminalElement || terminalElement.contains(target))) {
            return;
        }
    }

    // 从 DOM 获取当前导航栏的顺序（支持拖拽排序后）。
    // 只在「可见」的导航项之间跳转：被侧边栏可见性设置隐藏的项
    // （style.display === 'none'）会被跳过，否则 switchPage 会经
    // resolveVisiblePage 把隐藏页重定向到第一个可见页（test），
    // 导致方向键在隐藏页位置上「直接跳到测试界面」。
    const navItems = Array.from(document.querySelectorAll('.sidebar-item')).filter(
        item => item.style.display !== 'none'
    );
    const pages = navItems.map(item => item.dataset.page);
    const currentIndex = pages.indexOf(currentPage);

    if (currentIndex === -1 || pages.length === 0) return;

    let newIndex;
    if (e.key === 'ArrowUp') {
        // 向上：前一个可见页面
        newIndex = (currentIndex - 1 + pages.length) % pages.length;
    } else {
        // 向下：下一个可见页面
        newIndex = (currentIndex + 1) % pages.length;
    }

    // 切换页面
    const targetPage = pages[newIndex];
    switchPage(targetPage, null);
    e.preventDefault();
});

function updateCategorySelect() {
    const select = document.getElementById('tool-category');
    if (!select) return;

    const categories = Object.keys(categorizedTools);

    // 如果没有分类，使用默认分类
    if (categories.length === 0) {
        categories.push(...Object.keys(DEFAULT_CATEGORIES));
    }

    select.innerHTML = categories.map(category => {
        const categoryInfo = DEFAULT_CATEGORIES[category] || { icon: '📁', color: '#8e8e93' };
        return `<option value="${category}">${categoryInfo.icon} ${category}</option>`;
    }).join('');

    // 添加当前选中的分类（如果不在列表中）
    if (currentCategory !== 'all' && !categories.includes(currentCategory)) {
        const categoryInfo = DEFAULT_CATEGORIES[currentCategory] || { icon: '📁', color: '#8e8e93' };
        const option = document.createElement('option');
        option.value = currentCategory;
        option.textContent = `${categoryInfo.icon} ${currentCategory}`;
        select.appendChild(option);
    }
}
