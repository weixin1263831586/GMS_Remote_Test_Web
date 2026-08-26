// Shell 模块：通用 UI 反馈（连接状态、确认框、Toast、右下角通知，从 navigation.js 拆分）。
// 依赖 state.js 的 debugLog 与 shell/logging.js 的 addLogEntry；均为全局函数。

// ==================== UI Helpers ====================
function updateConnectionStatus(connected) {
    state.connected = connected;
}

// 统一确认对话框
function showConfirmDialog(title, message, onConfirm, onCancel) {
    return new Promise((resolve) => {
        const modal = document.getElementById('confirm-modal');
        const titleEl = document.getElementById('confirm-title');
        const messageEl = document.getElementById('confirm-message');
        const okBtn = document.getElementById('confirm-ok-btn');
        const cancelBtn = document.getElementById('confirm-cancel-btn');

        // 设置标题和消息
        titleEl.textContent = title;
        messageEl.textContent = message;

        let settled = false;

        // 确定按钮事件
        const handleOk = () => {
            if (settled) return;
            settled = true;
            ModalManager.close('confirm-modal');
            cleanup();
            resolve(true);
            if (onConfirm) onConfirm();
        };

        // 取消按钮事件
        const handleCancel = () => {
            if (settled) return;
            settled = true;
            ModalManager.close('confirm-modal');
            cleanup();
            resolve(false);
            if (onCancel) onCancel();
        };

        // 清理事件监听器
        const cleanup = () => {
            okBtn.removeEventListener('click', handleOk);
            cancelBtn.removeEventListener('click', handleCancel);
        };

        // 绑定事件
        okBtn.addEventListener('click', handleOk);
        cancelBtn.addEventListener('click', handleCancel);
        ModalManager.onClose('confirm-modal', () => {
            if (settled) return;
            settled = true;
            cleanup();
            resolve(false);
            if (onCancel) onCancel();
        });

        // 显示模态框
        ModalManager.open('confirm-modal');
    });
}

let _toastToken = 0;
function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    if (!toast) return;
    // 版本号守卫：只有最新一次调用的定时器才能隐藏 Toast，
    // 避免连续调用时旧定时器提前关掉新 Toast。
    const token = ++_toastToken;
    toast.textContent = message;
    toast.className = `toast ${type} show`;

    // 根据消息类型自动调整显示时间
    const durationMap = {
        'success': 2000,  // 成功消息：2秒
        'info': 2500,     // 普通信息：2.5秒
        'warning': 3500,  // 警告消息：3.5秒
        'error': 5000     // 错误消息：5秒（需要更多时间阅读）
    };

    const duration = durationMap[type] || 3000;

    setTimeout(() => {
        if (token === _toastToken) {
            toast.className = `toast ${type}`;
        }
    }, duration);
}

// ==================== Snackbar 右下角通知 ====================

// 暴露到全局作用域，确保模板中的函数可以调用
window.showSnackbar = function showSnackbar(title, message, level = 'info', duration = 5000) {
    debugLog('[showSnackbar] 被调用:', { title, message, level });

    const container = document.getElementById('snackbar-container');
    debugLog('[showSnackbar] container:', container);

    if (!container) {
        console.error('[Snackbar] Container not found! 无法显示通知');
        return;
    }

    const icons = {
        'success': '✅',
        'error': '❌',
        'warning': '⚠️',
        'info': '📢'
    };

    const snackbar = document.createElement('div');
    snackbar.className = `snackbar ${level}`;
    snackbar.innerHTML = `
        <span class="snackbar-icon">${icons[level] || icons.info}</span>
        <div class="snackbar-content">
            <div class="snackbar-title">${escapeHtml(title)}</div>
            <div class="snackbar-message">${escapeHtml(message || '')}</div>
        </div>
        <button class="snackbar-close" onclick="this.parentElement.remove()">×</button>
    `;

    debugLog('[showSnackbar] 创建 snackbar 元素:', snackbar);
    container.appendChild(snackbar);
    debugLog('[showSnackbar] 已添加到容器');

    // 自动关闭
    setTimeout(() => {
        if (snackbar.parentElement) {
            snackbar.classList.add('snackbar-exit');
            setTimeout(() => {
                if (snackbar.parentElement) {
                    snackbar.remove();
                    debugLog('[showSnackbar] 已移除 snackbar');
                }
            }, 300);
        }
    }, duration);
};

// 点击弹框外部时关闭，并复用弹框映射。
// 各 close* 函数定义在 pages/*.js（本模块之后加载），必须惰性包装；
// 加载期直接传函数引用会在脚本求值时抛 ReferenceError。
const _modalCloseHandlers = {
    'config-modal': () => closeModal(),
    'firmware-modal': () => closeFirmwareModal(),
    'firmware-share-modal': () => closeFirmwareShareModal(),
    'firmware-share-password-modal': () => closeFirmwareSharePasswordModal(),
    'file-browser-modal': () => closeFileBrowserModal(),
    'gsi-modal': () => closeGsiModal(),
    'sn-modal': () => closeSnModal(),
    'ui-control-modal': () => closeUiControl()
};

document.addEventListener('click', function(event) {
    const target = event.target;
    if (target.classList && target.classList.contains('modal') && _modalCloseHandlers[target.id]) {
        _modalCloseHandlers[target.id]();
    }
});


