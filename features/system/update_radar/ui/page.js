'use strict';
const radar = {admin: false, offset: 0, changeOffset: 0, run: null, timer: null, categories: {}, timezone: 'Asia/Shanghai'};
const $ = id => document.getElementById(id);
const labels = {manual: '手动', schedule: '每日计划', running: '扫描中', completed: '完成', partial: '部分失败', failed: '失败', interrupted: '已中断', pending: '待发送', sending: '发送中', sent: '已发送', skipped: '按策略未发送', unconfigured: '收件人未设置', unknown: '发送状态未确认'};
const label = key => labels[key] || key;
const date = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone: radar.timezone, hour12: false}) : '—';
function message(text, error = false) { $('message').textContent = text; $('message').dataset.error = String(error); }
async function request(path, options = {}) {
    const response = await fetch('/api/gms-update-radar' + path, options);
    const body = await response.json();
    if (!response.ok || !body.success) {
        const error = new Error(body.error || (Array.isArray(body.detail) ? body.detail.map(item => item.msg).join('；') : body.detail) || '请求失败');
        error.status = response.status;
        throw error;
    }
    return body.data;
}
function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
}
function showView(view) {
    document.querySelectorAll('[data-view]').forEach(tab => {
        const active = tab.dataset.view === view;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        $(tab.getAttribute('aria-controls')).hidden = !active;
    });
}
async function loadSettings() {
    try {
        await request('/settings');
        radar.admin = true;
        $('modify').disabled = false;
    } catch (error) {
        if (error.status !== 403) message(error.message, true);
    }
}
async function refresh() {
    const [status, history] = await Promise.all([request('/status'), request(`/runs?limit=30&offset=${radar.offset}`)]);
    radar.timezone = status.timezone;
    document.querySelectorAll('[data-task]').forEach(chip => { const watched = status.tasks.includes(chip.dataset.task); chip.style.opacity = watched ? '1' : '.4'; chip.title = watched ? '正在监听' : '未监听'; });
    $('schedule-status').textContent = status.enabled
        ? `每天 ${status.scan_time}（${status.timezone}）全量扫描 · 下次 ${date(status.next_run_at)}`
        : `每日计划未开启 · 默认 ${status.scan_time}（${status.timezone}）`;
    $('schedule-status').textContent += ` · 监听 ${status.tasks.length} / 6 项`;
    radar.categories = history.categories;
    $('runs').replaceChildren();
    if (!history.items.length) {
        const row = element('tr');
        const cell = element('td', '暂无记录。可先设置收件人，再手动全量扫描建立基线。');
        cell.colSpan = 7;
        row.append(cell); $('runs').append(row);
    }
    let running = false;
    for (const run of history.items) {
        running ||= run.status === 'running';
        const row = element('tr');
        for (const text of [`#${run.id}`, label(run.trigger), date(run.started_at), label(run.status), run.summary.changes_total ?? '—', label(run.email_status)]) row.append(element('td', text));
        const button = element('button', '查看');
        button.type = 'button'; button.dataset.run = run.id;
        const actions = element('td'); actions.append(button); row.append(actions);
        $('runs').append(row);
    }
    $('scan').disabled = !radar.admin || running;
    $('runs-prev').disabled = radar.offset === 0;
    $('runs-next').disabled = radar.offset + 30 >= history.total;
    $('runs-page').textContent = history.total ? `${radar.offset + 1}–${Math.min(radar.offset + 30, history.total)} / ${history.total}` : '0 条记录';
    if (radar.run) await loadDetail(radar.run);
    clearTimeout(radar.timer);
    radar.timer = setTimeout(() => { if (!document.hidden) refresh().catch(error => message(error.message, true)); }, running ? 3000 : 30000);
}
async function loadDetail(id) {
    const data = await request(`/runs/${id}?limit=100&offset=${radar.changeOffset}`);
    radar.run = id;
    const run = data.run;
    $('detail').hidden = false;
    $('detail-title').textContent = `扫描 #${id} · ${label(run.status)}`;
    $('detail-state').textContent = `${date(run.started_at)} → ${date(run.finished_at)} · 邮件：${label(run.email_status)} · 监听：${(run.summary.tasks || Object.keys(radar.categories)).map(key => radar.categories[key]).join('、')}${run.summary.initial_baseline ? ' · 首次建立基线' : ''}`;
    $('scan-errors').textContent = Object.values(run.summary.errors || {}).join('\n');
    $('mail-error').textContent = run.email_error;
    $('counts').replaceChildren();
    for (const [key, name] of Object.entries(radar.categories)) {
        const item = run.summary.categories?.[key];
        if (!item) continue;
        const card = element('div', undefined, 'count');
        const failed = run.summary.errors?.[key === 'exemptions' ? 'exemptions' : 'updates'];
        card.append(element('strong', name), element('div', `${item.before} → ${item.after}`), element('small', failed ? '扫描失败，保留原数据' : `新增 ${item.added} / 修改 ${item.modified} / 移除 ${item.removed}`, failed ? 'error' : ''));
        $('counts').append(card);
    }
    const allowed = radar.admin && run.status !== 'running';
    $('preview-mail').disabled = !allowed;
    $('retry-mail').disabled = !allowed || run.email_status === 'sending';
    $('retry-mail').textContent = run.email_status === 'unknown' ? '核对邮件台账后重发' : '重发本次汇总';
    $('changes').replaceChildren();
    for (const item of data.changes.items) {
        const record = item.after_json || item.before_json || {};
        const title = record.file_name || record.title || record.test_case || record.patch_id || record.month_label || record.section_title || record.item_key || record.row_key || '明细';
        const detail = element('details');
        detail.append(element('summary', `${radar.categories[item.category]} · ${{added: '新增', modified: '修改', removed: '移除'}[item.change_type]} · ${title}`));
        const diff = element('div', undefined, 'diff');
        for (const [text, value] of [['扫描前', item.before_json], ['扫描后', item.after_json]]) {
            const side = element('div'); side.append(element('p', text), element('pre', value ? JSON.stringify(value, null, 2) : '无此条目')); diff.append(side);
        }
        detail.append(diff); $('changes').append(detail);
    }
    if (!data.changes.total) $('changes').append(element('p', run.status === 'running' ? '扫描完成后展示差异。' : Object.keys(run.summary.errors || {}).length ? '未获取完整扫描结果，请查看上方失败原因。' : '本次扫描未发现数据变化。', 'muted'));
    $('changes-prev').disabled = radar.changeOffset === 0;
    $('changes-next').disabled = radar.changeOffset + 100 >= data.changes.total;
    $('changes-page').textContent = data.changes.total ? `${radar.changeOffset + 1}–${Math.min(radar.changeOffset + 100, data.changes.total)} / ${data.changes.total}` : '0 项变更';
}
function action(id, fn) { $(id).addEventListener('click', () => Promise.resolve().then(fn).catch(error => message(error.message, true))); }
document.querySelector('.workspace-tabs').addEventListener('click', event => { const tab = event.target.closest('[data-view]'); if (tab) showView(tab.dataset.view); });
document.querySelector('.workspace-tabs').addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const tabs = [...document.querySelectorAll('[data-view]')];
    const index = tabs.indexOf(document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault(); tabs[next].focus(); showView(tabs[next].dataset.view);
});
action('modify', () => window.GmsUpdateRadarSettings.open());
window.addEventListener('gms:radar-settings-saved', () => { message('计划与邮件设置已保存。'); refresh().catch(error => message(error.message, true)); });
action('refresh', refresh);
action('scan', async () => {
    $('scan').disabled = true;
    try {
        const data = await request('/scan', {method: 'POST'});
        radar.run = data.run_id; radar.changeOffset = 0; radar.offset = 0;
        message(`全量扫描 #${data.run_id} 已启动。`); showView('history'); await refresh();
    } catch (error) { $('scan').disabled = !radar.admin; throw error; }
});
$('runs').addEventListener('click', event => { const button = event.target.closest('[data-run]'); if (button) { radar.changeOffset = 0; $('email-preview').hidden = true; loadDetail(Number(button.dataset.run)).catch(error => message(error.message, true)); } });
action('runs-prev', () => { radar.offset -= 30; return refresh(); });
action('runs-next', () => { radar.offset += 30; return refresh(); });
action('changes-prev', () => { radar.changeOffset -= 100; return loadDetail(radar.run); });
action('changes-next', () => { radar.changeOffset += 100; return loadDetail(radar.run); });
action('preview-mail', async () => { const data = await request(`/runs/${radar.run}/email`); $('email-preview').hidden = false; $('email-preview').textContent = data.subject + '\n\n' + data.body; });
action('retry-mail', async () => {
    $('retry-mail').disabled = true;
    try { await request(`/runs/${radar.run}/email`, {method: 'POST'}); await loadDetail(radar.run); }
    finally { $('retry-mail').disabled = false; }
});
document.addEventListener('visibilitychange', () => { clearTimeout(radar.timer); if (!document.hidden) refresh().catch(error => message(error.message, true)); });
window.addEventListener('pagehide', () => clearTimeout(radar.timer));
(async () => {
    await loadSettings(); await refresh();
    const id = Number(new URLSearchParams(location.search).get('run'));
    if (Number.isSafeInteger(id) && id > 0) await loadDetail(id);
})().catch(error => message(error.message, true));
