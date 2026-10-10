        // ==================== 页面切换功能 ====================
        let currentPage = window.__targetPage || 'test';
        let terminalInitialized = false;
        let terminal = null;
        let terminalSocket = null;
        let isTerminalConnected = false;
        let terminalDomEventController = null;
        // 页面标题映射
        const PAGE_TITLES = {
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

        const SIDEBAR_VISIBLE_STORAGE_KEY = 'gms_sidebar_visible_pages';
        const SIDEBAR_PAGE_DESCRIPTIONS = {
            test: '选择 CTS/GTS/VTS/STS 等套件，指定模块/用例和设备，启动/停止测试，查看实时日志和执行状态。',
            desktop: '启动或查看 noVNC/x11vnc 桌面，用于远程 GUI 操作、调试工具和桌面环境确认。',
            terminal: '打开服务器 SSH 终端，执行命令、上传文件、辅助定位环境问题。',
            users: '查看在线用户、客户端 IP、用户名、测试运行状态和设备占用情况。',
            devices: '查看 ADB 设备、型号、Android 版本、电量、来源、锁定状态；支持重启、remount、WiFi、投屏、bootloader 操作。',
            'devices-console': '枚举 Controller 本机 USB 转串口，绑定设备备注，查看实时控制台并管理开机早期日志。',
            reports: '列出历史测试报告，按用户/时间查看，下载、删除、进入分析。',
            'report-analysis': '上传报告或打开已有报告，解析失败项，做失败诊断、AI 分析和根因线索整理。',
            'apk-analysis': '上传 APK/JAR，反编译源码，查看 Manifest、权限、源码树和文件内容。',
            'test-suites': '浏览本地/远端套件目录，查看 tradefed 结果，下载/解压套件，触发套件内 APK/JAR 分析。',
            'api-docs': '查看 API 清单、参数、curl 示例和响应格式，适合调试接口或脚本调用。',
            architecture: '查看平台模块、数据流和核心组件关系。',
            websites: '按分类维护常用站点、图标和链接。',
            tools: '维护可下载工具条目，从服务器白名单工具目录下载脚本或二进制工具。',
            'security-audit': '查看页面访问、API 调用、请求摘要、响应摘要和耗时，辅助追踪操作记录。',
            'gms-assistant': '打开外部/内置 GMS 知识助手入口。',
            automation: '把 Gerrit 触发、构建产物、设备选择、烧写、测试执行和结果回写串成自动化测试站流程。',
            cluster: '查看 Controller/Worker、设备、套件、运行任务和部署状态，管理持久化 Cluster Job。',
            'redmine-agent': '查看个人/部门/项目 Redmine 统计、待回复、超阈值未回复、解决趋势和问题明细。',
            'gerrit-dashboard': '查看个人/部门 Gerrit 提交统计、查询变更、趋势明细和成员配置。',
            agent: '用自然语言查询设备/报告/套件，生成测试计划，打开项目页面，执行确认类操作并跟踪分析流程。',
            notes: '按知识空间和目录维护 Wiki，上传附件、全文检索、关联报告/Redmine/Gerrit 并进行知识问答。'
        };

        function getSidebarNav() {
            return document.getElementById('sidebar-nav');
        }

        function getSidebarItems() {
            const nav = getSidebarNav();
            return nav ? Array.from(nav.querySelectorAll('.sidebar-item')) : [];
        }

        function getAllSidebarPages() {
            return getSidebarItems().map(item => item.dataset.page).filter(Boolean);
        }

        function normalizeSidebarVisiblePages(pages) {
            const allPages = getAllSidebarPages();
            if (!Array.isArray(pages) || pages.length === 0) {
                return allPages;
            }
            const valid = [];
            const seen = new Set();
            pages.forEach(page => {
                if (allPages.includes(page) && !seen.has(page)) {
                    valid.push(page);
                    seen.add(page);
                }
            });
            for (const requiredPage of ['notes', 'cluster']) {
                if (allPages.includes(requiredPage) && !seen.has(requiredPage)) valid.push(requiredPage);
            }
            return valid.length > 0 ? valid : allPages;
        }

        function getSavedSidebarVisiblePages() {
            if (Array.isArray(window.__savedSidebarVisiblePages)) {
                return normalizeSidebarVisiblePages(window.__savedSidebarVisiblePages);
            }
            try {
                return normalizeSidebarVisiblePages(JSON.parse(localStorage.getItem(SIDEBAR_VISIBLE_STORAGE_KEY) || 'null'));
            } catch (e) {
                console.error('[Sidebar] Failed to parse visible pages:', e);
                return normalizeSidebarVisiblePages(null);
            }
        }

        function getCurrentVisibleSidebarPages() {
            return getSidebarItems()
                .filter(item => !item.hidden && item.style.display !== 'none')
                .map(item => item.dataset.page)
                .filter(Boolean);
        }

        function resolveVisiblePage(pageName) {
            const visiblePages = getCurrentVisibleSidebarPages();
            if (visiblePages.includes(pageName)) {
                return pageName;
            }
            if (visiblePages.includes('test')) {
                return 'test';
            }
            return visiblePages[0] || getAllSidebarPages()[0] || 'test';
        }

        function readCurrentPageCookie() {
            const match = document.cookie.match(/(?:^|;\s*)gms_current_page=([^;]+)/);
            return match ? decodeURIComponent(match[1]) : '';
        }

        function saveCurrentPageState(pageName, { updateHash = true } = {}) {
            localStorage.setItem('gms_current_page', pageName);
            document.cookie = 'gms_current_page=' + encodeURIComponent(pageName) + '; path=/; max-age=31536000; SameSite=Lax';
            if (!updateHash || !window.history || !window.location) {
                return;
            }
            const currentHash = window.location.hash.substring(1);
            if (currentHash.split('?')[0] === pageName) {
                return;
            }
            window.history.replaceState(null, '', '#' + encodeURIComponent(pageName));
        }

        function applySidebarVisibility(pages) {
            const visiblePages = normalizeSidebarVisiblePages(pages);
            const visibleSet = new Set(visiblePages);
            getSidebarItems().forEach(item => {
                item.style.display = visibleSet.has(item.dataset.page) ? '' : 'none';
            });
            return visiblePages;
        }

        function getSidebarItemLabel(item) {
            const icon = item.querySelector('.sidebar-icon')?.innerHTML || '';
            const text = item.querySelector('.sidebar-text')?.textContent?.trim() || item.dataset.page;
            return { icon, text };
        }

        function handleSidebarBrandKeydown(event) {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                openSidebarVisibilityModal();
            }
        }

        function switchSidebarSettingsTab(tabName) {
            const target = tabName === 'guide' ? 'guide' : 'visibility';
            document.querySelectorAll('[data-sidebar-settings-tab]').forEach(button => {
                const active = button.dataset.sidebarSettingsTab === target;
                button.classList.toggle('active', active);
                button.setAttribute('aria-selected', active ? 'true' : 'false');
            });
            document.querySelectorAll('[data-sidebar-settings-panel]').forEach(panel => {
                panel.hidden = panel.dataset.sidebarSettingsPanel !== target;
            });
        }

        function setGuideImageZoom(actualSize) {
            const image = document.getElementById('guide-image-preview');
            const button = document.getElementById('guide-image-zoom-btn');
            const viewport = document.getElementById('guide-image-viewport');
            if (!image || !button || !viewport) return;
            image.classList.toggle('actual-size', actualSize);
            viewport.classList.toggle('actual-size', actualSize);
            button.textContent = actualSize ? '适合窗口' : '1:1 原图';
            viewport.scrollTop = 0;
            viewport.scrollLeft = 0;
        }

        function openGuideImageLightbox(trigger) {
            const sourceImage = trigger?.querySelector('img');
            if (!sourceImage) return;
            const preview = document.getElementById('guide-image-preview');
            const title = document.getElementById('guide-image-title');
            const caption = document.getElementById('guide-image-caption');
            if (!preview || !title || !caption) return;
            preview.src = sourceImage.currentSrc || sourceImage.src;
            preview.alt = sourceImage.alt || '';
            title.textContent = trigger.dataset.guideTitle || sourceImage.alt || '图片操作实例';
            caption.textContent = trigger.dataset.guideCaption || '';
            setGuideImageZoom(false);
            ModalManager.open('guide-image-modal');
        }

        function toggleGuideImageZoom() {
            const image = document.getElementById('guide-image-preview');
            if (!image) return;
            setGuideImageZoom(!image.classList.contains('actual-size'));
        }

        function closeGuideImageLightbox() {
            ModalManager.close('guide-image-modal');
        }

        function closeGuideImageLightboxFromBackdrop(event) {
            if (event.target?.id === 'guide-image-modal') closeGuideImageLightbox();
        }

        function openSidebarVisibilityModal() {
            const modal = document.getElementById('sidebar-visibility-modal');
            const list = document.getElementById('sidebar-visibility-list');
            if (!modal || !list) return;

            const guideUrl = document.getElementById('project-guide-url');
            if (guideUrl) {
                const currentOrigin = `${window.location.origin}/`;
                guideUrl.href = currentOrigin;
                guideUrl.textContent = `${currentOrigin} ↗`;
            }

            const visibleSet = new Set(getCurrentVisibleSidebarPages());
            list.innerHTML = getSidebarItems().map(item => {
                const { icon, text } = getSidebarItemLabel(item);
                const page = item.dataset.page;
                const checked = visibleSet.has(page) ? 'checked' : '';
                const description = SIDEBAR_PAGE_DESCRIPTIONS[page] || '显示或隐藏此功能页面。';
                return `<label class="sidebar-visibility-option">
                    <input type="checkbox" value="${page}" ${checked}>
                    <span class="sidebar-icon">${icon}</span>
                    <span class="sidebar-text">${text}</span>
                    <span class="sidebar-description" title="${description}">${description}</span>
                </label>`;
            }).join('');

            switchSidebarSettingsTab('visibility');

            ModalManager.open('sidebar-visibility-modal');
        }

        function closeSidebarVisibilityModal() {
            ModalManager.close('sidebar-visibility-modal');
        }

        function selectAllSidebarVisibility() {
            document.querySelectorAll('#sidebar-visibility-list input[type="checkbox"]').forEach(input => {
                input.checked = true;
            });
        }

        function saveSidebarVisibilityFromModal() {
            const checkedPages = Array.from(document.querySelectorAll('#sidebar-visibility-list input[type="checkbox"]:checked'))
                .map(input => input.value)
                .filter(Boolean);
            if (checkedPages.length === 0) {
                if (typeof showToast === 'function') {
                    showToast('至少保留一个导航页面', 'warning');
                } else {
                    console.warn('至少保留一个导航页面');
                }
                return;
            }

            const visiblePages = applySidebarVisibility(checkedPages);
            localStorage.setItem(SIDEBAR_VISIBLE_STORAGE_KEY, JSON.stringify(visiblePages));
            window.__savedSidebarVisiblePages = visiblePages;
            saveSidebarVisibilityToBackend(visiblePages);
            closeSidebarVisibilityModal();

            const nextPage = resolveVisiblePage(currentPage);
            if (nextPage !== currentPage) {
                switchPage(nextPage);
            }
        }

        async function saveSidebarVisibilityToBackend(visiblePages) {
            const nav = getSidebarNav();
            const order = nav ? getSidebarPages(nav) : getAllSidebarPages();
            try {
                const response = await fetch('/api/sidebar-order', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ order, visible_pages: visiblePages })
                });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const data = await response.json();
                if (data.success === false) {
                    throw new Error(data.error || 'Save failed');
                }
            } catch (e) {
                console.error('[Sidebar] Failed to save visibility:', e);
            }
        }

        async function loadSidebarConfigFromBackend() {
            try {
                const response = await fetch('/api/sidebar-order');
                if (!response.ok) return;
                const data = await response.json();
                if (data.success === false || !data.data) return;

                const nav = getSidebarNav();
                if (nav && Array.isArray(data.data.order) && data.data.order.length > 0) {
                    applySidebarOrder(nav, data.data.order);
                    localStorage.setItem('gms_sidebar_order', JSON.stringify(getSidebarPages(nav)));
                }
                if (Array.isArray(data.data.visible_pages) && data.data.visible_pages.length > 0) {
                    const visiblePages = applySidebarVisibility(data.data.visible_pages);
                    localStorage.setItem(SIDEBAR_VISIBLE_STORAGE_KEY, JSON.stringify(visiblePages));
                    window.__savedSidebarVisiblePages = visiblePages;
                    const nextPage = resolveVisiblePage(currentPage);
                    if (currentPage && nextPage !== currentPage) {
                        switchPage(nextPage);
                    }
                }
            } catch (e) {
                console.error('[Sidebar] Failed to load config:', e);
            }
        }

        const LAZY_FRAME_PROGRESS_DELAY_MS = 650;

        function prepareLazyFrameLoadingSurface(frame) {
            const shell = frame?.closest('.embedded-frame-shell');
            const status = shell?.querySelector('.embedded-frame-loading');
            if (!status) return null;
            if (!status.dataset.loadingText) {
                status.dataset.loadingText = status.textContent.trim() || '页面仍在准备中…';
            }
            if (status.dataset.hydrated === 'true') return status;

            const skeleton = document.createElement('div');
            skeleton.className = 'embedded-frame-skeleton';
            skeleton.setAttribute('aria-hidden', 'true');

            const bar = document.createElement('div');
            bar.className = 'embedded-frame-skeleton-bar';
            const cards = document.createElement('div');
            cards.className = 'embedded-frame-skeleton-cards';
            for (let index = 0; index < 4; index += 1) {
                const card = document.createElement('div');
                card.className = 'embedded-frame-skeleton-card';
                cards.appendChild(card);
            }
            const panels = document.createElement('div');
            panels.className = 'embedded-frame-skeleton-panels';
            for (let index = 0; index < 2; index += 1) {
                const panel = document.createElement('div');
                panel.className = 'embedded-frame-skeleton-panel';
                panels.appendChild(panel);
            }
            skeleton.append(bar, cards, panels);

            const chip = document.createElement('span');
            chip.className = 'embedded-frame-loading-chip';
            const spinner = document.createElement('span');
            spinner.className = 'embedded-frame-loading-spinner';
            spinner.setAttribute('aria-hidden', 'true');
            const text = document.createElement('span');
            text.className = 'embedded-frame-loading-text';
            chip.append(spinner, text);
            status.replaceChildren(skeleton, chip);
            status.dataset.hydrated = 'true';
            return status;
        }

        function setLazyFrameStatus(frame, message) {
            const status = prepareLazyFrameLoadingSurface(frame);
            const text = status?.querySelector('.embedded-frame-loading-text');
            if (text) text.textContent = message;
        }

        function revealLazyFrame(frame, deferUntilPaint = true) {
            const shell = frame?.closest('.embedded-frame-shell');
            if (!shell || shell.dataset.frameState !== 'loading') return;
            if (frame.__surfaceReadyFallbackTimer) {
                clearTimeout(frame.__surfaceReadyFallbackTimer);
                frame.__surfaceReadyFallbackTimer = null;
            }
            if (frame.__surfaceProgressTimer) {
                clearTimeout(frame.__surfaceProgressTimer);
                frame.__surfaceProgressTimer = null;
            }
            const reveal = () => {
                if (shell.dataset.frameState === 'loading') {
                    shell.dataset.frameHasContent = 'true';
                    shell.dataset.frameState = 'ready';
                    shell.dataset.frameProgress = 'hidden';
                }
            };
            if (deferUntilPaint) requestAnimationFrame(() => requestAnimationFrame(reveal));
            else reveal();
        }

        function markLazyFrameReady(frame) {
            if (!frame) return;
            frame.dataset.surfaceReadyReceived = 'true';
            // 子页主动发出的 ready 表示它的稳定首帧已经完成布局，比 iframe
            // load（可能被图表/CDN/慢接口拖延）更可靠；收到后即可揭示。
            revealLazyFrame(frame, false);
            flushEmbeddedActiveTabFocus(frame);
        }
        window.markLazyFrameReady = markLazyFrameReady;

        const EMBEDDED_TAB_FOCUS_FRAMES = Object.freeze({
            automation: 'automation-frame',
            cluster: 'cluster-frame',
            'devices-console': 'devices-console-frame',
            'redmine-agent': 'redmine-agent-frame',
            'gerrit-dashboard': 'gerrit-dashboard-frame'
        });

        function pageForEmbeddedTabFrame(frame) {
            return Object.keys(EMBEDDED_TAB_FOCUS_FRAMES).find(
                page => document.getElementById(EMBEDDED_TAB_FOCUS_FRAMES[page]) === frame
            ) || '';
        }

        function flushEmbeddedActiveTabFocus(frame) {
            if (frame?.dataset.pendingActiveTabFocus !== 'true') return;
            const pageName = pageForEmbeddedTabFrame(frame);
            if (!pageName || currentPage !== pageName || !frame.contentWindow) {
                delete frame.dataset.pendingActiveTabFocus;
                return;
            }
            delete frame.dataset.pendingActiveTabFocus;
            requestAnimationFrame(() => {
                if (currentPage === pageName && frame.contentWindow) {
                    frame.contentWindow.postMessage(
                        {type: 'embedded-focus-active-tab'}, window.location.origin
                    );
                }
            });
        }

        function focusEmbeddedActiveTab(pageName) {
            const frameId = EMBEDDED_TAB_FOCUS_FRAMES[pageName];
            const frame = frameId && document.getElementById(frameId);
            if (!frame) return;
            frame.dataset.pendingActiveTabFocus = 'true';
            if (frame.dataset.surfaceReadyReceived === 'true'
                    || frame.dataset.documentLoaded === 'true') {
                flushEmbeddedActiveTabFocus(frame);
            }
        }
        window.focusEmbeddedActiveTab = focusEmbeddedActiveTab;

        function focusSidebarNavigationTarget(pageName) {
            if (EMBEDDED_TAB_FOCUS_FRAMES[pageName]) {
                focusEmbeddedActiveTab(pageName);
                return;
            }
            // 非 Tab 页面不能保留上一页 iframe 的焦点；页面容器作为
            // 稳定的键盘导航锚点，不抢占页面内输入框或终端的用户焦点。
            const page = document.getElementById(`page-${pageName}`);
            if (!page) return;
            page.tabIndex = -1;
            page.focus({preventScroll: true});
        }
        window.focusSidebarNavigationTarget = focusSidebarNavigationTarget;

        function bindLazyFrameState(frame) {
            if (!frame || frame.dataset.frameStateBound === 'true') return;
            frame.dataset.frameStateBound = 'true';
            frame.addEventListener('load', () => {
                const shell = frame.closest('.embedded-frame-shell');
                if (!shell) return;
                frame.dataset.documentLoaded = 'true';
                if (frame.dataset.waitForReady === 'true'
                        && frame.dataset.surfaceReadyReceived !== 'true') {
                    // 同源微前端在首批数据已原子渲染后主动通知。
                    // 超时只是容错，避免子页异常时永久遮挡诊断信息。
                    frame.__surfaceReadyFallbackTimer = setTimeout(
                        () => markLazyFrameReady(frame),
                        2500
                    );
                    return;
                }
                // load 事件早于 iframe 内容的首次合成；延后两帧再揭示，
                // 避免用户看到 about:blank 或子页样式尚未绘制的中间帧。
                revealLazyFrame(frame);
            });
            frame.addEventListener('error', () => {
                const shell = frame.closest('.embedded-frame-shell');
                if (!shell) return;
                if (frame.__surfaceReadyFallbackTimer) clearTimeout(frame.__surfaceReadyFallbackTimer);
                if (frame.__surfaceProgressTimer) clearTimeout(frame.__surfaceProgressTimer);
                shell.dataset.frameState = 'error';
                shell.dataset.frameProgress = 'visible';
                setLazyFrameStatus(frame, '页面加载失败，请稍后重试');
            });
        }

        function setLazyFrameSource(frame, source) {
            if (!frame || !source) return;
            bindLazyFrameState(frame);
            const shell = frame.closest('.embedded-frame-shell');
            if (shell) {
                const status = prepareLazyFrameLoadingSurface(frame);
                setLazyFrameStatus(frame, status?.dataset.loadingText || '页面仍在准备中…');
                shell.dataset.frameState = 'loading';
                shell.dataset.frameProgress = 'hidden';
            }
            if (frame.__surfaceReadyFallbackTimer) clearTimeout(frame.__surfaceReadyFallbackTimer);
            if (frame.__surfaceProgressTimer) clearTimeout(frame.__surfaceProgressTimer);
            frame.__surfaceProgressTimer = setTimeout(() => {
                if (shell?.dataset.frameState === 'loading') {
                    shell.dataset.frameProgress = 'visible';
                }
                frame.__surfaceProgressTimer = null;
            }, LAZY_FRAME_PROGRESS_DELAY_MS);
            frame.dataset.documentLoaded = 'false';
            frame.dataset.surfaceReadyReceived = 'false';
            frame.setAttribute('src', source);
        }
        window.setLazyFrameSource = setLazyFrameSource;

        // 页面切换函数：iframe 微前端懒加载
        // 解决 F5 刷新后 iframe 内容丢失的问题
        function ensureLazyFrameLoaded(pageName, forceReload = false) {
            const frameMap = {
                'gms-assistant': 'gms-assistant-frame',
                'automation': 'automation-frame',
                'cluster': 'cluster-frame',
                'devices-console': 'devices-console-frame',
                'redmine-agent': 'redmine-agent-frame',
                'gerrit-dashboard': 'gerrit-dashboard-frame',
                'architecture': 'architecture-iframe'
            };
            const frameId = frameMap[pageName];
            if (!frameId) return;
            const frame = document.getElementById(frameId);
            if (!frame) return;

            const dataSrc = frame.getAttribute('data-src');
            if (!dataSrc) return;

            const hasSrc = frame.hasAttribute('src');

            // 首次加载：iframe 没有 src，从 data-src 加载
            if (!hasSrc) {
                setLazyFrameSource(frame, dataSrc);
                frame.dataset.lazyLoadedAt = String(Date.now());
                return;
            }

            // F5 刷新后：iframe 有 src 但内容可能空白。
            // 只有 forceReload=true 时才强制重新加载（避免页面切换时不必要的重载）。
            // 如果 iframe 在 10 秒内刚被加载过（例如 DOMContentLoaded 提前触发），
            // 跳过重复重载，避免用户看到二次结构骨架。
            if (forceReload) {
                const loadedAt = Number(frame.dataset.lazyLoadedAt || 0);
                if (loadedAt && Date.now() - loadedAt < 10000) return;
                const currentSrc = frame.getAttribute('src');
                if (currentSrc) setLazyFrameSource(frame, currentSrc);
            }
        }

        const pendingAuthPageInitializers = new Set();

        function initializePageSafely(pageName) {
            runPageInitializers(pageName).catch(error => {
                console.error(`[Navigation] Failed to initialize page ${pageName}:`, error);
                if (currentPage === pageName) {
                    showToast(`页面初始化失败：${error?.message || '未知错误'}`, 'error');
                }
            });
        }

        async function runPageInitializers(pageName) {
            if (!state.authReady) {
                if (pendingAuthPageInitializers.has(pageName)) return;
                pendingAuthPageInitializers.add(pageName);
                window.addEventListener(
                    'gms:auth-ready',
                    () => {
                        pendingAuthPageInitializers.delete(pageName);
                        // Do not initialize a page that the user already left;
                        // entering it later will invoke this initializer again.
                        if (currentPage === pageName) initializePageSafely(pageName);
                    },
                    { once: true }
                );
                return;
            }
            if ((pageName === 'desktop' || pageName === 'terminal')
                    && typeof initializeClusterMode === 'function') {
                // Resolve infrastructure + persisted workspace scope before
                // either host workspace is allowed to choose its layout.
                await initializeClusterMode();
            }
            // 页面切换和刷新恢复使用相同初始化入口。
            if (pageName === 'test') {
                if (typeof loadClusterWorkers === 'function') {
                    loadClusterWorkers().catch(error => debugLog('[Cluster] Worker list unavailable:', error));
                }
                // 等待 workspace 上下文就绪后再加载设备，避免先闪现本机设备。
                if (typeof loadDevices === 'function') {
                    (window.GmsWorkspace?.ready || Promise.resolve())
                        .then(() => loadDevices(false))
                        .catch(() => {});
                }
                if (typeof loadTestSuites === 'function') loadTestSuites(false).catch(() => {});
                if (typeof checkInitialTestStatus === 'function') checkInitialTestStatus().catch(() => {});
            }
            if (pageName === 'terminal') {
                if (!await ensureTerminalElevation()) return;
                // Load local xterm assets while fetching the host directory so
                // the first terminal pane can open immediately afterwards. A
                // local target does not depend on the cluster round trip, so
                // let that directory refresh finish behind the visible mount.
                const terminalContext = window.GmsWorkspace?.get?.() || {};
                const needsRemoteHost = terminalContext.scope_mode === 'cluster'
                    && !isLocalWorkspaceWorker(
                        terminalContext.worker_id || workspaceLocalWorkerId()
                    );
                const terminalHostsReady = loadTerminalClusterHosts().catch(error =>
                    debugLog('[Terminal] Worker host directory unavailable:', error));
                const terminalAssetsReady = loadXTermScripts().catch(error => {
                    debugLog('[Terminal] xterm assets unavailable:', error);
                    throw error;
                });
                if (needsRemoteHost) {
                    await Promise.all([terminalHostsReady, terminalAssetsReady]);
                } else {
                    await terminalAssetsReady;
                    terminalHostsReady.catch(() => {});
                }
                const pendingCommand = sessionStorage.getItem('pending_terminal_command');
                const commandSource = sessionStorage.getItem('command_source');

                // 设备 ADB shell 走 workspace pane 的 pendingAdbTarget 路径，
                // 这里只剩 SSH 终端与路由命令两种入口。
                if (!pendingCommand) {
                    await ensureTerminalWorkspaceInitialized();
                } else {
                    if (terminalInitialized) {
                        debugLog('Forcing terminal re-initialization');
                        isReconnecting = true;
                        if (terminalSocket) {
                            terminalSocket.close();
                            terminalSocket = null;
                        }
                        if (terminal) {
                            terminal.dispose();
                            terminal = null;
                        }
                        terminalInitialized = false;
                        setTimeout(() => {
                            isReconnecting = false;
                        }, 100);
                    }

                    if (commandSource === 'route_check') {
                        updateSilentMode(false, 'route', pendingCommand);
                        sessionStorage.removeItem('pending_terminal_command');
                        sessionStorage.removeItem('command_source');
                        debugLog('Route command mode enabled, command:', pendingCommand);
                    }

                    initTerminal();
                }
            }

            if (pageName === 'users') {
                if (window.agentAccessPanelIsOpen?.()) {
                    stopUsersAutoRefresh();
                    agentAccessReload();
                } else {
                    loadUsersList();
                    startUsersAutoRefresh();
                }
            } else {
                stopUsersAutoRefresh();
            }

            if (pageName === 'security-audit') {
                if (!securityAuditState.loaded) loadSecurityAudit(true);
            }
            if (pageName === 'devices') {
                loadDevicesManagement();
            } else {
                stopDevicesAutoRefresh();
            }
            if (pageName === 'reports') {
                loadTestReports(typeof currentUserFilter !== 'undefined' ? currentUserFilter : false);
            } else if (typeof cleanupReportsPolling === 'function') {
                cleanupReportsPolling();
            }
            if (pageName === 'api-docs') {
                loadApiDocs();
            }
            if (pageName === 'test-suites') {
                await initTestSuiteBrowserPage();
            }
            if (pageName === 'apk-analysis') {
                setTimeout(() => {
                    if (typeof window.initApkAnalysisPage === 'function') {
                        window.initApkAnalysisPage();
                    }
                }, 50);
            }
            if (pageName === 'terminal' && terminalInitialized && terminal && sessionStorage.getItem('pending_terminal_command')) {
                setTimeout(() => {
                    terminal.focus();
                }, 100);
            }
            if (pageName === 'desktop') {
                if (!await ensureTerminalElevation(false, '打开主机桌面', '主机桌面')) return;
                await ensureDesktopInitialized();
            }
            if (pageName === 'websites') {
                loadToolsList();
            }
            if (pageName === 'tools') {
                ut_loadToolsList();
            }
            if (pageName === 'agent' && typeof initAgentPage === 'function') {
                initAgentPage();
            }
            if (pageName === 'notes' && typeof initNotesPage === 'function') {
                initNotesPage();
            }
        }

        function switchPage(pageName, event, options = {}) {
            if (event) {
                event.preventDefault();
            }
            pageName = resolveVisiblePage(pageName);
            // 页面切换时保留 VNC iframe 和终端 WebSocket，避免重复连接。
            const pageEl = document.getElementById(`page-${pageName}`);
            if (!pageEl) return;
            const initStyle = document.getElementById('page-init-style');
            if (initStyle) {
                initStyle.remove();
            }

            // 更新导航栏高亮
            document.querySelectorAll('.sidebar-item').forEach(item => {
                item.classList.remove('active');
            });
            const activeItem = document.querySelector(`[data-page="${pageName}"]`);
            if (activeItem) {
                activeItem.classList.add('active');
            }

            // 隐藏所有页面
            document.querySelectorAll('.page-content.active').forEach(el => el.classList.remove('active'));

            // 显示目标页面
            pageEl.classList.add('active');
            ensureLazyFrameLoaded(pageName);
            window.GmsWorkspace?.setActivePage(pageName);
            window.GmsWorkspace?.update({origin_page: pageName}, {source: 'navigation'});
            window.GmsWorkspace?.postToFrame(pageName);
            if (document.readyState !== 'complete') {
                window.__shellPageSwitchedBeforeLoad = true;
            }

            // 清除DOM缓存，避免保留已隐藏页面的元素引用
            if (typeof clearDomCache === 'function') clearDomCache();

            currentPage = pageName;

            // 保存当前页面。hash 参与刷新恢复，localStorage/cookie 用于首次无 hash 的回退。
            saveCurrentPageState(pageName, { updateHash: options.updateHash !== false });

            // 动态更新页面标题
            document.title = PAGE_TITLES[pageName] || 'GMS远程测试';
            if (typeof recordSecurityPageView === 'function') {
                recordSecurityPageView(pageName);
            }

            initializePageSafely(pageName);
        }

        // 将 switchPage 导出到全局作用域，供外部脚本调用
        window.switchPage = switchPage;



