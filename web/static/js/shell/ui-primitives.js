/* Shared presentation only: labels stay escaped and icons stay decorative. */
window.GmsUi = (() => {
    const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char]);
    const icon = name => {
        const key = /^[a-z-]+$/.test(name) ? name : 'info';
        return `<svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#${key}"></use></svg>`;
    };
    const badge = (tone, name, label) => {
        const safeTone = /^(success|warning|danger|info|accent|neutral)$/.test(tone) ? tone : 'neutral';
        return `<span class="ui-status is-${safeTone}">${icon(name)}<span>${escape(label)}</span></span>`;
    };
    return Object.freeze({icon, badge, escape});
})();
