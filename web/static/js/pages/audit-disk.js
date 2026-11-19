// Read-only runtime artifact inventory in Security Audit.
// Never call /api/ops/disk/cleanup; no backend path parameters from the user.
const diskArtifactAuditState = {
    loaded: false,
    loading: false,
    requestGeneration: 0,
    items: [],
    selectedId: '',
    attentionOnly: false,
    sizeSort: '',
};

function formatDiskArtifactBytes(value) {
    if (value === null || value === undefined || value === '') return '-';
    const count = Number(value);
    if (!Number.isFinite(count) || count < 0) return '-';
    if (count === 0) return '0 B';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    const scale = Math.min(Math.floor(Math.log(count) / Math.log(1024)), units.length - 1);
    return (count / Math.pow(1024, scale)).toFixed(scale === 0 ? 0 : 1) + ' ' + units[scale];
}

function diskAuditStatusText(item) {
    if (item.status === 'unavailable') return '未接入';
    if (item.status === 'partial' || !item.complete) return '不完整';
    return item.present ? '已扫描' : '未产生';
}

function setDiskAuditRefreshState(loading, message) {
    const button = document.getElementById('audit-disk-refresh-btn');
    const label = document.getElementById('audit-disk-refresh-label');
    const text = document.getElementById('audit-disk-scanned-at');
    if (button) {
        button.disabled = loading;
        button.setAttribute('aria-busy', String(loading));
        button.classList.toggle('audit-disk-refreshing', loading);
    }
    if (label) label.textContent = loading ? '扫描中…' : '刷新';
    if (text && message) text.textContent = message;
}

function selectDiskArtifact(id) {
    const item = diskArtifactAuditState.items.find(entry => entry.id === id);
    const detail = $('audit-disk-detail');
    if (!item || !detail) return;
    diskArtifactAuditState.selectedId = id;
    document.querySelectorAll('#audit-disk-table-body tr[data-artifact-id]').forEach(row => {
        row.classList.toggle('selected', row.dataset.artifactId === id);
        row.setAttribute('aria-selected', String(row.dataset.artifactId === id));
    });
    const fields = [
        ['产物类型', item.name],
        ['扫描范围', item.scope],
        ['已统计大小', formatDiskArtifactBytes(item.bytes)],
        ['超龄参考', formatDiskArtifactBytes(item.aged_bytes) + ' / ' +
            Number(item.aged_files || 0).toLocaleString('zh-CN') + ' 个文件'],
        ['7 / 30 / 90 天未修改', [item.older_7d_bytes, item.older_30d_bytes,
            item.older_90d_bytes].map(formatDiskArtifactBytes).join(' / ')],
        ['文件 / 目录', Number(item.files || 0).toLocaleString('zh-CN') + ' / ' +
            Number(item.directories || 0).toLocaleString('zh-CN')],
        ['清理机制', item.cleanup],
        ['参考阈值', Number(item.age_days || 0) + ' 天'],
        ['审计状态', diskAuditStatusText(item)],
    ];
    const facts = fields.map(([label, value]) =>
        '<div><span>' + escapeHtml(label) + '</span><strong title="' +
        escapeJsAttr(value) + '">' + escapeHtml(value) + '</strong></div>'
    ).join('');

    const breakdown = Array.isArray(item.breakdown) ? item.breakdown : [];
    const fileTypeFacts = breakdown.length ?
        '<div class="audit-disk-breakdown-title">文件类型占用（前 6 项）</div>' +
        breakdown.slice(0, 6).map(entry => '<div><span>' +
            escapeHtml(entry.type) + ' · ' + Number(entry.files || 0).toLocaleString('zh-CN') +
            ' 个</span><strong>' + escapeHtml(formatDiskArtifactBytes(entry.bytes)) +
            '</strong></div>').join('') : '';
    detail.innerHTML = facts + fileTypeFacts +
        '<p class="audit-disk-description">' + escapeHtml(item.description || '') +
        (item.note ? '<span> · ' + escapeHtml(item.note) + '</span>' : '') + '</p>';
}

