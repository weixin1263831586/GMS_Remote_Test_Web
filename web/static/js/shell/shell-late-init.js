    // 初始化Emoji选择器
    document.addEventListener('DOMContentLoaded', function() {
        const picker = document.getElementById('emoji-picker');
        if (picker && typeof commonEmojis !== 'undefined') {
            picker.innerHTML = commonEmojis.map(emoji =>
                `<button data-click="selectEmoji" data-a0="${emoji}" class="emoji-opt">${emoji}</button>`
            ).join('');
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
                // Preserve the category and structured identity when an
                // embedded page forwards a notification to the shell. The
                // previous three-argument call discarded navigation anchors.
                const categories = {
                    'redmine-agent-notification': 'redmine',
                    'automation-notification': 'automation',
                    'cluster-notification': 'cluster',
                    'gms-update-monitor-notification': 'system',
                    'gms-dashboard-notification': 'system',
                };
                const details = e.data.data && typeof e.data.data === 'object'
                    && !Array.isArray(e.data.data) ? e.data.data : {};
                notifyOperationResult(
                    e.data.title, e.data.message, e.data.level || 'info',
                    e.data.category || categories[e.data.type] || 'system',
                    details
                );
            }
        }
    });
