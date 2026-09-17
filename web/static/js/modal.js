// Shared modal helpers and Escape-key modal lifecycle management.

// 可聚焦元素选择器：focus trap 与初始聚焦共用同一契约。
const MODAL_FOCUSABLE_SELECTOR = [
    '[autofocus]',
    'input:not([disabled])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    'button:not([disabled])',
    '[href]',
    '[tabindex]:not([tabindex="-1"])'
].join(', ');

function showModalError(modal, message) {
    modal.querySelector('.modal-title').textContent = '❌ 分析失败';
    modal.querySelector('.modal-body').textContent = message;
    modal.querySelector('.modal-body').style.cssText = 'color: var(--danger-color); padding: 20px; text-align: center;';
}

function createAnalysisModal(type, title, loadingMessage) {
    const modalId = `${type}-modal-${Date.now()}`;
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.innerHTML = `
        <div class="modal-content" style="max-width: 900px; max-height: min(90vh, calc(100dvh - 16px));">
            <div class="modal-header">
                <span class="modal-title"></span>
                <button type="button" class="modal-close" aria-label="关闭">&times;</button>
            </div>
            <div class="modal-body">
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; margin-bottom: 20px;">🔍</div>
                    <div class="modal-loading-message" style="color: var(--text-secondary); margin-bottom: 12px;"></div>
                </div>
            </div>
        </div>
    `;

    modal.querySelector('.modal-title').textContent = String(title ?? '');
    modal.querySelector('.modal-loading-message').textContent = String(loadingMessage ?? '');
    const closeButton = modal.querySelector('.modal-close');
    closeButton.addEventListener('click', () => ModalManager.close(modalId));
    closeButton.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            ModalManager.close(modalId);
        }
    });

    document.body.appendChild(modal);
    ModalManager.open(modalId);

    return { modal, modalId };
}

