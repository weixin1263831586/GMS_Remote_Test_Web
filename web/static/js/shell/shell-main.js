        // ==================== Shell 引导（boot 保护 + 初始页面恢复）====================
        // 页面加载完成后，如果是终端页面则初始化终端
        window.addEventListener('load', () => {
            // 核心脚本（state.js/api.js/navigation.js）因网络错误未加载时，内联逻辑会
            // 因缺少全局 state 崩溃（控制台报 "state is not defined"）。
            // 这里给出明确提示而非 ReferenceError，便于定位到本机网络问题。
            if (
        typeof state === 'undefined'
        || typeof apiCall === 'undefined'
        || window.GmsNavigationReady !== true
            ) {
        console.error('[Boot] 核心脚本未加载完成，页面初始化中止');
        const bootBanner = document.createElement('div');
        bootBanner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;'
            + 'background:#b3261e;color:#fff;padding:10px 16px;font-size:14px;'
            + 'text-align:center;line-height:1.6;';
        bootBanner.textContent = '页面核心脚本加载失败（网络错误），功能暂不可用。'
            + '请刷新重试；若反复出现，请关闭本站多余标签页并检查本机网络代理/端口占用后重启浏览器。';
        document.body.appendChild(bootBanner);
        return;
            }
            // 获取页面加载前确定的目标页面
            const pageWasSwitchedBeforeLoad = Boolean(window.__shellPageSwitchedBeforeLoad);
            let targetPage = pageWasSwitchedBeforeLoad
        ? currentPage
        : (window.__targetPage || localStorage.getItem('gms_current_page') || readCurrentPageCookie() || 'test');
            applySidebarVisibility(getSavedSidebarVisiblePages());
            targetPage = resolveVisiblePage(targetPage);

            // 保存初始页面状态到 localStorage（确保首次访问后刷新能保持）
            saveCurrentPageState(targetPage);

            // 移除临时内联样式
            const initStyle = document.getElementById('page-init-style');
            if (initStyle) {
        initStyle.remove();
            }

            // 认证状态确认后再读取客户端信息，避免首次初始化页面产生 401。
            runAfterAuthReady(initClientInfo);

            // Sidebar 事件委托：替代逐项 inline onclick（去 unsafe-inline 的
            // 第一批迁移）。data-page 已标注目标页面，keydown 同样支持键盘导航。
            document.getElementById('sidebar-nav')?.addEventListener('click', event => {
                const item = event.target.closest('.sidebar-item[data-page]');
                if (item) {
                    switchPage(item.dataset.page, event);
                }
            });

            if (pageWasSwitchedBeforeLoad && targetPage === currentPage) {
        return;
            }

            // 先设置导航栏高亮（在显示页面前）
            document.querySelectorAll('.sidebar-item').forEach(item => {
        item.classList.remove('active');
            });
            const activeItem = document.querySelector(`[data-page="${targetPage}"]`);
            if (activeItem) {
        activeItem.classList.add('active');
            }

            // 使用正常的类系统显示目标页面
            document.querySelectorAll('.page-content').forEach(page => {
        page.classList.remove('active');
            });
            const targetPageEl = document.getElementById(`page-${targetPage}`);
            if (targetPageEl) {
        targetPageEl.classList.add('active');
            }

            // 刷新恢复页面时显式加载懒加载 iframe。
            ensureLazyFrameLoaded(targetPage, true);
            window.GmsWorkspace?.setActivePage(targetPage);

            currentPage = targetPage;

            // 更新页面标题
            document.title = PAGE_TITLES[targetPage] || 'GMS远程测试';
            runAfterAuthReady(function() {
        if (typeof recordSecurityPageView === 'function') {
            recordSecurityPageView(targetPage);
        }
            });
            runPageInitializers(targetPage);

        });
