    // 初始化Emoji选择器
    document.addEventListener('DOMContentLoaded', function() {
        const picker = document.getElementById('emoji-picker');
        if (picker && typeof commonEmojis !== 'undefined') {
            picker.innerHTML = commonEmojis.map(emoji =>
                `<button data-click="selectEmoji" data-a0="${emoji}" class="emoji-opt">${emoji}</button>`
            ).join('');
        }
    });

    // OpenGrok 源码分析弹框。

    function closeSourceAnalysisModal() {
        ModalManager.close('source-analysis-modal');
    }

    function searchSourceCode() {
        const queryEl = document.getElementById('opengrok-query');
        const fieldEl = document.getElementById('opengrok-search-field');
        const projectEl = document.getElementById('opengrok-project');
        const typeEl = document.getElementById('opengrok-type');
        const resultsDiv = document.getElementById('opengrok-results');
        const resultsList = document.getElementById('opengrok-results-list');

        const query = (queryEl ? queryEl.value : '').trim();
        if (!query) {
            showToast('请输入搜索关键词', 'warning');
            return;
        }

        if (!OPENGROK_CONFIG || !OPENGROK_CONFIG.isValid) {
            showToast('OpenGrok 未配置，请在配置中设置 OpenGrok 地址', 'error');
            return;
        }

        const field = fieldEl ? fieldEl.value : 'smart';
        const project = projectEl ? projectEl.value : OPENGROK_CONFIG._defaultProject || '';
        const type = typeEl ? typeEl.value : '';

        // Build OpenGrok search URL
        let searchUrl = `${OPENGROK_CONFIG._baseUrl}/search?`;
        const params = [];
        if (field === 'full') {
            params.push(`q=${encodeURIComponent(query)}`);
        } else if (field === 'def') {
            params.push(`defs=${encodeURIComponent(query)}`);
        } else if (field === 'symbol') {
            params.push(`refs=${encodeURIComponent(query)}`);
        } else if (field === 'path') {
            params.push(`path=${encodeURIComponent(query)}`);
        } else {
            // smart: search all fields
            params.push(`q=${encodeURIComponent(query)}`);
        }
        if (project) params.push(`project=${encodeURIComponent(project)}`);
        if (type) params.push(`type=${encodeURIComponent(type)}`);
        searchUrl += params.join('&');

        // Show results area with loading state
        if (resultsDiv) resultsDiv.style.display = 'block';
        if (resultsList) resultsList.innerHTML = '<div style="text-align: center; padding: 20px; color: var(--text-secondary);">搜索中...</div>';

        // Use the OpenGrok API endpoint
        fetch(`/api/opengrok/search`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query, full: field === 'full', project, type })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success && data.results && data.results.length > 0) {
                if (resultsList) {
                    resultsList.innerHTML = data.results.map(item => {
                        const fileUrl = buildOpenGrokUrl(item.file || item.path, item.line);
                        return `<div style="padding: 8px; border-bottom: 1px solid var(--border-color);">
                            <div style="font-weight: 600; font-size: 12px;">
                                <a href="${fileUrl}" target="_blank" style="color: var(--link-color, #667eea); text-decoration: none;">${item.file || item.path || '未知文件'}</a>
                                ${item.line ? `<span style="color: var(--text-secondary); font-weight: normal;">:${item.line}</span>` : ''}
                            </div>
                            ${item.summary || item.line_text ? `<div style="font-size: 11px; color: var(--text-secondary); margin-top: 2px;">${escapeHtml(item.summary || item.line_text || '')}</div>` : ''}
                        </div>`;
                    }).join('');
                }
            } else {
                if (resultsList) resultsList.innerHTML = '<div style="text-align: center; padding: 20px; color: var(--text-secondary);">无搜索结果</div>';
            }
        })
        .catch(err => {
            console.error('[OpenGrok] Search failed:', err);
            if (resultsList) resultsList.innerHTML = '<div style="text-align: center; padding: 20px; color: var(--error-color, #e74c3c);">搜索失败: ' + escapeHtml(err.message) + '</div>';
        });
    }

    function openOpenGrokLink() {
        const queryEl = document.getElementById('opengrok-query');
        const fieldEl = document.getElementById('opengrok-search-field');
        const projectEl = document.getElementById('opengrok-project');
        const typeEl = document.getElementById('opengrok-type');

        const query = (queryEl ? queryEl.value : '').trim();
        if (!query || !OPENGROK_CONFIG || !OPENGROK_CONFIG.isValid) {
            showToast('请先输入搜索关键词', 'warning');
            return;
        }

        const field = fieldEl ? fieldEl.value : 'smart';
        const project = projectEl ? projectEl.value : OPENGROK_CONFIG._defaultProject || '';
        const type = typeEl ? typeEl.value : '';

        let url = `${OPENGROK_CONFIG._baseUrl}/search?`;
        const params = [];
        if (field === 'full') params.push(`q=${encodeURIComponent(query)}`);
        else if (field === 'def') params.push(`defs=${encodeURIComponent(query)}`);
        else if (field === 'symbol') params.push(`refs=${encodeURIComponent(query)}`);
        else if (field === 'path') params.push(`path=${encodeURIComponent(query)}`);
        else params.push(`q=${encodeURIComponent(query)}`);
        if (project) params.push(`project=${encodeURIComponent(project)}`);
        if (type) params.push(`type=${encodeURIComponent(type)}`);
        url += params.join('&');

        window.open(url, '_blank');
    }

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
