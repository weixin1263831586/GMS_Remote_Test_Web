// Shared browser utilities used by page-level scripts.

function debounce(func, wait) {
    let timer = null;
    return function(...args) {
        clearTimeout(timer);
        timer = setTimeout(() => func.apply(this, args), wait);
    };
}

function throttle(func, limit) {
    let inThrottle = false;
    return function(...args) {
        if (!inThrottle) {
            func.apply(this, args);
            inThrottle = true;
            setTimeout(() => {
                inThrottle = false;
            }, limit);
        }
    };
}

function normalizeApiTextError(text) {
    const message = String(text || '').trim();
    const lower = message.toLowerCase();
    if (lower.startsWith('<!doctype html') || lower.startsWith('<html')) {
        return '服务器返回了 HTML 错误页，请稍后重试或查看服务端日志';
    }
    return message;
}

const HTML_ENTITIES = Object.freeze({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;'
});

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    return String(text).replace(/[&<>"']/g, char => HTML_ENTITIES[char]);
}

// 数据区加载占位：超新星动画（.loading-orbit 舞台，样式见 loading-orbit.css）
// + 说明文字。redmine/gerrit 看板等慢接口的首屏占位复用同一实现。
// stage=true 启用视口锚定舞台（.loading-orbit--stage，fixed 居中），
// 用于整页 Tab 主内容区：页头/工具栏行显隐增高不再牵动动画位置。
function loadingOrbitHtml(text, stage) {
    return '<div class="loading-orbit' + (stage ? ' loading-orbit--stage' : '') + '" role="status" aria-label="' + escapeHtml(text) + '">'
        + '<div class="loading-orbit-bar" aria-hidden="true"></div>'
        + '<div class="loading-orbit-cards" aria-hidden="true">'
        + '<div class="loading-orbit-card"></div><div class="loading-orbit-card"></div>'
        + '<div class="loading-orbit-card"></div><div class="loading-orbit-card"></div></div>'
        + '<div class="loading-orbit-panels" aria-hidden="true">'
        + '<div class="loading-orbit-panel"></div><div class="loading-orbit-panel"></div></div>'
        + '<span class="loading-orbit-text">' + escapeHtml(text) + '</span></div>';
}

// 幂等写入：容器已渲染动画时只改文案、不重建 DOM——重复加载（如 tab
// 初始化 + force 刷新）不会让动画从头重播或移位；返回是否新注入。
function setLoadingOrbit(box, text, stage) {
    if (!box) return false;
    const existing = box.querySelector(':scope > .loading-orbit');
    if (existing) {
        existing.classList.toggle('loading-orbit--stage', Boolean(stage));
        existing.setAttribute('aria-label', text);
        const label = existing.querySelector('.loading-orbit-text');
        if (label) label.textContent = text;
        return false;
    }
    box.innerHTML = loadingOrbitHtml(text, stage);
    return true;
}

function safeHeaderPercentEncode(value) {
    const text = String(value ?? '');
    try {
        return encodeURIComponent(text);
    } catch (error) {
        // 编码前移除无效代理字符，确保 Header 为 ASCII。
        return encodeURIComponent(text.replace(/[\uD800-\uDFFF]/g, ''));
    }
}

// 密码输入框明文/密文切换：按钮位于包裹层 .password-input-wrap 内，
// 点击后在 password 与 text 之间切换，便于校验已输入的密码。
// 图标由 CSS 伪元素按 is-visible 类切换眼睛/闭眼 mask（form-controls.css）。
function togglePasswordVisibility(button) {
    const input = button.closest('.password-input-wrap')?.querySelector('input');
    if (!input) return;
    const showPlain = input.type === 'password';
    input.type = showPlain ? 'text' : 'password';
    button.classList.toggle('is-visible', showPlain);
    const label = showPlain ? '隐藏密码' : '显示密码';
    button.setAttribute('aria-label', label);
    button.setAttribute('title', label);
}

function formatBytes(bytes, hideIfZero = false) {
    if (hideIfZero && (!bytes || bytes === '0')) return '';
    const numBytes = parseInt(bytes) || 0;
    if (numBytes === 0) return '0 B';

    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(numBytes) / Math.log(k));
    return parseFloat((numBytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function triggerDownload(url, filename, isBlobUrl = false) {
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();

    if (isBlobUrl) {
        setTimeout(() => {
            document.body.removeChild(link);
            window.URL.revokeObjectURL(url);
        }, 100);
    } else {
        document.body.removeChild(link);
    }
}

window.debounce = debounce;
window.throttle = throttle;
window.normalizeApiTextError = normalizeApiTextError;
window.escapeHtml = escapeHtml;
window.safeHeaderPercentEncode = safeHeaderPercentEncode;
window.togglePasswordVisibility = togglePasswordVisibility;
window.formatBytes = formatBytes;
window.triggerDownload = triggerDownload;
