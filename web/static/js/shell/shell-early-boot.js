        (function() {
            const sidebarAllPages = ['test', 'desktop', 'terminal', 'users', 'devices', 'devices-console', 'reports', 'report-analysis', 'apk-analysis', 'test-suites', 'api-docs', 'architecture', 'websites', 'tools', 'security-audit', 'gms-assistant', 'automation', 'cluster', 'redmine-agent', 'gerrit-dashboard', 'agent', 'notes'];
            const pageTitles = {
                'test': '测试界面 - GMS远程测试',
                'desktop': '主机桌面 - GMS远程测试',
                'terminal': '主机终端 - GMS远程测试',
                'users': '用户管理 - GMS远程测试',
                'devices': '设备管理 - GMS远程测试',
                'devices-console': '设备串口 - GMS 远程测试',
                'reports': '报告管理 - GMS远程测试',
                'report-analysis': '报告分析 - GMS远程测试',
                'apk-analysis': 'APK分析 - GMS远程测试',
                'test-suites': '测试套件 - GMS远程测试',
                'api-docs': '系统接口 - GMS远程测试',
                'architecture': '系统架构 - GMS远程测试',
                'websites': '常用网址 - GMS远程测试',
                'tools': '常用工具 - GMS远程测试',
                'security-audit': '安全审计 - GMS 远程测试',
                'gms-assistant': 'GMS助手 - GMS 远程测试',
                'automation': 'GMS ATS - GMS 远程测试',
                'cluster': '主机集群 - GMS 远程测试',
                'redmine-agent': 'Redmine - GMS 远程测试',
                'gerrit-dashboard': 'Gerrit看板 - GMS 远程测试',
                'notes': '个人知识库 - GMS 远程测试',
                'agent': '对话Agent - GMS 远程测试'
            };
            const saveCurrentPageCookie = (pageName) => {
                document.cookie = 'gms_current_page=' + encodeURIComponent(pageName) + '; path=/; max-age=31536000; SameSite=Lax';
            };
            const readCurrentPageCookie = () => {
                const match = document.cookie.match(/(?:^|;\s*)gms_current_page=([^;]+)/);
                return match ? decodeURIComponent(match[1]) : '';
            };

            // 确定要显示的页面（在页面渲染前）
            const hash = window.location.hash.substring(1);
            const hashPage = hash.split('?')[0];
            const savedPage = localStorage.getItem('gms_current_page') || readCurrentPageCookie() || 'test';
            let savedVisiblePages = null;
            try {
                savedVisiblePages = JSON.parse(localStorage.getItem('gms_sidebar_visible_pages') || 'null');
                if (!Array.isArray(savedVisiblePages) || savedVisiblePages.length === 0) {
                    savedVisiblePages = null;
                } else {
                    for (const requiredPage of ['notes', 'cluster']) {
                        if (!savedVisiblePages.includes(requiredPage)) savedVisiblePages.push(requiredPage);
                    }
                    localStorage.setItem('gms_sidebar_visible_pages', JSON.stringify(savedVisiblePages));
                }
            } catch (e) {
                console.error('[Sidebar] Failed to parse visible pages:', e);
                savedVisiblePages = null;
            }
            const visiblePages = savedVisiblePages || sidebarAllPages;

            // 优先使用 hash，否则使用保存的页面
            let targetPage;
            if (hashPage && sidebarAllPages.includes(hashPage)) {
                targetPage = hashPage;
            } else {
                targetPage = savedPage;
            }
            if (!visiblePages.includes(targetPage)) {
                targetPage = visiblePages.includes('test') ? 'test' : (visiblePages[0] || 'test');
            }

            // 保存目标页面供后续使用
            window.__targetPage = targetPage;
            saveCurrentPageCookie(targetPage);
            document.title = pageTitles[targetPage] || 'GMS远程测试';

            // 尝试获取保存的导航栏排序（从 localStorage）- 使用与保存时相同的 key
            let savedOrder = null;
            try {
                // 直接使用 gms_sidebar_order（与 saveSidebarOrder 保持一致）
                savedOrder = localStorage.getItem('gms_sidebar_order');
                if (savedOrder) {
                    savedOrder = JSON.parse(savedOrder);
                    if (Array.isArray(savedOrder) && savedOrder.includes('devices') && savedOrder.includes('devices-console')) {
                        savedOrder = savedOrder.filter(page => page !== 'devices-console');
                        savedOrder.splice(savedOrder.indexOf('devices') + 1, 0, 'devices-console');
                        localStorage.setItem('gms_sidebar_order', JSON.stringify(savedOrder));
                    }
                }
            } catch (e) {
                console.error('[Sidebar] Failed to parse saved order:', e);
                savedOrder = null;
            }

            // 保存排序供 DOMContentLoaded 使用
            window.__savedSidebarOrder = savedOrder;
            window.__savedSidebarVisiblePages = savedVisiblePages;

            // 添加内联样式来立即显示正确的页面和导航栏选中状态
            var style = document.createElement('style');
            style.id = 'page-init-style';
            style.textContent =
                // 隐藏所有页面内容
                '.page-content { display: none !important; } ' +
                // 显示目标页面（flex 列布局，使内嵌 iframe 的 flex:1 生效）
                '#page-' + targetPage + ' { display: flex !important; flex-direction: column !important; } ' +
                // 移除所有导航项的选中状态
                '.sidebar-item { background: transparent !important; color: var(--text-secondary) !important; } ' +
                // 设置目标导航项的选中状态
                '[data-page="' + targetPage + '"] { background: var(--active-bg) !important; color: var(--primary-color) !important; }' +
                (Array.isArray(savedOrder) ? savedOrder.map(function(page, index) {
                    return '.sidebar-item[data-page="' + page + '"] { order: ' + index + '; }';
                }).join('') : '');
            document.head.appendChild(style);
        })();

        // 提前在 DOMContentLoaded 触发懒加载 iframe，无需等到 window load。
        // 这样 iframe 的网络请求与剩余资源加载重叠，缩短结构骨架的持续时间。
        document.addEventListener('DOMContentLoaded', function() {
            var earlyPage = window.__targetPage || 'test';
            runAfterAuthReady(function() {
                if (typeof ensureLazyFrameLoaded === 'function') {
                    ensureLazyFrameLoaded(earlyPage, false);
                }
            });
        }, {once: true});
