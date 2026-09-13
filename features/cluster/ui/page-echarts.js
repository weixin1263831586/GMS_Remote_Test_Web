/* ECharts 仅从本地 vendor 加载，避免集群页面向第三方 CDN 发请求。 */
(function(){window.dashEchartsStatus='loading';var mark=function(status){window.dashEchartsStatus=status;window.renderDashboard&&window.renderDashboard()};var s=document.createElement('script');s.src='/static/vendor/echarts.min.js?v=5.5.0';s.onload=function(){mark('ready')};s.onerror=function(){mark('failed')};document.head.appendChild(s);})();
