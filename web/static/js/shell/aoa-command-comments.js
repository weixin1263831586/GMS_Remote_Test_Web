// Profile annotations are plain text, aligned with commands and never sent as HID input.
'use strict';

function aoaValidateCommandComments(metadata, count = null) {
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) throw new Error('配置 metadata 必须是对象');
    const comments = metadata.command_comments;
    if (comments === undefined) return;
    if (!Array.isArray(comments) || comments.length > 256 || (count !== null && comments.length !== count)
            || comments.some(value => typeof value !== 'string' || Array.from(value).length > 240)) {
        throw new Error('注释须与命令逐条对应，每条最多 240 字');
    }
    if (new TextEncoder().encode(JSON.stringify(comments)).length > 65536) throw new Error('注释总大小不能超过 64 KiB');
}

function aoaCurrentComments() {
    const comments = aoaPanelState.profileMetadata.command_comments || [];
    return aoaPanelState.commands.map((_, index) => comments[index] || '');
}

function aoaSyncCommandComments(removeIndex = null) {
    const comments = aoaPanelState.profileMetadata.command_comments;
    if (!comments) return;
    const next = [...comments];
    if (removeIndex !== null) next.splice(removeIndex, 1);
    aoaPanelState.profileMetadata.command_comments = aoaPanelState.commands.map((_, index) => next[index] || '');
}

function aoaResetCommandComments() {
    aoaPanelState.commentEdit = null;
    delete aoaPanelState.profileMetadata.command_comments;
}

function aoaCreateCommandComment(index) {
    const text = aoaCurrentComments()[index];
    const input = document.createElement('input');
    input.type = 'text'; input.className = 'aoa-command-comment'; input.maxLength = 240;
    input.dataset.commentIndex = index;
    input.value = aoaPanelState.commentEdit?.index === index ? aoaPanelState.commentEdit.draft : text.replace(/\r?\n/g, ' ');
    input.title = text || '注释不参与命令执行';
    input.setAttribute('aria-label', `第 ${index + 1} 条命令注释`);
    input.disabled = aoaPanelState.running || aoaPanelState.playbackActive;
    input.addEventListener('input', () => { aoaPanelState.commentEdit = {index, draft: input.value}; });
    input.addEventListener('blur', () => {
        if (input.isConnected && aoaPanelState.commentEdit?.index === index) aoaCommentEditFinish();
    });
    input.addEventListener('keydown', event => {
        if (event.isComposing) return;
        if (event.key === 'Escape' || event.key === 'Enter') {
            event.preventDefault(); aoaCommentEditFinish(event.key !== 'Escape');
        }
    });
    return input;
}

function aoaCommentEditFinish(save = true) {
    const edit = aoaPanelState.commentEdit;
    if (!edit) return true;
    if (aoaPanelState.running || aoaPanelState.playbackActive) return false;
    if (save && edit.index < aoaPanelState.commands.length) {
        const comments = aoaCurrentComments(); comments[edit.index] = edit.draft.trim();
        try { aoaValidateCommandComments({command_comments: comments}, aoaPanelState.commands.length); }
        catch (error) { aoaSetStatus(error.message, 'warning'); return false; }
        aoaPanelState.profileMetadata.command_comments = comments;
        aoaSetStatus(`第 ${edit.index + 1} 条注释已更新，保存配置后可持久保留`);
    }
    const input = aoaEl('aoa-command-list')?.querySelector(`[data-comment-index="${edit.index}"]`);
    const value = aoaCurrentComments()[edit.index] || '';
    if (input) { input.value = value.replace(/\r?\n/g, ' '); input.title = value || '注释不参与命令执行'; }
    aoaPanelState.commentEdit = null;
    return true;
}
