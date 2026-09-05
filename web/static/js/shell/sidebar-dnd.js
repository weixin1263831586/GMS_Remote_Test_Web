// Shell 模块：导航栏拖拽排序（从 shell.html 内联脚本尾部提取）。
// ==================== 导航栏拖拽排序 ====================
let draggedItem = null;

function getSidebarPages(nav) {
    return Array.from(nav.querySelectorAll('.sidebar-item'))
        .map(item => item.dataset.page)
        .filter(Boolean);
}

function applySidebarOrder(nav, order) {
    const items = Array.from(nav.querySelectorAll('.sidebar-item'));
    const itemByPage = new Map(items.map(item => [item.dataset.page, item]));
    const currentPages = getSidebarPages(nav);
    const currentSet = new Set(currentPages);
    // 只保留 order 里真实存在的页面
    const orderedPages = order.filter(page => itemByPage.has(page));

    // 排序必须覆盖全部页面，否则保留 HTML 默认顺序。
    if (orderedPages.length !== currentPages.length) {
        return;
    }

    orderedPages.forEach(page => {
        nav.appendChild(itemByPage.get(page));
    });
}

// 页面加载时立即应用保存的导航栏排序（同步执行，防止闪烁）
(function applySavedSidebarOrder() {
    const savedOrder = window.__savedSidebarOrder;
    const nav = document.getElementById('sidebar-nav');
    if (!nav) return;

    // applySidebarOrder 会忽略不完整或无效的保存顺序。
    if (savedOrder && Array.isArray(savedOrder) && savedOrder.length > 0) {
        applySidebarOrder(nav, savedOrder);
    }

    applySidebarVisibility(getSavedSidebarVisiblePages());
})();

function saveSidebarOrder() {
    const nav = document.getElementById('sidebar-nav');
    if (!nav) return;

    const order = getSidebarPages(nav);

    // 同时更新 localStorage 和后端
    localStorage.setItem('gms_sidebar_order', JSON.stringify(order));
    saveSidebarOrderToBackend(order);
}

async function saveSidebarOrderToBackend(order) {
    try {
        const response = await fetch('/api/sidebar-order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order })
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const data = await response.json();
        if (data.success === false) {
            throw new Error(data.error || 'Save failed');
        }
    } catch (e) {
        console.error('[Sidebar] Failed to save to backend:', e);
    }
}

function isExternalDrop(dataTransfer) {
    if (!dataTransfer || !dataTransfer.types) return false;
    return Array.from(dataTransfer.types).some(type =>
        type === 'Files' ||
        type === 'text/uri-list' ||
        type === 'URL' ||
        type === 'text/html'
    );
}

function isSidebarUploadTarget(item) {
    return item && ['report-analysis', 'apk-analysis'].includes(item.dataset.page);
}

async function handleSidebarUploadDrop(targetItem, dataTransfer) {
    const page = targetItem?.dataset.page;
    if (!page) return false;

    if (page === 'report-analysis') {
        switchPage('report-analysis');
        if (typeof window.handleReportDataTransfer === 'function') {
            await window.handleReportDataTransfer(dataTransfer);
            return true;
        }
        return false;
    }

    if (page === 'apk-analysis') {
        const file = dataTransfer?.files?.[0];
        if (!file) return false;

        switchPage('apk-analysis');
        if (typeof window.initApkAnalysisPage === 'function') {
            window.initApkAnalysisPage();
        }
        if (typeof window.handleApkFile === 'function') {
            await window.handleApkFile(file);
            return true;
        }
    }

    return false;
}

function initSidebarDragDrop() {
    const nav = document.getElementById('sidebar-nav');
    if (!nav) return;

    nav.addEventListener('dragstart', function(e) {
        const item = e.target.closest('.sidebar-item');
        if (!item) return;

        draggedItem = item;
        item.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', item.dataset.page);
    });

    nav.addEventListener('dragend', function(e) {
        const item = e.target.closest('.sidebar-item');
        if (!item) return;

        item.classList.remove('dragging');
        draggedItem = null;

        nav.querySelectorAll('.sidebar-item').forEach(el => {
            el.classList.remove('drag-over');
        });

        saveSidebarOrder();
    });

    nav.addEventListener('dragover', function(e) {
        const item = e.target.closest('.sidebar-item');
        if (isExternalDrop(e.dataTransfer) && isSidebarUploadTarget(item)) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'copy';

            nav.querySelectorAll('.sidebar-item').forEach(el => {
                el.classList.remove('drag-over');
            });
            item.classList.add('drag-over');
            return;
        }

        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';

        if (!item || item === draggedItem) return;

        nav.querySelectorAll('.sidebar-item').forEach(el => {
            el.classList.remove('drag-over');
        });
        item.classList.add('drag-over');
    });

    nav.addEventListener('dragleave', function(e) {
        const item = e.target.closest('.sidebar-item');
        if (item) {
            item.classList.remove('drag-over');
        }
    });

    nav.addEventListener('drop', function(e) {
        e.preventDefault();

        const targetItem = e.target.closest('.sidebar-item');
        if (isExternalDrop(e.dataTransfer) && isSidebarUploadTarget(targetItem)) {
            targetItem.classList.remove('drag-over');
            handleSidebarUploadDrop(targetItem, e.dataTransfer);
            return;
        }

        if (!targetItem || !draggedItem || targetItem === draggedItem) return;

        const nav = document.getElementById('sidebar-nav');
        const items = Array.from(nav.querySelectorAll('.sidebar-item'));
        const dragIndex = items.indexOf(draggedItem);
        const dropIndex = items.indexOf(targetItem);

        if (dragIndex < dropIndex) {
            nav.insertBefore(draggedItem, targetItem.nextSibling);
        } else {
            nav.insertBefore(draggedItem, targetItem);
        }
    });
}

document.addEventListener('DOMContentLoaded', function() {

    // 初始化拖拽功能
    setTimeout(initSidebarDragDrop, 50);
    runAfterAuthReady(loadSidebarConfigFromBackend);

    // 初始化默认页面的组件（如果默认是 APK 分析页面）
    const defaultPage = localStorage.getItem('gms_current_page') || readCurrentPageCookie() || 'test';
    if (defaultPage === 'apk-analysis') {
        setTimeout(() => {
            if (typeof window.initApkAnalysisPage === 'function') {
                window.initApkAnalysisPage();
            }
        }, 100);
    }
});