function showDiskArtifactAuditGate(tbody) {
    // Administrator disk statistics must not remain visible or cached after
    // elevation expiry, even if a previous scan completed successfully.
    diskArtifactAuditState.loaded = false;
    diskArtifactAuditState.loading = false;
    diskArtifactAuditState.items = [];
    diskArtifactAuditState.selectedId = '';
    diskArtifactAuditState.attentionOnly = false;
    updateDiskAttentionCard();
    for (const id of [
        'audit-disk-bytes', 'audit-disk-attention', 'audit-disk-covered',
        'audit-disk-aged', 'audit-disk-free', 'audit-disk-count',
        'audit-disk-controller-bytes', 'audit-disk-worker-bytes',
        'audit-disk-disk-used', 'audit-disk-scan-cost', 'audit-disk-visited',
    ]) {
        const node = $(id);
        if (node) node.textContent = '-';
    }
    const detail = $('audit-disk-detail');
    if (detail) {
        detail.innerHTML = '<div><span>提示</span><strong>请先完成管理员提权</strong></div>';
    }
    setDiskAuditRefreshState(false, '需要管理员提权');
    tbody.setAttribute('aria-busy', 'false');
    tbody.innerHTML = diskArtifactAuditGateHtml();
}

function updateDiskAttentionCard() {
    const card = $('audit-disk-attention-card');
    if (!card) return;
    const active = diskArtifactAuditState.attentionOnly;
    card.setAttribute('aria-pressed', String(active));
    card.setAttribute('aria-label', active
        ? '取消重点关注筛选，显示全部产物'
        : '筛选重点关注：仅显示高关注且存在文件的分类');
    card.title = active ? '点击恢复全部产物' : '点击只显示高关注且存在文件的分类';
}

function toggleDiskAttentionFilter() {
    if (!diskArtifactAuditState.loaded) return;
    diskArtifactAuditState.attentionOnly = !diskArtifactAuditState.attentionOnly;
    // Reset other conditions so the rows match the KPI count exactly.
    for (const id of ['audit-disk-group', 'audit-disk-priority', 'audit-disk-search']) {
        const control = $(id);
        if (control) control.value = '';
    }
    updateDiskAttentionCard();
    filterDiskArtifacts();
}

function filterDiskArtifactsByControls() {
    diskArtifactAuditState.attentionOnly = false;
    updateDiskAttentionCard();
    filterDiskArtifacts();
}

function toggleDiskArtifactSizeSort() {
    diskArtifactAuditState.sizeSort = diskArtifactAuditState.sizeSort === 'desc'
        ? 'asc' : 'desc';
    const header = $('audit-disk-size-heading');
    const indicator = $('audit-disk-sort-indicator');
    const button = $('audit-disk-size-sort');
    const desc = diskArtifactAuditState.sizeSort === 'desc';
    if (header) header.setAttribute('aria-sort', desc ? 'descending' : 'ascending');
    if (indicator) indicator.textContent = desc ? '↓' : '↑';
    if (button) {
        button.setAttribute('aria-label', desc
            ? '当前按已统计大小降序排列，点击改为升序'
            : '当前按已统计大小升序排列，点击改为降序');
        button.title = desc ? '从大到小，点击改为从小到大'
            : '从小到大，点击改为从大到小';
    }
    filterDiskArtifacts();
}

