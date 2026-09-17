// Shell 模块：方向键导航（从 shell.html 内联脚本尾部提取）。
// ==================== 方向键导航支持 ====================
function navigateSidebarByArrow(key, target = document.activeElement) {
    if (key !== 'ArrowUp' && key !== 'ArrowDown') return false;
    target = target || document.body;

    // 在输入框、文本域等元素中不拦截
    if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' ||
        target.tagName === 'SELECT' || target.isContentEditable) {
        return false;
    }

    // 在终端页面，检查终端是否获得焦点
    if (currentPage === 'terminal') {
        const terminalElement = document.getElementById('terminal');
        // 如果点击的是终端区域或者终端内有焦点，则不拦截方向键
        if (terminalElement && (target === terminalElement || terminalElement.contains(target))) {
            return false;
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

    if (currentIndex === -1 || pages.length === 0) return false;

    let newIndex;
    if (key === 'ArrowUp') {
        // 向上：前一个可见页面
        newIndex = (currentIndex - 1 + pages.length) % pages.length;
    } else {
        // 向下：下一个可见页面
        newIndex = (currentIndex + 1) % pages.length;
    }

    // 切换页面
    const targetPage = pages[newIndex];
    switchPage(targetPage, null);
    // 切换后必须由 Shell 重新声明焦点归属：Tab iframe 进入当前 Tab，
    // 普通页面留在外层容器。否则焦点会残留在刚被隐藏的 iframe，后续
    // 上下键只能落入旧页面而无法继续导航。
    window.focusSidebarNavigationTarget?.(targetPage);
    return true;
}
window.navigateSidebarByArrow = navigateSidebarByArrow;

// iframe 内的键盘事件不能冒泡到此 document。仅接收来自当前活动页面
// iframe 的同源转发，避免隐藏页面或其他嵌入内容改变侧栏导航。
window.addEventListener('message', function(event) {
    if (event.origin !== window.location.origin
            || event.data?.type !== 'embedded-sidebar-arrow') return;
    const activeFrame = document.getElementById(`page-${currentPage}`)?.querySelector('iframe');
    if (activeFrame?.contentWindow !== event.source) return;
    navigateSidebarByArrow(event.data.key);
});

document.addEventListener('keydown', function(e) {
    // 只处理方向键
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    if (navigateSidebarByArrow(e.key, e.target)) {
        e.preventDefault();
    }
});

function updateCategorySelect() {
    const select = document.getElementById('tool-category');
    if (!select) return;

    const categories = Object.keys(categorizedTools);

    // 如果没有分类，使用默认分类
    if (categories.length === 0) {
        categories.push(...Object.keys(DEFAULT_CATEGORIES));
    }

    select.replaceChildren();
    categories.forEach(category => {
        const categoryInfo = DEFAULT_CATEGORIES[category] || { icon: '📁', color: '#8e8e93' };
        const option = document.createElement('option');
        option.value = category;
        option.textContent = `${categoryInfo.icon} ${category}`;
        select.appendChild(option);
    });

    // 添加当前选中的分类（如果不在列表中）
    if (currentCategory !== 'all' && !categories.includes(currentCategory)) {
        const categoryInfo = DEFAULT_CATEGORIES[currentCategory] || { icon: '📁', color: '#8e8e93' };
        const option = document.createElement('option');
        option.value = currentCategory;
        option.textContent = `${categoryInfo.icon} ${currentCategory}`;
        select.appendChild(option);
    }
}
