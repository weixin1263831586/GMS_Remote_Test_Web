// Shared modal helpers and Escape-key modal lifecycle management.

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
                <span class="modal-close" role="button" tabindex="0" aria-label="关闭">&times;</span>
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
    _activeModals: [],
    _dynamicModals: new Set(),
    _closeHandlers: new Map(),
    _originalZIndexes: new Map(),
    _focusOrigins: new Map(),
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
    },

    _cleanupEscListener() {
        if (this._escListener && this._activeModals.length === 0) {
            document.removeEventListener('keydown', this._escListener);
            this._escListener = null;
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
        const focusTarget = modal.querySelector(
            '[autofocus], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
        ) || content;
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
