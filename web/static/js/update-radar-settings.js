// Shared settings dialog in the tools shell and standalone radar page.
(function () {
    'use strict';
    const id = 'radar-settings-modal';
    const modal = () => document.getElementById(id);
    const field = name => modal().querySelector('#radar-' + name);
    const owner = () => window.ModalManager || window.EmbeddedModalController;
    const tasks = () => [...modal().querySelectorAll('[name="radar-task"]')];
    async function request(method, body) {
        const response = await fetch('/api/gms-update-radar/settings', {method,
            ...(body ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {})});
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(response.status === 403 ? '仅管理员可修改雷达配置' : result.error
                || (Array.isArray(result.detail) ? result.detail.map(item => item.msg).join('；') : '雷达设置请求失败'));
        }
        return result.data;
    }
    function syncTasks() {
        const count = tasks().filter(task => task.checked).length;
        field('all-tasks').checked = count === 6;
        field('all-tasks').indeterminate = count > 0 && count < 6;
        field('task-count').textContent = `已选择 ${count} / 6 项`;
    }
    async function open() {
        const config = await request('GET');
        field('enabled').checked = config.enabled;
        field('time').value = config.scan_time;
        field('timezone').value = config.timezone;
        field('recipients').value = config.recipients.join('\n');
        field('notify').value = config.notify;
        field('url').value = config.dashboard_url || location.origin + '/gms-update-radar';
        const selected = config.tasks || tasks().map(task => task.value);
        tasks().forEach(task => { task.checked = selected.includes(task.value); });
        syncTasks();
        field('settings-feedback').textContent = '';
        field('settings-fields').disabled = false;
        owner().open(id);
        try {
            const response = await fetch('/api/email/smtp');
            const data = await response.json();
            field('smtp-status').textContent = response.ok && data.data?.smtp_host
                ? `复用全局 SMTP：${data.data.smtp_host}${data.data.enabled === false ? '（邮件总开关已关闭）' : ''}。配置入口：测试执行 → 系统设置 → 邮件发送方。`
                : '尚未配置 SMTP：请在「测试执行 → 系统设置 → 邮件发送方」配置。扫描记录会独立保留。';
        } catch (_) { /* Optional SMTP status; settings remain editable. */ }
    }
    async function save(event) {
        event.preventDefault();
        const selected = tasks().filter(task => task.checked).map(task => task.value);
        if (!selected.length) { field('settings-feedback').textContent = '请至少选择一个监听任务'; return; }
        field('settings-save').disabled = true;
        try {
            const config = await request('PUT', {enabled: field('enabled').checked, scan_time: field('time').value,
                timezone: field('timezone').value, recipients: field('recipients').value.split(/[,;\n]+/).map(s => s.trim()).filter(Boolean),
                notify: field('notify').value, dashboard_url: field('url').value || null, tasks: selected});
            owner().close(id);
            window.dispatchEvent(new CustomEvent('gms:radar-settings-saved', {detail: config}));
            if (typeof window.showToast === 'function') window.showToast('雷达设置已保存', 'success');
        } catch (error) { field('settings-feedback').textContent = error.message; }
        finally { field('settings-save').disabled = false; }
    }
    function bind() {
        if (!modal()) return;
        field('settings-form').addEventListener('submit', save);
        field('settings-close').addEventListener('click', () => owner().close(id));
        field('settings-cancel').addEventListener('click', () => owner().close(id));
        modal().addEventListener('click', event => { if (event.target === modal()) owner().close(id); });
        field('all-tasks').addEventListener('change', () => { tasks().forEach(task => { task.checked = field('all-tasks').checked; }); syncTasks(); });
        tasks().forEach(task => task.addEventListener('change', syncTasks));
    }
    function createEditButton() {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = '✏️';
        button.title = '修改雷达执行时间、收件人、发送策略和监听任务';
        button.setAttribute('aria-label', '修改');
        button.className = 'tool-radar-edit';
        button.style.cssText = 'flex:1;padding:3px;border:1px solid var(--border-color);border-radius:4px;background:var(--lighter-bg);color:var(--text-primary);font-size:12px;cursor:pointer';
        button.addEventListener('click', event => {
            event.stopPropagation();
            open().catch(error => window.showToast?.(error.message, 'error'));
        });
        return button;
    }
    const builtinTool = {icon: '📡', title: 'GMS更新雷达',
        description: '定时全量扫描所选GMS更新，记录前后差异并发送邮件汇总',
        url: '/gms-update-radar', builtin_id: 'gms-update-radar'};
    window.GmsUpdateRadarSettings = {open, createEditButton, builtinTool};
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind, {once: true});
    else bind();
})();
