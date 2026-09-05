// Shell 模块：拖拽排序（从 shell.html 内联脚本尾部提取）。
// ==================== 拖拽排序功能 ====================
let draggedTool = null;
let draggedCategory = null;
let draggedCategoryIndex = null;

// 工具卡片拖拽事件处理
function handleToolDragStart(e) {
    const card = e.target.closest('.tool-card');
    if (!card) return;

    draggedTool = {
        category: card.dataset.toolCategory,
        index: parseInt(card.dataset.toolIndex)
    };

    card.style.opacity = '0.4';
    e.dataTransfer.effectAllowed = 'move';
}

function handleToolDragEnd(e) {
    const card = e.target.closest('.tool-card');
    if (card) {
        card.style.opacity = '1';
    }

    // 移除所有拖拽样式
    document.querySelectorAll('.tool-card').forEach(c => {
        c.style.transform = '';
        c.style.boxShadow = '';
    });

    draggedTool = null;
}

function handleToolDragOver(e) {
    e.preventDefault();
    const card = e.target.closest('.tool-card');
    if (!card || !draggedTool) return;

    // 不允许在自己身上拖拽
    if (card.dataset.toolCategory === draggedTool.category &&
        parseInt(card.dataset.toolIndex) === draggedTool.index) {
        return;
    }

    card.style.transform = 'scale(1.05)';
    card.style.boxShadow = 'var(--shadow-lg)';
}

function handleToolDrop(e) {
    e.preventDefault();
    const card = e.target.closest('.tool-card');
    if (!card || !draggedTool) return;

    const targetCategory = card.dataset.toolCategory;
    const targetIndex = parseInt(card.dataset.toolIndex);

    if (targetCategory === draggedTool.category &&
        targetIndex === draggedTool.index) return;

    moveTool(categorizedTools, draggedTool.category, draggedTool.index, targetCategory, targetIndex, saveCategories);
    renderToolsGrid();
}

// 通用工具移动函数
function moveTool(dataObj, fromCategory, fromIndex, toCategory, toIndex, saveFn) {
    const fromTools = dataObj[fromCategory];
    const toTools = dataObj[toCategory];
    if (!fromTools || !toTools) return;
    const [tool] = fromTools.splice(fromIndex, 1);
    const insertAt = (fromCategory === toCategory && fromIndex < toIndex) ? toIndex - 1 : toIndex;
    toTools.splice(insertAt, 0, tool);
    saveFn();
}

// 分类拖拽事件处理
function handleCategoryDragStart(e) {
    const section = e.target.closest('.category-section');
    if (!section) return;

    draggedCategoryIndex = parseInt(section.dataset.categoryIndex);
    draggedCategory = section.dataset.category;

    e.target.style.opacity = '0.4';
    e.dataTransfer.effectAllowed = 'move';
}

function handleCategoryDragEnd(e) {
    const sections = document.querySelectorAll('.category-section');
    sections.forEach(s => {
        s.style.opacity = '1';
        s.style.transform = '';
        s.style.boxShadow = '';
    });

    draggedCategory = null;
    draggedCategoryIndex = null;
}

function handleCategoryDragOver(e) {
    e.preventDefault();
    const section = e.target.closest('.category-section');
    if (!section || draggedCategoryIndex === null) return;

    const targetIndex = parseInt(section.dataset.categoryIndex);
    if (targetIndex === draggedCategoryIndex) return;

    section.style.transform = 'scale(1.02)';
    section.style.boxShadow = 'var(--shadow-lg)';
}

function handleCategoryDrop(e) {
    e.preventDefault();
    const section = e.target.closest('.category-section');
    if (!section || draggedCategoryIndex === null) return;

    const targetIndex = parseInt(section.dataset.categoryIndex);
    if (targetIndex === draggedCategoryIndex) return;

    moveCategory(categorizedTools, draggedCategoryIndex, targetIndex, (newData) => { categorizedTools = newData; saveCategories(); });
    renderToolsGrid();
}

// 通用分类移动函数
function moveCategory(dataObj, fromIndex, toIndex, applyFn) {
    const categories = Object.keys(dataObj);
    if (fromIndex < 0 || fromIndex >= categories.length ||
        toIndex < 0 || toIndex >= categories.length) return;
    const categoryToMove = categories[fromIndex];
    const newOrder = {};
    const entries = Object.entries(dataObj);
    if (fromIndex < toIndex) {
        entries.forEach((entry, i) => {
            if (i === fromIndex) return;
            if (i === toIndex) newOrder[categoryToMove] = dataObj[categoryToMove];
            newOrder[entry[0]] = entry[1];
        });
    } else {
        entries.forEach((entry, i) => {
            if (i === toIndex) newOrder[categoryToMove] = dataObj[categoryToMove];
            if (i === fromIndex) return;
            newOrder[entry[0]] = entry[1];
        });
    }
    applyFn(newOrder);
}

// 初始化拖拽功能
function initDragAndDrop() {
    // 分类拖拽
    document.querySelectorAll('.category-section').forEach(section => {
        section.addEventListener('dragstart', handleCategoryDragStart);
        section.addEventListener('dragend', handleCategoryDragEnd);
        section.addEventListener('dragover', handleCategoryDragOver);
        section.addEventListener('drop', handleCategoryDrop);
    });
}

// 修改 renderAllCategories 函数，在渲染完成后调用 initDragAndDrop

function addNewToolToCategory(category) {
    updateCategorySelect();

    document.getElementById('tool-index').value = '-1';
    document.getElementById('tool-category').value = category;
    document.getElementById('tool-modal-title').textContent = '添加工具到 ' + category;
    document.getElementById('tool-icon').value = '🌐';
    document.getElementById('tool-title').value = '';
    document.getElementById('tool-url').value = '';
    // Emoji选择器现在默认显示，不需要隐藏逻辑

    ModalManager.open('tool-modal');
}

