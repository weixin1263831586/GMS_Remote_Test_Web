/* Shared presentation only: labels stay escaped and icons stay decorative. */
window.GmsUi = (() => {
    const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char]);
    const icon = name => {
        const key = /^[a-z-]+$/.test(name) ? name : 'info';
        return `<svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#${key}"></use></svg>`;
    };
    // Stable, escaped colored emoji for test-workspace controls and their dynamic states.
    const testEmoji = glyph => `<span class="test-action-icon" aria-hidden="true">${escape(glyph)}</span>`;
    const badge = (tone, name, label) => {
        const safeTone = /^(success|warning|danger|info|accent|neutral)$/.test(tone) ? tone : 'neutral';
        return `<span class="ui-status is-${safeTone}">${icon(name)}<span>${escape(label)}</span></span>`;
    };
    // Keep the size chooser only when a smaller supported page could require navigation.
    const paginationVisibility = (total, size, minimumSize) => ({
        footer: total > minimumSize,
        navigation: total > size,
    });
    return Object.freeze({icon, testEmoji, badge, escape, paginationVisibility});
})();