function filterDiskArtifacts() {
    const tbody = $('audit-disk-table-body');
    if (!tbody || !diskArtifactAuditState.loaded) return;
    const group = $('audit-disk-group')?.value || '';
    const priority = $('audit-disk-priority')?.value || '';
    const query = String($('audit-disk-search')?.value || '').trim().toLowerCase();
    const filtered = diskArtifactAuditState.items.filter(item => {
        if (diskArtifactAuditState.attentionOnly &&
            !(item.priority === '高' && Number(item.bytes || 0) > 0)) return false;
        if (group && item.group !== group) return false;
        if (priority && item.priority !== priority) return false;
        return !query || [item.name, item.location, item.description, item.cleanup]
            .some(value => String(value || '').toLowerCase().includes(query));
    });
    if (diskArtifactAuditState.sizeSort) {
        const direction = diskArtifactAuditState.sizeSort === 'desc' ? -1 : 1;
        filtered.sort((a, b) => direction * (Number(a.bytes || 0) - Number(b.bytes || 0)));
    }
    const count = $('audit-disk-count');
    if (count) count.textContent = filtered.length + ' / ' +
        diskArtifactAuditState.items.length + ' 类';
    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="table-placeholder">当前筛选条件下没有匹配的产物分类</td></tr>';
        $('audit-disk-detail').innerHTML = '<div><span>提示</span><strong>无匹配分类</strong></div>';
        return;
    }
    tbody.innerHTML = filtered.map(item => {
        const stateText = diskAuditStatusText(item);
        const stateTone = item.complete && item.status !== 'unavailable' ? 'success' : 'warning';
        const label = item.priority === '高' ? 'high' : item.priority === '中' ? 'medium' : 'low';
        const hover = [
            item.name, item.description, item.note,
            '参考超龄：' + Number(item.aged_files || 0) + ' 个文件',
            '文件年龄不意味着可以删除'
        ].filter(Boolean).join('\n');
        return '<tr class="audit-disk-row" tabindex="0" role="row" data-artifact-id="' +
            escapeJsAttr(item.id) + '" title="' + escapeJsAttr(hover) + '">' +
            '<td title="' + escapeJsAttr(item.name) + '"><span class="audit-disk-priority ' +
            label + '">' + escapeHtml(item.priority) + '</span> ' + escapeHtml(item.name) + '</td>' +
            '<td title="' + escapeJsAttr(item.location) +
            '" class="audit-disk-location">' + escapeHtml(item.location) + '</td>' +
            '<td class="audit-disk-number">' + escapeHtml(formatDiskArtifactBytes(item.bytes)) + '</td>' +
            '<td class="audit-disk-number">' +
            Number(item.files || 0).toLocaleString('zh-CN') + '</td>' +
            '<td title="' + escapeJsAttr(item.description || '') + '">' +
            escapeHtml(item.cleanup) + '</td>' +
            '<td><span class="audit-status-badge audit-status-' +
            stateTone + '">' + stateText + '</span></td></tr>';
    }).join('');
    tbody.querySelectorAll('tr[data-artifact-id]').forEach(row => {
        const select = () => selectDiskArtifact(row.dataset.artifactId);
        row.addEventListener('click', select);
        row.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                select();
            }
        });
    });
    const selected = filtered.find(item => item.id === diskArtifactAuditState.selectedId);
    selectDiskArtifact((selected || filtered[0]).id);
}

