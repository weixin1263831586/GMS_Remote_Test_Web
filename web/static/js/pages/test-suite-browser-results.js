// 客户端缓存：Worker + suitePath → { results, columns }，避免不同主机的同路径串数据。
const _testResultsCache = new Map();

function testResultsCacheKey(suitePath, suite = null) {
    const workerId = suite?.worker_id
        || _browserSuitesWorkerId
        || $('suite-worker-select')?.value
        || workspaceLocalWorkerId();
    return `${workerId}\u0000${suitePath || ''}`;
}

window.openTestResultsModal = function openTestResultsModal() {
    if (!state.suiteBrowser.selectedSuitePath) {
        showToast('请先选择一个测试套件', 'warning');
        return;
    }
    ModalManager.open('test-results-modal');
    const minimized = document.getElementById('test-results-minimized');
    if (minimized) minimized.style.display = 'none';
    // 若已有缓存则立即渲染，再后台静默刷新（后端缓存命中时几乎无延迟）。
    const suitePath = state.suiteBrowser.selectedSuitePath;
    const suite = _browserSuitesCache.find(item => item.tools_path === suitePath);
    const cached = _testResultsCache.get(testResultsCacheKey(suitePath, suite));
    if (cached) {
        renderTestResults(cached.results, cached.columns);
        const statusEl = $('test-results-modal-status');
        if (statusEl) statusEl.textContent = `共 ${cached.results.length} 条结果 · 点击行跳转目录 · 缓存`;
        loadTestResults(false, false);
    } else {
        loadTestResults(false);
    }
};

window.closeTestResultsModal = function closeTestResultsModal() {
    ModalManager.close('test-results-modal');
    const minimized = document.getElementById('test-results-minimized');
    if (minimized) minimized.style.display = 'none';
};

window.minimizeTestResultsModal = function minimizeTestResultsModal() {
    ModalManager.close('test-results-modal');
    const minimized = document.getElementById('test-results-minimized');
    const title = document.getElementById('test-results-minimized-title');
    if (title) {
        const suite = document.getElementById('test-results-modal-suite');
        title.textContent = suite ? suite.textContent.trim() : '';
    }
    if (minimized) minimized.style.display = 'flex';
};

window.restoreTestResultsModal = function restoreTestResultsModal() {
    const minimized = document.getElementById('test-results-minimized');
    if (minimized) minimized.style.display = 'none';
    ModalManager.open('test-results-modal');
};

async function loadTestResults(force = false, showSpinner = true) {
    const suitePath = state.suiteBrowser.selectedSuitePath;
    const suite = _browserSuitesCache.find(s => s.tools_path === suitePath);
    const cacheKey = testResultsCacheKey(suitePath, suite);
    const requestWorkerId = cacheKey.split('\u0000', 1)[0];
    const listEl = $('test-results-list');
    const statusEl = $('test-results-modal-status');
    const suiteLabelEl = $('test-results-modal-suite');

    if (suiteLabelEl && suite) {
        let displayType = suite.test_type || '';
        if (displayType === 'cts-verifier') displayType = 'cts-v';
        suiteLabelEl.textContent = `· ${displayType.toUpperCase()} ${getSuiteDisplayName(suite)}`;
    }

    if (!suitePath) {
        if (listEl) listEl.innerHTML = '<div style="padding: 20px; color: var(--text-secondary); text-align: center;">请先选择测试套件</div>';
        return;
    }

    if (showSpinner) {
        if (listEl) listEl.innerHTML = '<div style="padding: 20px; color: var(--text-secondary); text-align: center;">查询 tradefed list results 中...</div>';
        if (statusEl) statusEl.textContent = '正在执行 tradefed list results，可能需要数秒...';
    }

    try {
        // 不传 tradefed_bin：让后端 find_tradefed_binary 解析绝对路径。
        // suite.binary 只是裸文件名（如 vts-tradefed），cd 到 tools 后不在
        // PATH 中无法直接执行，会触发系统 "command not found" 建议而失败。
        const forceParam = force ? '?force_refresh=true' : '';
        const payload = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
            ? await apiCall(`/api/cluster/suites/results?${new URLSearchParams({
                worker_id: suite.worker_id, suite_path: suitePath
            })}`, 'POST')
            : await apiCall(`/api/test/suites/result${forceParam}`, 'POST', {suite_path: suitePath});
        if (!payload || !payload.success) {
            const msg = (payload && (payload.error || payload.message)) || '查询失败';
            if (listEl) listEl.innerHTML = `<div style="padding: 20px; color: var(--danger-color, #e53935); text-align: center;">查询失败: ${escapeHtml(msg)}</div>`;
            if (statusEl) statusEl.textContent = '查询失败';
            return;
        }
        const currentSuite = _browserSuitesCache.find(item => item.tools_path === suitePath);
        if (state.suiteBrowser.selectedSuitePath !== suitePath
                || testResultsCacheKey(suitePath, currentSuite).split('\u0000', 1)[0] !== requestWorkerId) {
            return;
        }
        renderTestResults(payload.results || [], payload.columns || []);
        _testResultsCache.set(cacheKey, { results: payload.results || [], columns: payload.columns || [] });
        const cacheTag = payload.cached ? ' · 缓存' : '';
        if (statusEl) statusEl.textContent = `共 ${payload.count || 0} 条结果 · 点击行跳转目录${cacheTag}`;
    } catch (error) {
        if (listEl) listEl.innerHTML = `<div style="padding: 20px; color: var(--danger-color, #e53935); text-align: center;">加载失败: ${escapeHtml(error.message || String(error))}</div>`;
        if (statusEl) statusEl.textContent = '加载失败';
    }
}

