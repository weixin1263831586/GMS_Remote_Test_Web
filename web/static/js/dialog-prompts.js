// Content-aware async forms and confirmations; user text is assigned through DOM APIs.
(function () {
    'use strict';
    let sequence = 0;
    const frame = `<svg class="tech-frame" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M16 1H986L999 14V584L984 599H16L1 584V16Z" vector-effect="non-scaling-stroke"/></svg><span class="tech-corner-mark tech-corner-mark--tl" aria-hidden="true"></span><span class="tech-corner-mark tech-corner-mark--br" aria-hidden="true"></span>`;
    const titleIcon = `<svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#edit"></use></svg>`;
    function openPrompt(title, fields, options = {}) {
        return new Promise(resolve => {
            const id = `gms-input-dialog-${++sequence}`;
            const owner = typeof ModalManager !== 'undefined' ? ModalManager : window.EmbeddedModalController;
            const modal = document.createElement(owner ? 'div' : 'dialog');
            modal.id = id;
            modal.className = owner ? 'modal' : 'modal-backdrop gms-native-dialog';
            modal.setAttribute('aria-labelledby', `${id}-title`);
            modal.innerHTML = `
                <form class="modal-content modal-s tech-dialog tech-dialog--input" autocomplete="off">
                    ${frame}
                    <div class="modal-header tech-dialog-header">
                        <div class="tech-title-group"><span class="tech-title-glyph" aria-hidden="true">${titleIcon}</span><span class="tech-title-divider" aria-hidden="true"></span><h2 class="modal-title" id="${id}-title"></h2></div>
                        <button class="modal-close" type="button" aria-label="关闭">×</button>
                        <svg class="tech-header-rule" viewBox="0 0 1000 16" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M0 15H490L506 1H916" vector-effect="non-scaling-stroke"/></svg>
                    </div>
                    <div class="modal-body"><p class="tech-intro" hidden></p><div class="tech-prompt-fields"></div></div>
                    <div class="modal-buttons tech-dialog-actions"><span class="tech-footer-decoration" aria-hidden="true"></span><button type="button" data-cancel>取消</button><button type="submit" class="btn-primary"></button></div>
                </form>`;
            modal.querySelector('.modal-title').textContent = String(title);
            const message = modal.querySelector('.tech-intro');
            if (options.message) { message.textContent = String(options.message); message.hidden = false; }
            if (options.danger) modal.classList.add('gms-danger-prompt');
            modal.querySelector('[type=submit]').textContent = options.submitLabel || '确定';
            const container = modal.querySelector('.tech-prompt-fields');
            const inputs = new Map();
            fields.forEach((field, index) => {
                const row = document.createElement('div');
                row.className = 'tech-field-card tech-field-stack';
                row.innerHTML = `<span class="tech-field-glyph" aria-hidden="true"><svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#edit"></use></svg></span>`;
                const label = document.createElement('label');
                const input = document.createElement(field.multiline ? 'textarea' : 'input');
                input.id = `${id}-field-${index}`;
                label.htmlFor = input.id;
                label.textContent = field.label;
                if (!field.multiline) input.type = field.type || 'text';
                input.name = field.name;
                input.value = field.value || '';
                input.placeholder = field.placeholder || '';
                input.required = Boolean(field.required);
                if (field.maxLength) input.maxLength = field.maxLength;
                row.append(label, input);
                container.append(row);
                inputs.set(field.name, input);
            });
            let settled = false;
            const observer = new MutationObserver(() => {
                if (owner && !modal.classList.contains('show')) finish(null);
            });
            function finish(value) {
                if (settled) return;
                settled = true;
                observer.disconnect();
                if (owner) owner.close(id);
                else modal.close();
                modal.remove();
                resolve(value);
            }
            modal.querySelector('form').addEventListener('submit', event => {
                event.preventDefault();
                finish(Object.fromEntries([...inputs].map(([name, input]) => [name, input.value.trim()])));
            });
            modal.querySelector('[data-cancel]').addEventListener('click', () => finish(null));
            modal.querySelector('.modal-close').addEventListener('click', () => finish(null));
            modal.addEventListener('click', event => { if (event.target === modal) finish(null); });
            modal.addEventListener('keydown', event => { if (!owner) event.stopPropagation(); });
            modal.addEventListener('cancel', event => { event.preventDefault(); finish(null); });
            modal.addEventListener('close', () => finish(null));
            document.body.append(modal);
            if (owner) {
                owner.open(id);
                observer.observe(modal, {attributes: true, attributeFilter: ['class']});
            } else {
                modal.showModal();
                (modal.querySelector('input,textarea') || modal.querySelector('[data-cancel]')).focus();
            }
        });
    }
    window.GmsDialogs = {
        form: openPrompt,
        async confirm(title, message) {
            if (typeof window.parent.showConfirmDialog === 'function') {
                return window.parent.showConfirmDialog(title, message);
            }
            return (await openPrompt(title, [], {message, danger: true})) !== null;
        }
    };
})();
