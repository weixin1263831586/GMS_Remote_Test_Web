    // 初始化Emoji选择器
    document.addEventListener('DOMContentLoaded', function() {
        const picker = document.getElementById('emoji-picker');
        if (picker && typeof commonEmojis !== 'undefined') {
            picker.innerHTML = commonEmojis.map(emoji =>
                `<button data-click="selectEmoji" data-a0="${emoji}" class="emoji-opt">${emoji}</button>`
            ).join('');
        }
        // 外部知识库（Android Internals Wiki）管理面板：仅 notes 页存在时
        // 按需加载（面板自身动态注入 DOM，shell.html 不携带静态标记）。
        if (document.getElementById('page-notes')) {
            const script = document.createElement('script');
            script.src = '/static/js/shell/external-knowledge.js?v=20260921-ek';
            document.head.appendChild(script);
        }
    });

    // Listen for embedded dashboard notifications from iframes
    window.addEventListener('message', function(e) {
        if (e.origin !== window.location.origin) return;
        const allowedTypes = new Set([
            'redmine-agent-notification',
            'gms-dashboard-notification',
            'gms-update-monitor-notification',
            'automation-notification',
            'cluster-notification'
        ]);
        const fromEmbeddedFrame = Array.from(document.querySelectorAll('iframe'))
            .some(frame => frame.contentWindow === e.source);
        if (fromEmbeddedFrame && e.data && allowedTypes.has(e.data.type)) {
            if (typeof notifyOperationResult === 'function') {
                notifyOperationResult(e.data.title, e.data.message, e.data.level || 'info');
            }
        }
    });