const ModalManager = {
    _escListener: null,
    _trapListener: null,
    _activeModals: [],
    _dynamicModals: new Set(),
    _closeHandlers: new Map(),
    _originalZIndexes: new Map(),
    _focusOrigins: new Map(),
    _inertedRoots: new Set(),
    _baseZIndex: 12000,
    _stackStep: 20,

    open(modalId) {
        const modal = document.getElementById(modalId);
        if (!modal) {
            return;
        }

        if (!this._originalZIndexes.has(modalId)) {
            this._originalZIndexes.set(modalId, modal.style.zIndex || '');
        }
        if (!this._focusOrigins.has(modalId)) {
            this._focusOrigins.set(modalId, document.activeElement);
        }

        if (modal.classList.contains('modal')) {
            modal.style.display = 'flex';
        }
        modal.classList.add('show');
        modal.setAttribute('role', modal.getAttribute('role') || 'dialog');
        modal.setAttribute('aria-hidden', 'false');
        this._addActiveModal(modalId);
        this._syncModalStack();
        this._ensureEscListener();
        window.requestAnimationFrame(() => this._focusModal(modalId));
    },

    close(modalId) {
        if (this._dynamicModals.has(modalId)) {
            this.unregisterDynamic(modalId);
            return;
        }
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.classList.remove('show');
            if (modal.classList.contains('modal')) {
                modal.style.display = 'none';
            }
            modal.setAttribute('aria-hidden', 'true');
            modal.removeAttribute('aria-modal');
            modal.inert = false;
            this._removeActiveModal(modalId);
            this._restoreZIndex(modalId, modal);
            this._emitClose(modalId);
            this._syncModalStack();
            this._restoreFocus(modalId);
            this._cleanupEscListener();
        }
    },

    closeAll() {
        [...this._activeModals].reverse().forEach(modalId => this.close(modalId));
        document.querySelectorAll('.modal.show').forEach(modal => {
            modal.classList.remove('show');
            modal.style.display = 'none';
            modal.setAttribute('aria-hidden', 'true');
            modal.removeAttribute('aria-modal');
            modal.inert = false;
        });
        this._activeModals = [];
        this._syncModalStack();
        this._cleanupEscListener();
    },

    closeTopmost() {
        this._syncModalStack();
        const modalId = this._activeModals[this._activeModals.length - 1];
        if (modalId) {
            this.close(modalId);
        }
    },

    toggle(modalId) {
        this.isOpen(modalId) ? this.close(modalId) : this.open(modalId);
    },

    isOpen(modalId) {
        const modal = document.getElementById(modalId);
        return modal ? modal.classList.contains('show') : false;
    },

    registerDynamic(modalElement) {
        if (!modalElement.id) {
            throw new Error('Dynamic modal must have an id');
        }
        document.body.appendChild(modalElement);
        this._dynamicModals.add(modalElement.id);
        this.open(modalElement.id);
        return modalElement;
    },

    unregisterDynamic(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            this._restoreZIndex(modalId, modal);
            modal.remove();
        }
        this._dynamicModals.delete(modalId);
        this._removeActiveModal(modalId);
        this._emitClose(modalId);
        this._syncModalStack();
        this._restoreFocus(modalId);
        this._cleanupEscListener();
    },

    onClose(modalId, handler) {
        if (typeof handler === 'function') {
            this._closeHandlers.set(modalId, handler);
        }
    },

    _addActiveModal(modalId) {
        const existingIndex = this._activeModals.indexOf(modalId);
        if (existingIndex !== -1) {
            this._activeModals.splice(existingIndex, 1);
        }
        this._activeModals.push(modalId);
    },

    _removeActiveModal(modalId) {
        const idx = this._activeModals.indexOf(modalId);
        if (idx !== -1) {
            this._activeModals.splice(idx, 1);
        }
        if (this._activeModals.length === 0) {
            this._cleanupEscListener();
        }
    },

    _ensureEscListener() {
        if (!this._escListener) {
            this._escListener = (event) => {
                if (event.key === 'Escape' && this._activeModals.length > 0) {
                    const topModalId = this._activeModals[this._activeModals.length - 1];
                    event.preventDefault();
                    event.stopPropagation();
                    this.close(topModalId);
                }
            };
            document.addEventListener('keydown', this._escListener);
        }
        if (!this._trapListener) {
            // Focus trap：Tab / Shift+Tab 只在栈顶 modal 内循环，
            // 焦点不得落到被 inert 的背景页面（删除/烧写/停止任务等
            // 破坏性操作所在的页面尤其重要）。
            this._trapListener = (event) => {
                if (event.key !== 'Tab' || this._activeModals.length === 0) {
                    return;
                }
                const topModalId = this._activeModals[this._activeModals.length - 1];
                const modal = document.getElementById(topModalId);
                if (!modal) {
                    return;
                }
                const focusables = Array.from(
                    modal.querySelectorAll(MODAL_FOCUSABLE_SELECTOR)
                ).filter(el => !el.disabled && el.getAttribute('tabindex') !== '-1');
                if (focusables.length === 0) {
                    event.preventDefault();
                    modal.focus({ preventScroll: true });
                    return;
                }
                const active = document.activeElement;
                const inside = modal.contains(active);
                const first = focusables[0];
                const last = focusables[focusables.length - 1];
                if (event.shiftKey) {
                    if (!inside || active === first) {
                        event.preventDefault();
                        last.focus({ preventScroll: true });
                    }
                } else if (!inside || active === last) {
                    event.preventDefault();
                    first.focus({ preventScroll: true });
                }
            };
            document.addEventListener('keydown', this._trapListener);
        }
    },

    _cleanupEscListener() {
        if (this._escListener && this._activeModals.length === 0) {
            document.removeEventListener('keydown', this._escListener);
            this._escListener = null;
        }
        if (this._trapListener && this._activeModals.length === 0) {
            document.removeEventListener('keydown', this._trapListener);
            this._trapListener = null;
        }
    },

    _emitClose(modalId) {
        const handler = this._closeHandlers.get(modalId);
        if (handler) {
            this._closeHandlers.delete(modalId);
            handler();
        }
    },

    _syncModalStack() {
        this._activeModals = this._activeModals.filter(modalId => {
            const modal = document.getElementById(modalId);
            return Boolean(modal && modal.classList.contains('show'));
        });

        const topIndex = this._activeModals.length - 1;
        this._activeModals.forEach((modalId, index) => {
            const modal = document.getElementById(modalId);
            if (!modal) {
                return;
            }
            modal.style.zIndex = String(this._baseZIndex + index * this._stackStep);
            modal.inert = index !== topIndex;
            modal.setAttribute('aria-hidden', index === topIndex ? 'false' : 'true');
            if (index === topIndex) {
                modal.setAttribute('aria-modal', 'true');
            } else {
                modal.removeAttribute('aria-modal');
            }
        });
        document.body.classList.toggle('modal-open', this._activeModals.length > 0);
        this._syncBackgroundInert();
    },

    _syncBackgroundInert() {
        // 背景 inert：modal 打开期间，body 下除 modal 顶层祖先之外的
        // 所有内容一律不可聚焦/不可交互（主页面本身此前没有 inert，
        // Tab 可以逃出 modal 落到背景按钮上）。只记录本管理器置过的
        // inert，避免误清页面自身的 inert 状态。
        const modalRoots = new Set();
        for (const modalId of this._activeModals) {
            const modal = document.getElementById(modalId);
            if (!modal) continue;
            let node = modal;
            while (node.parentElement && node.parentElement !== document.body) {
                node = node.parentElement;
            }
            if (node.parentElement === document.body) {
                modalRoots.add(node);
            }
        }
        for (const el of [...this._inertedRoots]) {
            if (!modalRoots.has(el) || this._activeModals.length === 0) {
                el.inert = false;
                this._inertedRoots.delete(el);
            }
        }
        if (this._activeModals.length === 0) {
            return;
        }
        for (const child of Array.from(document.body.children)) {
            if (modalRoots.has(child) || this._inertedRoots.has(child)) {
                continue;
            }
            const tag = child.tagName;
            if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK'
                || tag === 'TEMPLATE' || tag === 'NOSCRIPT') {
                continue;
            }
            // 只接管"由本管理器置为 inert"的元素：页面/嵌套组件可能本来
            // 就把自己的根置为 inert（另一种 UI 状态），关闭 modal 时只
            // 释放自己的 inert，绝不能把别人的状态强制清成 false。
            if (child.inert) {
                continue;
            }
            child.inert = true;
            this._inertedRoots.add(child);
        }
    },

    _restoreZIndex(modalId, modal) {
        if (!this._originalZIndexes.has(modalId)) {
            return;
        }
        const original = this._originalZIndexes.get(modalId);
        if (original) {
            modal.style.zIndex = original;
        } else {
            modal.style.removeProperty('z-index');
        }
        this._originalZIndexes.delete(modalId);
    },

    _focusModal(modalId) {
        const modal = document.getElementById(modalId);
        if (!modal || this._activeModals[this._activeModals.length - 1] !== modalId) {
            return;
        }
        const content = modal.querySelector('.modal-content');
        const focusTarget = modal.querySelector(MODAL_FOCUSABLE_SELECTOR) || content;
        if (focusTarget) {
            if (focusTarget === content && !content.hasAttribute('tabindex')) {
                content.setAttribute('tabindex', '-1');
            }
            focusTarget.focus({ preventScroll: true });
        }
    },

    _restoreFocus(modalId) {
        const origin = this._focusOrigins.get(modalId);
        this._focusOrigins.delete(modalId);
        if (this._activeModals.length > 0) {
            this._focusModal(this._activeModals[this._activeModals.length - 1]);
        } else if (origin && origin.isConnected && typeof origin.focus === 'function') {
            origin.focus({ preventScroll: true });
        }
    }
};

window.showModalError = showModalError;
window.createAnalysisModal = createAnalysisModal;
window.ModalManager = ModalManager;