// 原始列名 → 字段渲染。不同套件列不同（CTS/GTS 多 Warning 列），按后端
// 返回的原始表头 columns 动态渲染，列名与 tradefed 输出完全一致。
const RESULT_COLUMN_RENDERERS = {
    'session': r => ({ text: escapeHtml(String(r.session ?? '-')) }),
    'pass': r => ({ text: escapeHtml(String(r.pass ?? '-')), style: 'text-align: right; color: var(--success-color, #43a047);' }),
    'fail': r => {
        const failNum = Number(r.fail) || 0;
        return { text: escapeHtml(String(r.fail ?? '-')), style: `text-align: right;${failNum > 0 ? ' color: var(--danger-color, #e53935); font-weight: 600;' : ''}` };
    },
    'warning': r => ({ text: escapeHtml(String(r.warning ?? '-')), style: 'text-align: right;' }),
    'modules complete': r => ({
        text: (r.modules || r.modules_total)
            ? `${escapeHtml(String(r.modules ?? '-'))}${r.modules_total ? ` of ${escapeHtml(String(r.modules_total))}` : ''}`
            : '<span style="color: var(--text-secondary);">-</span>',
    }),
    'result directory': r => ({
        text: r.result_directory ? `📁 ${escapeHtml(String(r.result_directory))}` : '<span style="color: var(--text-secondary);">-</span>',
    }),
    'test plan': r => ({ text: escapeHtml(String(r.test_plan ?? '-')) }),
    'device serial(s)': r => {
        const v = String(r.device_serial ?? '-');
        return {
            text: `<span title="${escapeHtml(v)}" style="display: inline-block; max-width: 120px; overflow: hidden; text-overflow: ellipsis; vertical-align: bottom;">${escapeHtml(v)}</span>`,
            style: 'padding: 4px 6px;',
        };
    },
    'build id': r => {
        const v = String(r.build_id ?? '-');
        return {
            text: `<span title="${escapeHtml(v)}" style="display: inline-block; max-width: 120px; overflow: hidden; text-overflow: ellipsis; vertical-align: bottom;">${escapeHtml(v)}</span>`,
            style: 'padding: 4px 6px;',
        };
    },
    'product': r => ({ text: escapeHtml(String(r.product ?? '-')) }),
    'project': r => {
        const project = String(r.project ?? '');
        if (!project) return { text: '<span style="color: var(--text-secondary);">-</span>' };
        const palette = ['#1e88e5', '#43a047', '#e53935', '#8e24aa', '#00897b', '#f4511e'];
        const hash = Array.from(project).reduce(
            (value, char) => ((value * 31) + char.charCodeAt(0)) >>> 0,
            0,
        );
        const color = palette[hash % palette.length];
        return {
            text: `<span style="background: ${color}22; color: ${color}; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 11px;">${escapeHtml(project)}</span>`,
        };
    },
};

