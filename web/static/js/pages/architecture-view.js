/**
 * architecture-view —— 系统架构页多视图切换（平台总览 / USB/IP / AOA / 固件烧写 / 测试闭环 / Agent 诊断）。
 *
 * 全站页面 CSP 为 script-src 'self'（bootstrap/application.py 的
 * _STRICT_CSP），架构页脚本必须外置为静态文件加载，不允许 inline <script>。
 * 交互约定与 ATS 页 workflow-tab 一致：tab 行 + active 底线、aria tab 语义、
 * ?arch= URL 参数与 sessionStorage 记忆当前视图。
 */
(function () {
    'use strict';
    const STORAGE_KEY = 'gms_architecture_view';
    const tabs = Array.from(document.querySelectorAll('.arch-tab'));
    const panes = Array.from(document.querySelectorAll('[data-arch-pane]'));

    function showArch(name, persist) {
        if (!panes.some((pane) => pane.dataset.archPane === name)) name = 'platform';
        tabs.forEach((tab) => {
            const active = tab.dataset.arch === name;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
            tab.tabIndex = active ? 0 : -1;
        });
        panes.forEach((pane) => { pane.hidden = pane.dataset.archPane !== name; });
        if (persist === false) return;
        try { window.sessionStorage.setItem(STORAGE_KEY, name); } catch (_error) {}
        try {
            const url = new URL(window.location.href);
            if (name === 'platform') url.searchParams.delete('arch');
            else url.searchParams.set('arch', name);
            window.history.replaceState({}, '', url.toString());
        } catch (_error) {}
    }

    tabs.forEach((tab) => {
        tab.addEventListener('click', () => showArch(tab.dataset.arch));
    });

    // ←/→ 在视图间循环切换（与其他页面的方向键页面切换习惯一致）；
    // 焦点在表单控件或可编辑区域时让位，不劫持文本编辑光标移动。
    document.addEventListener('keydown', (event) => {
        if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        const target = event.target;
        if (target && (['input', 'textarea', 'select'].includes(String(target.tagName).toLowerCase()) || target.isContentEditable)) return;
        const order = tabs.map((tab) => tab.dataset.arch);
        const current = tabs.find((tab) => tab.classList.contains('active'));
        const index = order.indexOf(current ? current.dataset.arch : 'platform');
        const delta = event.key === 'ArrowRight' ? 1 : -1;
        const next = order[(index + delta + order.length) % order.length];
        showArch(next);
        const activeTab = tabs.find((tab) => tab.dataset.arch === next);
        if (activeTab) activeTab.focus({ preventScroll: true });
        event.preventDefault();
    });

    // 跨 iframe 键盘协作（与 embedded-workspace.js 同一约定）：
    // 1) 父页 ↑/↓ 切到本页后投递 embedded-focus-active-tab → 聚焦当前
    //    页签，←/→ 立即可用，无需先用鼠标点击 iframe；
    // 2) 页签持有焦点时的 ↑/↓ 转发回父页侧栏导航，继续页面间移动；
    //    子页输入框内的方向键不受影响。
    const post = (type, detail) => window.parent.postMessage({ type, ...detail }, window.location.origin);
    window.addEventListener('message', (event) => {
        if (event.origin !== window.location.origin || event.source !== window.parent) return;
        if (event.data?.type === 'embedded-focus-active-tab') {
            document.querySelector('[role="tab"][aria-selected="true"]:not([disabled])')
                ?.focus({ preventScroll: true });
        }
    });
    document.addEventListener('keydown', (event) => {
        if (!['ArrowUp', 'ArrowDown'].includes(event.key)) return;
        const target = event.target;
        if (!(target instanceof Element) || target.getAttribute('role') !== 'tab') return;
        event.preventDefault();
        post('embedded-sidebar-arrow', { key: event.key });
    }, true);

    let initial = 'platform';
    try {
        initial = new URLSearchParams(window.location.search).get('arch')
            || window.sessionStorage.getItem(STORAGE_KEY)
            || 'platform';
    } catch (_error) {}
    showArch(initial, false);
})();
