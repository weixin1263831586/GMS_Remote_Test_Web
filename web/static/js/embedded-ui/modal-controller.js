// Shared modal stack controller for standalone/iframe pages (Redmine, Gerrit, ...).
// Single owner for everything the pages used to reimplement separately:
// modal stack + z-index, inert/aria attributes, Escape, backdrop click,
// focus trap + initial focus + focus restore, background inert, scroll lock.
// 语义契约（tests/test_runtime_ui_smoke.py 固化）：
//   - 打开/关闭用 .show 类；非栈顶 modal 置 inert；
//   - Escape / 点击遮罩只关栈顶；
//   - 全局 window.showModal / window.hideModal 别名由各页面提供。
(function () {
    'use strict';

    // 与 web/static/js/modal.js (Shell ModalManager) 相同的可聚焦契约。
    var FOCUSABLE_SELECTOR = [
        '[autofocus]',
        'input:not([disabled])',
        'select:not([disabled])',
        'textarea:not([disabled])',
        'button:not([disabled])',
        '[href]',
        '[tabindex]:not([tabindex="-1"])'
    ].join(', ');

    var EmbeddedModalController = {
        _stack: [],
        _escListener: null,
        _trapListener: null,
        _clickListener: null,
        _focusOrigins: new Map(),
        _inertedRoots: new Set(),
        _baseZIndex: 10000,
        _stackStep: 20,

        open: function (modalId) {
            var modal = document.getElementById(modalId);
            if (!modal) return;
            var existing = this._stack.indexOf(modalId);
            if (existing !== -1) this._stack.splice(existing, 1);
            this._stack.push(modalId);
            if (!this._focusOrigins.has(modalId)) {
                this._focusOrigins.set(modalId, document.activeElement);
            }
            modal.classList.add('show');
            this._ensureListeners();
            this.sync();
            var self = this;
            window.requestAnimationFrame(function () { self._focusModal(modalId); });
        },

        close: function (modalId) {
            var modal = document.getElementById(modalId);
            if (modal) {
                modal.classList.remove('show');
                modal.inert = false;
                modal.setAttribute('aria-hidden', 'true');
                modal.removeAttribute('aria-modal');
                modal.style.removeProperty('z-index');
            }
            var index = this._stack.indexOf(modalId);
            if (index !== -1) this._stack.splice(index, 1);
            this._restoreFocus(modalId);
            this.sync();
        },

        // 动态创建后随取消/关闭从 DOM 移除的 modal（Redmine 弹框模式）。
        remove: function (modalId) {
            var modal = document.getElementById(modalId);
            if (modal) modal.remove();
            var index = this._stack.indexOf(modalId);
            if (index !== -1) this._stack.splice(index, 1);
            this._restoreFocus(modalId);
            this.sync();
        },

        closeTopmost: function () {
            if (this._stack.length) this.close(this._stack[this._stack.length - 1]);
        },

        isOpen: function (modalId) {
            var modal = document.getElementById(modalId);
            return Boolean(modal && modal.classList.contains('show'));
        },

        sync: function () {
            var self = this;
            this._stack = this._stack.filter(function (modalId) {
                var modal = document.getElementById(modalId);
                return Boolean(modal && modal.classList.contains('show'));
            });
            var topIndex = this._stack.length - 1;
            this._stack.forEach(function (modalId, index) {
                var modal = document.getElementById(modalId);
                modal.style.zIndex = String(self._baseZIndex + index * self._stackStep);
                modal.inert = index !== topIndex;
                modal.setAttribute('role', modal.getAttribute('role') || 'dialog');
                modal.setAttribute('aria-hidden', index === topIndex ? 'false' : 'true');
                if (index === topIndex) modal.setAttribute('aria-modal', 'true');
                else modal.removeAttribute('aria-modal');
            });
            document.documentElement.classList.toggle('modal-open', this._stack.length > 0);
            document.body.classList.toggle('modal-open', this._stack.length > 0);
            this._syncBackgroundInert();
        },

        _ensureListeners: function () {
            var self = this;
            if (!this._escListener) {
                this._escListener = function (event) {
                    if (event.key === 'Escape' && self._stack.length) {
                        event.preventDefault();
                        event.stopPropagation();
                        self.close(self._stack[self._stack.length - 1]);
                    }
                };
                document.addEventListener('keydown', this._escListener);
            }
            if (!this._clickListener) {
                // 点击遮罩（.modal 本体）只关闭栈顶，避免误关下层。
                this._clickListener = function (event) {
                    if (event.target && event.target.classList
                        && event.target.classList.contains('modal')
                        && self._stack[self._stack.length - 1] === event.target.id) {
                        self.close(event.target.id);
                    }
                };
                document.addEventListener('click', this._clickListener);
            }
            if (!this._trapListener) {
                // Focus trap：Tab/Shift+Tab 只在栈顶 modal 内循环，焦点不得
                // 落到被 inert 的背景（删除/回复/设置等 modal 同契约）。
                this._trapListener = function (event) {
                    if (event.key !== 'Tab' || self._stack.length === 0) return;
                    var modal = document.getElementById(self._stack[self._stack.length - 1]);
                    if (!modal) return;
                    var focusables = Array.prototype.filter.call(
                        modal.querySelectorAll(FOCUSABLE_SELECTOR),
                        function (el) {
                            return !el.disabled && el.getAttribute('tabindex') !== '-1';
                        }
                    );
                    if (focusables.length === 0) {
                        event.preventDefault();
                        modal.focus({ preventScroll: true });
                        return;
                    }
                    var active = document.activeElement;
                    var inside = modal.contains(active);
                    var first = focusables[0];
                    var last = focusables[focusables.length - 1];
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

        _maybeCleanupListeners: function () {
            var self = this;
            if (this._stack.length) return;
            ['_escListener', '_trapListener', '_clickListener'].forEach(function (key) {
                if (self[key]) {
                    document.removeEventListener('keydown', self[key]);
                    document.removeEventListener('click', self[key]);
                    self[key] = null;
                }
            });
        },

        _syncBackgroundInert: function () {
            // modal 打开期间，body 下除 modal 顶层祖先之外一律 inert。
            var modalRoots = new Set();
            var self = this;
            this._stack.forEach(function (modalId) {
                var modal = document.getElementById(modalId);
                if (!modal) return;
                var node = modal;
                while (node.parentElement && node.parentElement !== document.body) {
                    node = node.parentElement;
                }
                if (node.parentElement === document.body) modalRoots.add(node);
            });
            this._inertedRoots.forEach(function (el) {
                if (!modalRoots.has(el) || self._stack.length === 0) {
                    el.inert = false;
                    self._inertedRoots.delete(el);
                }
            });
            if (this._stack.length === 0) return;
            Array.from(document.body.children).forEach(function (child) {
                if (modalRoots.has(child) || self._inertedRoots.has(child)) return;
                var tag = child.tagName;
                if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK'
                    || tag === 'TEMPLATE' || tag === 'NOSCRIPT') return;
                child.inert = true;
                self._inertedRoots.add(child);
            });
        },

        _focusModal: function (modalId) {
            var modal = document.getElementById(modalId);
            if (!modal || this._stack[this._stack.length - 1] !== modalId) return;
            var content = modal.querySelector('.modal-content');
            var focusTarget = modal.querySelector(FOCUSABLE_SELECTOR) || content;
            if (!focusTarget) return;
            if (focusTarget === content && !content.hasAttribute('tabindex')) {
                content.setAttribute('tabindex', '-1');
            }
            focusTarget.focus({ preventScroll: true });
        },

        _restoreFocus: function (modalId) {
            var origin = this._focusOrigins.get(modalId);
            this._focusOrigins.delete(modalId);
            if (origin && origin.isConnected && typeof origin.focus === 'function') {
                origin.focus({ preventScroll: true });
            }
        }
    };

    window.EmbeddedModalController = EmbeddedModalController;
})();