function renderTestResults(results, columns) {
    const listEl = $('test-results-list');
    if (!listEl) return;

    if (!results.length) {
        listEl.innerHTML = '<div style="padding: 20px; color: var(--text-secondary); text-align: center;">暂无测试结果</div>';
        return;
    }

    // 若后端未返回表头，回退到默认列集（不含 Warning）。
    // 始终在 Product 后插入"项目"列以区分不同芯片平台。
    const _DEFAULT_COLS = ['Session', 'Pass', 'Fail', 'Modules Complete', 'Result Directory', 'Test Plan', 'Device serial(s)', 'Build ID', 'Product', 'Project'];
    let cols = (columns && columns.length)
        ? [...columns]
        : _DEFAULT_COLS;
    // 若后端列已有 Product 但没有 Project，在 Product 后插入。
    if (!cols.map(c => c.toLowerCase()).includes('project')) {
        const productIdx = cols.findIndex(c => c.toLowerCase() === 'product');
        if (productIdx >= 0) {
            cols.splice(productIdx + 1, 0, 'Project');
        } else {
            cols.push('Project');
        }
    }

    // 数值列（Pass/Fail/Warning）表头右对齐，与数据 text-align:right 保持一致，
    // 否则宽列里表头左对齐、数字右对齐会错位。
    const numericCols = new Set(['pass', 'fail', 'warning']);
    const headerCells = cols.map(name => {
        const align = numericCols.has(name.toLowerCase()) ? 'right' : 'left';
        return `<th style="padding: 8px; text-align: ${align}; white-space: nowrap;">${escapeHtml(name)}</th>`;
    }).join('');

    listEl.innerHTML = `
        <table style="width: 100%; border-collapse: collapse;">
            <thead style="position: sticky; top: 0; z-index: 1;">
                <tr style="background: var(--darker-bg); border-bottom: 1px solid var(--border-color); font-size: 12px;">${headerCells}</tr>
            </thead>
            <tbody id="test-results-tbody"></tbody>
        </table>
    `;

    const tbody = $('test-results-tbody');
    results.forEach(r => {
        const tr = document.createElement('tr');
        tr.style.cssText = 'border-bottom: 1px solid var(--border-color); cursor: pointer; font-size: 12px;';
        tr.onmouseenter = () => { tr.style.background = 'var(--hover-bg, rgba(0,0,0,0.04))'; };
        tr.onmouseleave = () => { tr.style.background = ''; };
        tr.title = r.result_directory ? `跳转到目录 results/${r.result_directory}` : '无结果目录';

        const cells = cols.map(name => {
            const renderer = RESULT_COLUMN_RENDERERS[name.toLowerCase()];
            const cell = renderer ? renderer(r) : { text: '' };
            const titleAttr = cell.title ? ` title="${escapeHtml(cell.title)}"` : '';
            // nowrap：每列单行显示，避免内容换行造成视觉错位。
            return `<td style="padding: 8px; white-space: nowrap; ${cell.style || ''}"${titleAttr}>${cell.text}</td>`;
        }).join('');
        tr.innerHTML = cells;

        tr.addEventListener('click', () => jumpToResultDirectory(r));
        tbody.appendChild(tr);
    });
}

async function jumpToResultDirectory(result) {
    const dir = result && result.result_directory;
    if (!dir) {
        showToast('该结果没有结果目录信息', 'warning');
        return;
    }
    // 结果目录位于套件根下的 results/<timestamp>，文件浏览器以套件根为相对根。
    const relPath = `results/${dir}`;
    closeTestResultsModal();
    // 先确保停留在当前选中套件，再跳转到结果目录并高亮。
    state.suiteBrowser.highlightPath = relPath;
    await loadSuiteBrowserDirectory(relPath);
    setSuiteBrowserHighlightedPath(relPath);
    showToast(`已跳转到 ${relPath}`, 'success');
}

