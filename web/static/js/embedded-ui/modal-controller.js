// Shared modal stack controller for standalone/iframe pages (Redmine, Gerrit, ...).
// Single owner for modal stack + z-index, inert/aria attributes, Escape,
// backdrop click, focus trap + initial focus + focus restore, background inert,
// and scroll lock.
(function () {
    'use strict';

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
        // Only entries in this Map are inert values OWNED by this controller.
        // A root that was already inert before a modal opened is never added,
        // so closing the modal cannot accidentally re-enable page-owned state.
        _inertedRoots: new Map(),
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
            // Release background inert BEFORE restoring focus. Focusing an
            // element under an inert root is ignored by browsers.
            this.sync();
            this._restoreFocus(modalId);
            this._maybeCleanupListeners();
        },

        remove: function (modalId) {
            var modal = document.getElementById(modalId);
            if (modal) modal.remove();
            var index = this._stack.indexOf(modalId);
            if (index !== -1) this._stack.splice(index, 1);
            this.sync();
            this._restoreFocus(modalId);
            this._maybeCleanupListeners();
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
                if (!modal) return;
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
                        if (!modal.hasAttribute('tabindex')) modal.setAttribute('tabindex', '-1');
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
            if (this._stack.length) return;
            if (this._escListener) {
                document.removeEventListener('keydown', this._escListener);
                this._escListener = null;
            }
            if (this._trapListener) {
                document.removeEventListener('keydown', this._trapListener);
                this._trapListener = null;
            }
            if (this._clickListener) {
                document.removeEventListener('click', this._clickListener);
                this._clickListener = null;
            }
        },

        _syncBackgroundInert: function () {
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

            Array.from(this._inertedRoots.keys()).forEach(function (el) {
                if (!modalRoots.has(el) || self._stack.length === 0) {
                    el.inert = self._inertedRoots.get(el);
                    self._inertedRoots.delete(el);
                }
            });
            if (this._stack.length === 0) return;

            Array.from(document.body.children).forEach(function (child) {
                if (modalRoots.has(child) || self._inertedRoots.has(child)) return;
                var tag = child.tagName;
                if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK'
                    || tag === 'TEMPLATE' || tag === 'NOSCRIPT') return;
                // Respect inert state owned by the page itself.
                if (child.inert) return;
                self._inertedRoots.set(child, false);
                child.inert = true;
            });
        },

        _focusModal: function (modalId) {
            var modal = document.getElementById(modalId);
            if (!modal || this._stack[this._stack.length - 1] !== modalId) return;
            var content = modal.querySelector('.modal-content');
            var focusTarget = modal.querySelector(FOCUSABLE_SELECTOR) || content || modal;
            if (!focusTarget) return;
            if (!focusTarget.hasAttribute('tabindex')
                && (focusTarget === content || focusTarget === modal)) {
                focusTarget.setAttribute('tabindex', '-1');
            }
            focusTarget.focus({ preventScroll: true });
        },

        _restoreFocus: function (modalId) {
            var origin = this._focusOrigins.get(modalId);
            this._focusOrigins.delete(modalId);
            if (this._stack.length > 0) {
                this._focusModal(this._stack[this._stack.length - 1]);
            } else if (origin && origin.isConnected && typeof origin.focus === 'function') {
                origin.focus({ preventScroll: true });
            }
        }
    };

    window.EmbeddedModalController = EmbeddedModalController;
})();
