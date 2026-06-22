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
    modal.style.cssText = 'z-index: 10000;';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 900px; max-height: 90vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">${title}</span>
                <span class="modal-close" onclick="ModalManager.close('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; margin-bottom: 20px;">🔍</div>
                    <div style="color: var(--text-secondary); margin-bottom: 12px;">${loadingMessage}</div>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    ModalManager.open(modalId);

    return { modal, modalId };
}

const ModalManager = {
    _escListener: null,
    _activeModals: [],
    _dynamicModals: new Set(),
    _closeHandlers: new Map(),

    open(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            if (modal.classList.contains('modal')) {
                modal.style.display = 'flex';
            }
            modal.classList.add('show');
            this._addActiveModal(modalId);
            this._ensureEscListener();
        }
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
            this._removeActiveModal(modalId);
            this._emitClose(modalId);
            this._cleanupEscListener();
        }
    },

    closeAll() {
        const modals = document.querySelectorAll('.modal.show');
        this._activeModals = [];
        modals.forEach(m => {
            m.classList.remove('show');
            m.style.display = 'none';
            this._emitClose(m.id);
        });
        this._cleanupEscListener();
    },

    toggle(modalId) {
        this.isOpen(modalId) ? this.close(modalId) : this.open(modalId);
    },

    isOpen(modalId) {
        const modal = document.getElementById(modalId);
        return modal ? modal.classList.contains('show') : false;
    },

    registerDynamic(modalElement) {
        document.body.appendChild(modalElement);
        if (modalElement.classList.contains('modal')) {
            modalElement.style.display = 'flex';
        }
        modalElement.classList.add('show');
        this._addActiveModal(modalElement.id);
        this._dynamicModals.add(modalElement.id);
        this._ensureEscListener();
        return modalElement;
    },

    unregisterDynamic(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.remove();
        }
        this._dynamicModals.delete(modalId);
        this._removeActiveModal(modalId);
        this._emitClose(modalId);
    },

    onClose(modalId, handler) {
        if (typeof handler === 'function') {
            this._closeHandlers.set(modalId, handler);
        }
    },

    _addActiveModal(modalId) {
        if (!this._activeModals.includes(modalId)) {
            this._activeModals.push(modalId);
        }
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
    }
};

window.showModalError = showModalError;
window.createAnalysisModal = createAnalysisModal;
window.ModalManager = ModalManager;
