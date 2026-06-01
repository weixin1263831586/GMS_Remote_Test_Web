// Shared browser utilities used by app.js and page-level scripts.

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

function safeHeaderPercentEncode(value) {
    const text = String(value ?? '');
    try {
        return encodeURIComponent(text);
    } catch (error) {
        // Drop invalid surrogate code units before encoding; header values must stay ASCII.
        return encodeURIComponent(text.replace(/[\uD800-\uDFFF]/g, ''));
    }
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
window.formatBytes = formatBytes;
window.triggerDownload = triggerDownload;
