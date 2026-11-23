'use strict';
const radar = {admin: false, access: 'checking', scanRunning: false, accessNotice: '',
    offset: 0, changeOffset: 0, run: null, timer: null, categories: {}, timezone: 'Asia/Shanghai'};
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

const radarKinds = {added:'新增', modified:'修改', removed:'移除'};
const radarColors = {added:'radar-added', modified:'radar-modified', removed:'radar-removed'};
const radarTitles = ['file_name','Name','title','name','test_case','package_name','Package name','patch_id','section_title','test_module','exemption_id','row_text'];
const radarIgnore = new Set(['id','source_key','row_key','row_index','item_key','table_index','section_key','headers_json','values_json']);
function radarBusinessRecord(raw) {
    const record = {...(raw || {})};
    try {
        const headers = typeof record.headers_json === 'string' ? JSON.parse(record.headers_json) : record.headers_json;
        const values = typeof record.values_json === 'string' ? JSON.parse(record.values_json) : record.values_json;
        if (Array.isArray(headers) && Array.isArray(values)) headers.forEach((name, i) => {
            if (typeof name === 'string' && name.trim() && record[name] === undefined) record[name] = values[i];
        });
    } catch (_) { /* Malformed row JSON must not break history. */ }
    return record;
}
function radarShort(value, size = 145) {
    const s = (typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value ?? '—')).replace(/\s+/g,' ').trim();
    return s.length > size ? s.slice(0,size - 1)+'…' : s || '—';
}
function radarRecordSummary(item) {
    const before = radarBusinessRecord(item.before_json), after = radarBusinessRecord(item.after_json);
    const record = item.change_type === 'removed' ? before : after;
    const title = radarTitles.map(key => record[key]).find(Boolean) || '数据条目';
    const fields = [...new Set([...Object.keys(after),...Object.keys(before)])].filter(k =>
        !radarIgnore.has(k) && !k.endsWith('_key') && before[k] !== after[k]);
    const visible = fields.filter(k => item.change_type === 'modified' || /name|title|desc|remark|text|module|version|package|file|patch/i.test(k));
    const description = visible.slice(0,2).map(k => item.change_type === 'modified'
        ? k+'：'+radarShort(before[k],70)+' → '+radarShort(after[k],70)
        : k+'：'+radarShort(record[k],130)).join('；');
    return {title:radarShort(title,100),description:description || '业务字段已变更，展开查看完整字段'};
}
function radarTd(row, value, css='') {const td=element('td',String(value ?? '—'),css);row.append(td);return td;}
function radarTable(names) {
    const table=element('table',undefined,'radar-data-table'),head=element('thead'),tr=element('tr'),body=element('tbody');
    names.forEach(name=>tr.append(element('th',name)));
    head.append(tr);table.append(head,body);return {table,body};
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
function updateScanAvailability() {
    const reason = radar.access === 'denied'
        ? '当前网页登录账号只有查看权限。请在主页面切换管理员账号，然后点击刷新。'
        : radar.access === 'unauthenticated'
            ? '登录状态已失效，请重新登录管理员账号后刷新。'
            : radar.access === 'unavailable'
                ? '管理员权限检查失败，请点击刷新重试。'
                : radar.access === 'checking'
                    ? '正在检查扫描权限…'
                    : radar.scanRunning
                        ? '已有扫描任务正在运行，完成后可再次扫描。' : '';
    $('modify').disabled = !radar.admin;
    $('modify').title = radar.admin ? '修改' : reason;
    $('scan').disabled = !!reason;
    $('scan').title = reason || '全量扫描当前选择的监听任务';
    $('scan').setAttribute('aria-label', reason ? '立即全量扫描（' + reason + '）' : '立即全量扫描');
    const text = $('message');
    if (radar.accessNotice && text.textContent === radar.accessNotice) message('');
    radar.accessNotice = '';
    if (reason && radar.access !== 'admin' && !text.textContent.trim()) {
        message(reason, radar.access === 'unavailable');
        radar.accessNotice = reason;
    }
}
async function refresh() {
    let status, history;
    try {
        [status, history] = await Promise.all([
            request('/status'), request(`/runs?limit=30&offset=${radar.offset}`)
        ]);
    } catch (error) {
        // Never retain a previously authorized button after a session expires.
        radar.admin = false;
        radar.access = error.status === 401 ? 'unauthenticated' : 'unavailable';
        updateScanAvailability();
        throw error;
    }
    radar.admin = status.can_manage === true;
    radar.access = radar.admin ? 'admin' : 'denied';
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
    radar.scanRunning = running;
    updateScanAvailability();
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
    $('mail-error').textContent = run.email_error;
    const errorList = Object.entries(run.summary.errors || {});
    $('scan-errors').hidden = !errorList.length;
    $('scan-errors').replaceChildren();
    if (errorList.length) {
        $('scan-errors').append(element('h3','扫描异常','radar-subtitle'));
        const {table,body}=radarTable(['模块','级别','原因']);
        for (const [group,detail] of errorList) {
            const tr=element('tr');
            radarTd(tr,{updates:'GMS更新',exemptions:'Mainline包豁免项',radar:'雷达任务'}[group]||group);
            radarTd(tr,'扫描失败','radar-warning');
            radarTd(tr,detail,'radar-description');body.append(tr);
        }
        const wrap=element('div',undefined,'radar-section');wrap.append(table);
        $('scan-errors').append(wrap,element('p','失败组保留扫描前的数据，不能据此认定无更新。','radar-warning'));
    }
    $('counts').replaceChildren();
    const stats=radarTable(['监控项','扫描前','扫描后','新增','修改','移除','状态']);
    for (const [key,name] of Object.entries(radar.categories)) {
        const item=run.summary.categories?.[key];
        if (!item) continue;
        const failed=Boolean(run.summary.errors?.[key==='exemptions'?'exemptions':'updates']||run.summary.errors?.radar);
        const tr=element('tr');radarTd(tr,name);
        radarTd(tr,item.before,'radar-num');radarTd(tr,item.after,'radar-num');
        for (const kind of ['added','modified','removed']) {
            const amount=Number(item[kind]||0);
            radarTd(tr,failed?'—':amount,'radar-num'+(!failed&&amount?' '+radarColors[kind]:''));
        }
        radarTd(tr,failed?'保留旧数据':'成功',failed?'radar-warning':'radar-added');
        stats.body.append(tr);
    }
    $('counts').append(stats.table);
    const allowed = radar.admin && run.status !== 'running';
    $('preview-mail').disabled = !allowed;
    $('retry-mail').disabled = !allowed || run.email_status === 'sending';
    $('retry-mail').textContent = run.email_status === 'unknown' ? '核对邮件台账后重发' : '重发本次汇总';
    $('changes').replaceChildren();
    if (data.changes.items.length) {
        const changesTable=radarTable(['类别','动作','对象','变更摘要 / 字段证据']);
        for (const item of data.changes.items) {
            const tr=element('tr');
            radarTd(tr,radar.categories[item.category]||item.category);
            const kind=element('td');
            kind.append(element('span',radarKinds[item.change_type]||'变更',
                'radar-pill '+(radarColors[item.change_type]||'')));
            tr.append(kind);
            const summary=radarRecordSummary(item);
            radarTd(tr,summary.title,'radar-record-title');
            const content=element('td',undefined,'radar-description');
            content.append(element('div',summary.description));
            const detail=element('details',undefined,'radar-evidence');
            detail.append(element('summary','查看完整字段证据'));
            const diff=element('div',undefined,'diff');
            for (const [label,value] of [['扫描前',item.before_json],['扫描后',item.after_json]]) {
                const part=element('div');
                part.append(element('p',label),element('pre',value?JSON.stringify(value,null,2):'无此条目'));
                diff.append(part);
            }
            detail.append(diff);content.append(detail);tr.append(content);changesTable.body.append(tr);
        }
        $('changes').append(changesTable.table);
    } else {
        $('changes').append(element('p',run.status==='running'?'扫描完成后展示差异。'
            :Object.keys(run.summary.errors||{}).length
                ?'成功扫描的项目未发现变化；其他项目尚未完整扫描，请查看异常。'
                :'本次扫描未发现数据变化。','radar-note'));
    }
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
    } catch (error) {
        await refresh().catch(() => updateScanAvailability());
        throw error;
    }
});
$('runs').addEventListener('click', event => { const button = event.target.closest('[data-run]'); if (button) { radar.changeOffset = 0; $('email-preview').hidden = true; $('email-preview-html').hidden = true; loadDetail(Number(button.dataset.run)).catch(error => message(error.message, true)); } });
action('runs-prev', () => { radar.offset -= 30; return refresh(); });
action('runs-next', () => { radar.offset += 30; return refresh(); });
action('changes-prev', () => { radar.changeOffset -= 100; return loadDetail(radar.run); });
action('changes-next', () => { radar.changeOffset += 100; return loadDetail(radar.run); });
action('preview-mail', async () => {
    const data = await request(`/runs/${radar.run}/email`);
    $('email-preview').hidden = Boolean(data.html);
    $('email-preview-html').hidden = !data.html;
    if (data.html) $('email-preview-html').srcdoc = data.html;
    else $('email-preview').textContent = data.subject + '\n\n' + data.body;
});
action('retry-mail', async () => {
    $('retry-mail').disabled = true;
    try { await request(`/runs/${radar.run}/email`, {method: 'POST'}); await loadDetail(radar.run); }
    finally { $('retry-mail').disabled = false; }
});
document.addEventListener('visibilitychange', () => { clearTimeout(radar.timer); if (!document.hidden) refresh().catch(error => message(error.message, true)); });
window.addEventListener('pagehide', () => clearTimeout(radar.timer));
(async () => {
    await refresh();
    const id = Number(new URLSearchParams(location.search).get('run'));
    if (Number.isSafeInteger(id) && id > 0) await loadDetail(id);
})().catch(error => message(error.message, true));
