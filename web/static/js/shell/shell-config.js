/**
 * shell-config —— 解析 gms-runtime-config JSON 数据标签为全局常量。
 *
 * 运行时配置（外部服务/品牌/cluster worker ID）由模板以
 * `<script type="application/json" id="gms-runtime-config">` 注入：
 * 数据标签不被浏览器执行，不受 CSP script-src 管制，且杜绝 tojson
 * 内容逃逸出 JS 上下文的注入面。本模块排在 defer 队列首位，保证
 * icon-auto.js 等模块的顶层常量读取时数据已就绪，同时不阻塞 HTML 解析。
 */
(function () {
    'use strict';

    var tag = document.getElementById('gms-runtime-config');
    var config = {};
    if (tag) {
        try {
            config = JSON.parse(tag.textContent) || {};
        } catch (e) {
            console.error('[shell-config] runtime config JSON parse failed:', e);
            config = {};
        }
    }

    window.EXTERNAL_SERVICES = config.external_services || {};
    window.PRODUCT_BRANDING = config.product_branding || {};
    window.__GMS_BOOTSTRAP__ = {
        localWorkerId: config.local_worker_id || '',
        ubuntu_host: config.ubuntu_host || '',
        ubuntu_user: config.ubuntu_user || '',
    };
})();