async function loadDiskArtifactAudit(force = false) {
    const tbody = $('audit-disk-table-body');
    if (!tbody) return false;
    if (state.authRequired && !state.elevated) {
        ++diskArtifactAuditState.requestGeneration;
        showDiskArtifactAuditGate(tbody);
        return false;
    }
    // Do not queue multiple expensive half-million-file scans on rapid clicks.
    if (diskArtifactAuditState.loading) return false;
    const generation = ++diskArtifactAuditState.requestGeneration;
    const alreadyLoaded = diskArtifactAuditState.loaded;
    diskArtifactAuditState.loading = true;
    tbody.setAttribute('aria-busy', 'true');
    setDiskAuditRefreshState(true, '正在读取磁盘目录，请勿重复点击…');
    if (!alreadyLoaded) {
        tbody.innerHTML = '<tr><td colspan="6" class="table-placeholder">正在只读扫描本机 Controller 与 Worker 产物...</td></tr>';
    }
    try {
        // refresh=true bypasses the server-side 60-second snapshot cache.
        // Cache-busting timestamp prevents stale GET responses in browsers/proxies.
        const url = '/api/security-audit/disk-artifacts?refresh=' +
            (force ? 'true' : 'false') + '&_=' + Date.now();
        const response = await apiCall(url);
        if (generation !== diskArtifactAuditState.requestGeneration) return false;
        const data = response?.data || {};
        if (!Array.isArray(data.items) || !data.summary) {
            throw new Error('返回数据缺少产物分类或统计摘要');
        }
        diskArtifactAuditState.items = data.items;
        diskArtifactAuditState.loaded = true;
        const summary = data.summary;
        const setValue = (id, value) => {
            const node = $(id);
            if (node) node.textContent = value;
        };
        setValue('audit-disk-bytes', formatDiskArtifactBytes(summary.measured_bytes));
        setValue('audit-disk-attention',
            Number(summary.attention_categories || 0).toLocaleString('zh-CN'));
        setValue('audit-disk-covered',
            Number(summary.scanned_categories || 0).toLocaleString('zh-CN') + ' / ' +
            data.items.length + ' 类');
        setValue('audit-disk-aged', formatDiskArtifactBytes(summary.aged_bytes));
        setValue('audit-disk-free', formatDiskArtifactBytes(summary.disk?.free_bytes));
        setValue('audit-disk-disk-used', formatDiskArtifactBytes(summary.disk?.used_bytes));
        setValue('audit-disk-controller-bytes',
            formatDiskArtifactBytes(summary.controller_data_bytes));
        setValue('audit-disk-worker-bytes',
            formatDiskArtifactBytes(summary.local_worker_bytes));
        setValue('audit-disk-scan-cost',
            Number(summary.scan_seconds || 0).toFixed(1) + ' 秒');
        setValue('audit-disk-visited',
            Number(summary.visited_entries || 0).toLocaleString('zh-CN') + ' 项');
        const localeTime = data.generated_at ?
            new Date(data.generated_at).toLocaleString('zh-CN', {hour12: false}) : '未知时间';
        const prefix = data.cache_hit ? '缓存快照' : '实盘扫描';
        const partial = Number(summary.incomplete_categories || 0);
        setDiskAuditRefreshState(false, prefix + ' · ' + localeTime +
            (partial ? ' · ' + partial + ' 类不完整' : ' · 全部完整'));
        filterDiskArtifacts();
        if (alreadyLoaded && force) {
            showToast('磁盘统计已重新扫描（' +
                Number(summary.scan_seconds || 0).toFixed(1) + ' 秒）', 'success');
        }
        return true;
    } catch (error) {
        if (generation !== diskArtifactAuditState.requestGeneration) return false;
        setDiskAuditRefreshState(false, '刷新失败 · ' + (error?.message || '未知错误'));
        if (Number(error?.status) === 403) {
            showDiskArtifactAuditGate(tbody);
        } else if (!diskArtifactAuditState.loaded) {
            tbody.innerHTML = '<tr><td colspan="6" class="table-placeholder">磁盘审计读取失败：' +
                escapeHtml(error?.message || '未知错误') + '</td></tr>';
        } else {
            showToast('磁盘审计刷新失败：' + (error?.message || '未知错误'), 'error');
        }
        return false;
    } finally {
        if (generation === diskArtifactAuditState.requestGeneration) {
            diskArtifactAuditState.loading = false;
            tbody.setAttribute('aria-busy', 'false');
            setDiskAuditRefreshState(false);
        }
    }
}

window.switchSecurityAuditView = switchSecurityAuditView;
window.elevateDiskArtifactAudit = elevateDiskArtifactAudit;
window.filterDiskArtifacts = filterDiskArtifacts;
window.filterDiskArtifactsByControls = filterDiskArtifactsByControls;
window.toggleDiskAttentionFilter = toggleDiskAttentionFilter;
window.toggleDiskArtifactSizeSort = toggleDiskArtifactSizeSort;
window.loadDiskArtifactAudit = loadDiskArtifactAudit;
