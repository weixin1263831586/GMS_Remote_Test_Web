// Shell 模块：全局Esc关闭弹框（从 shell.html 内联脚本尾部提取）。
// ==================== 全局 Esc 键关闭弹框 ====================
document.addEventListener('keydown', function(e) {
    if (e.key !== 'Escape') return;
    // 关闭 mainline-sync-modal
    const mainlineModal = document.getElementById('mainline-sync-modal');
    if (mainlineModal && mainlineModal.style.display === 'flex') {
        ut_closeMainlineSyncModal();
        e.preventDefault();
        return;
    }
    // 关闭 websites 页面的 tool/category modal
    if (currentPage === 'websites') {
        const toolModal = document.getElementById('tool-modal');
        if (toolModal && toolModal.classList.contains('show')) {
            closeToolModal();
            e.preventDefault();
            return;
        }
        const categoryModal = document.getElementById('category-modal');
        if (categoryModal && categoryModal.classList.contains('show')) {
            closeCategoryModal();
            e.preventDefault();
            return;
        }
    }
    // 关闭 tools 页面的 ut-tool/ut-category modal
    if (currentPage === 'tools') {
        const utToolModal = document.getElementById('ut-tool-modal');
        if (utToolModal && utToolModal.classList.contains('show')) {
            ut_closeToolModal();
            e.preventDefault();
            return;
        }
        const utCategoryModal = document.getElementById('ut-category-modal');
        if (utCategoryModal && utCategoryModal.classList.contains('show')) {
            ut_closeCategoryModal();
            e.preventDefault();
            return;
        }
    }
});