function editTool(category, index) {
    updateCategorySelect();

    const tool = categorizedTools[category]?.[index];
    if (!tool) return;

    document.getElementById('tool-index').value = `${category}|${index}`;
    document.getElementById('tool-category').value = category;
    document.getElementById('tool-modal-title').textContent = '✏️ 编辑工具';
    document.getElementById('tool-icon').value = tool.icon || '🌐';
    document.getElementById('tool-title').value = tool.title || '';
    document.getElementById('tool-url').value = tool.url || '';
    // Emoji选择器现在默认显示，不需要隐藏逻辑

    ModalManager.open('tool-modal');
}

function deleteTool(category, index) {
    _deleteTool(categorizedTools, category, index, saveCategories, renderToolsGrid, {
        onCategoryEmpty: (cat) => { if (currentCategory === cat) currentCategory = 'all'; }
    });
}

function saveTool() {
    const indexValue = document.getElementById('tool-index').value;
    const category = document.getElementById('tool-category').value;
    const icon = document.getElementById('tool-icon').value.trim() || '🌐';
    const title = document.getElementById('tool-title').value.trim();
    let url = document.getElementById('tool-url').value.trim();

    if (!title || !url) {
        showToast('标题和链接不能为空', 'error');
        return;
    }

    // 自动添加 https:// 前缀
    if (!url.match(/^https?:\/\//)) {
        url = 'https://' + url;
    }

    const tool = { icon, title, url };

    if (indexValue === '-1') {
        // 添加新工具
        if (!categorizedTools[category]) {
            categorizedTools[category] = [];
        }
        categorizedTools[category].push(tool);
    } else {
        // 编辑现有工具
        const [oldCategory, oldIndex] = indexValue.split('|');

        // 如果分类发生变化
        if (oldCategory !== category) {
            // 从原分类删除。
            categorizedTools[oldCategory].splice(parseInt(oldIndex), 1);

            // 删除空分类。
            if (categorizedTools[oldCategory].length === 0) {
                delete categorizedTools[oldCategory];
            }

            // 添加到分类
            if (!categorizedTools[category]) {
                categorizedTools[category] = [];
            }
            categorizedTools[category].push(tool);
        } else {
            // 同一分类内更新
            categorizedTools[category][parseInt(oldIndex)] = tool;
        }
    }

    saveCategories();
    renderToolsGrid();

    // 关闭模态框 - 使用统一的关闭函数
    closeToolModal();

    showToast('保存成功', 'success');
}

function closeToolModal() {
    _closeModal('tool-modal');
}

function showAddCategoryModal() {
    _showAddCategoryModal('category-modal', { name: 'category-name', icon: 'category-icon', iconDefault: '📁' });
}

function closeCategoryModal() {
    _closeModal('category-modal');
}

function saveCategory() {
    const name = document.getElementById('category-name').value.trim();
    const icon = document.getElementById('category-icon').value.trim() || '📁';

    if (!name) {
        showToast('请输入分类名称', 'error');
        return;
    }

    // 检查分类是否已存在
    if (categorizedTools[name]) {
        showToast('该分类已存在', 'error');
        return;
    }

    // 添加分类
    categorizedTools[name] = [];

    // 更新默认分类配置
    DEFAULT_CATEGORIES[name] = { icon: icon, color: '#8e8e93' };

    saveCategories();
    renderToolsGrid();
    closeCategoryModal();
    showToast('分类添加成功', 'success');
}

function editCategory(oldName, evt) {
    const categoryInfo = DEFAULT_CATEGORIES[oldName] || { icon: '📁', color: '#8e8e93' };
    const categoryHeader = evt.target.closest('.category-header');

    if (!categoryHeader) return;

    // 将分类名称替换为输入框
    const nameSpan = categoryHeader.querySelector('.category-name');
    if (!nameSpan) return;

    const currentName = nameSpan.textContent;
    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentName;
    input.style.cssText = 'font-size: 16px; font-weight: 600; color: var(--text-primary); background: var(--darker-bg); border: 1px solid var(--border-color); border-radius: 4px; padding: 4px 8px; width: 150px;';

    // 替换显示
    nameSpan.style.display = 'none';
    input.addEventListener('blur', function() {
        const newName = this.value.trim();
        if (newName && newName !== currentName) {
            if (categorizedTools[newName] && newName !== oldName) {
                showToast('该分类名称已存在', 'error');
                input.focus();
                return;
            }

            // 重命名分类
            categorizedTools[newName] = categorizedTools[oldName];
            delete categorizedTools[oldName];

            // 更新默认分类配置
            DEFAULT_CATEGORIES[newName] = categoryInfo;
            delete DEFAULT_CATEGORIES[oldName];

            saveCategories();
            renderToolsGrid();
            showToast('分类重命名成功', 'success');
        } else {
            // 恢复原显示
            nameSpan.style.display = '';
            input.remove();
        }
    });

    input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            this.blur();
        } else if (e.key === 'Escape') {
            nameSpan.style.display = '';
            input.remove();
        }
    });

    nameSpan.parentNode.insertBefore(input, nameSpan);
    input.focus();
}

function deleteCategory(categoryName) {
    _deleteCategory(categorizedTools, categoryName, [(cat) => delete DEFAULT_CATEGORIES[cat]], saveCategories, renderToolsGrid);
}

function selectEmoji(emoji) {
    document.getElementById('tool-icon').value = emoji;
}
