// Selected failure pane renderer for the Report Analysis workbench.
// Keep this independent of report parsing / upload handling.
/* Only the selected failure renders its full stack. This prevents large
   CTS/GTS reports from duplicating every stack trace in the list DOM. */
function selectReportFailure(index) {
    const failures = window.currentReportAnalysisData?.failures;
    if (!Array.isArray(failures) || !Number.isInteger(index) || index < 0 || index >= failures.length) {
        return;
    }
    const detail = $('report-failure-detail');
    const host = $('report-failure-detail-content');
    const headerActions = $('report-failure-header-actions');
    if (!detail || !host || !headerActions) return;
    const failure = failures[index];
    const moduleName = String(failure.module || '未知模块');
    const testName = String(failure.name || '未知用例');
    const reason = String(failure.reason || failure.stack_trace || '无失败原因');
    // ZIP 附件含 host_log 时展示原始 FAILURE 块（时间戳及 ModuleListener 前缀）。
    // 仅有 XML 时回退到 reason，绝不合成不存在的日志元数据。
    const displayLog = String(failure.raw_log || reason);
    document.querySelectorAll('#report-failure-list .report-failure-card').forEach((card, i) => {
        const selected = i === index;
        card.classList.toggle('selected', selected);
        const button = card.querySelector('.report-failure-select');
        if (button) button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    });
    host.replaceChildren();

    const title = document.createElement('div');
    title.className = 'report-detail-heading';
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.classList.add('ui-icon');
    icon.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '/static/icons/ui.svg#info');
    icon.appendChild(use);
    const caption = document.createElement('span');
    caption.textContent = testName;
    caption.title = testName;
    title.append(icon, caption);
    if (!failure.raw_log) {
        // XML archives frequently have the assertion but no host_log entry.
        // Show the limitation rather than presenting a made-up timestamp.
        const source = document.createElement('span');
        source.className = 'report-detail-source';
        const hostLogs = window.currentReportAnalysisData?.analysis_sources?.host_logs;
        const foundHostLogs = Array.isArray(hostLogs) && hostLogs.length > 0;
        source.textContent = foundHostLogs ? '已发现 host_log，但未匹配该用例' : '未发现原始 host_log';
        source.title = foundHostLogs
            ? '已扫描到主机日志，但当前用例名称或失败原因未匹配；请核对是否为同一次测试'
            : '上传包没有可识别的主机日志。可同时选择报告 ZIP/XML 和对应的 host_log 文件重新分析；XML 无法还原真实时间戳';
        title.appendChild(source);
    }

    const stack = document.createElement('div');
    stack.className = 'report-stack';
    stack.setAttribute('role', 'region');
    stack.setAttribute('aria-label', failure.raw_log ? '完整原始失败日志' : '完整报错堆栈');
    displayLog.replace(/\r\n?/g, '\n').split('\n').forEach((line, lineIndex) => {
        const row = document.createElement('div');
        row.className = 'report-stack-line';
        const number = document.createElement('span');
        number.className = 'report-stack-number';
        number.textContent = String(lineIndex + 1);
        const text = document.createElement('span');
        text.className = 'report-stack-text';
        text.textContent = line || ' ';
        row.append(number, text);
        stack.appendChild(row);
    });

    const actions = document.createDocumentFragment();
    const addAction = (label, kind, handler, args) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'report-failure-action ' + kind;
        button.textContent = label;
        button.setAttribute('data-click', handler);
        args.forEach((arg, i) => button.setAttribute('data-a' + i, String(arg)));
        actions.appendChild(button);
        return button;
    };
    const reportName = window.currentReportName || '';
    const issue = reportName.match(/^Redmine-(\d+)-/);
    if (issue) {
        const reply = addAction('Redmine回复', 'reply', 'openRedmineReplyModal',
            [moduleName, testName, index, issue[1]]);
        reply.setAttribute('data-reason', encodeURIComponent(reason));
    }
    addAction('单测用例', 'test', 'goToTestCase',
        [window.currentReportAnalysisData?.details?.test_type || '', moduleName, testName]);
    addAction('报错诊断', 'diagnose', 'openReportDiagnosisModal', [index]);
    host.append(title, stack);
    // Selected-case actions stay visible above the scrollable stack trace.
    headerActions.replaceChildren(actions);
    detail.hidden = false;
}

// Diagnosis recall panels: source evidence, historical cases and mechanism background.
function renderReportSourceCards(sourceResults) {
    const results = sourceResults || [];
    return results.length > 0
        ? results.map(item => {
            const itemUrl = safeReportExternalUrl(item.url);
            return `
            <div class="dx-list-item${itemUrl ? ' dx-clickable' : ''}" ${itemUrl ? `data-click="_actOpenUrlBlank" data-a0="${escapeJsAttr(itemUrl)}"` : ''}>
                <div class="dx-list-head">
                    <div class="dx-list-title">${escapeHtml(item.type || 'source')}</div>
                    ${itemUrl ? `<a class="dx-link" href="${escapeHtml(itemUrl)}" target="_blank" rel="noopener" data-click="_actStopPropagation">打开源码</a>` : ''}
                </div>
                <div class="dx-list-path dx-list-path-inline">${escapeHtml(item.path || item.display_path || '')}${item.line ? `<span>:${escapeHtml(String(item.line))}</span>` : ''}</div>
            </div>
        `;
        }).join('')
        : renderDxEmpty('未检索到源码结果');
}

function renderReportKbCards(kbResults) {
    const results = kbResults || [];
    return results.length > 0
        ? results.map(item => `
            <div class="dx-list-item">
                <div class="dx-list-title">#${escapeHtml(String(item.id || ''))} ${escapeHtml(item.subject || '')}</div>
                <div class="dx-list-meta">${escapeHtml(item.status_name || '')} | ${escapeHtml(item.updated_on || '')}</div>
                <div class="dx-list-text">${escapeHtml((item.solution_summary || item.description || '').slice(0, 260))}</div>
            </div>
        `).join('')
        : renderDxEmpty('未命中知识库');
}

/**
 * wiki section 行号锚点（schema v4 闭环）：`文件:L120-L168` + heading。
 * read model 的 background[].section 由后端归一（行号非法时为 null），
 * 这里只做展示拼装，不猜格式。
 */
function sectionAnchorHtml(item) {
    const section = item && item.section;
    if (!section || !section.start_line || !section.end_line) return '';
    const range = `L${section.start_line}-L${section.end_line}`;
    const heading = section.heading ? ` · ${String(section.heading).slice(0, 60)}` : '';
    return ` · ${escapeHtml(range)}${escapeHtml(heading)}`;
}

/**
 * 系统机制背景知识面板（ADR 0014）。background-only：
 * 展示机制解释与 provenance，明确标注未经过设备/源码证据验证。
 */
function renderReportSystemBackgroundPanel(backgroundResults) {
    const results = Array.isArray(backgroundResults) ? backgroundResults : [];
    if (results.length === 0) return '';
    const cards = results.map(item => {
        const meta = [
            item.applicable_versions ? `适用: ${item.applicable_versions}` : '',
            item.confidence ? `置信: ${item.confidence}` : '',
            item.last_verified ? `验证于: ${item.last_verified}` : '',
        ].filter(Boolean).join(' | ');
        const anchors = Array.isArray(item.source_anchors) ? item.source_anchors : [];
        // Wiki→codesearch 串联验证结论（内嵌在每条命中上，ADR 0014）。
        // 消费 evidence_level 封闭三态（后端唯一语义标签，见
        // anchor_verification.py——后端从不产出 verified 字段）：
        // path_matched=本地源码树找到同路径文件（绿）；
        // path_missing=codesearch 可用但未命中（红）；
        // unknown/无字段=验证器不可用或预算耗尽（灰）。
        const verifications = Array.isArray(item.anchor_verifications) ? item.anchor_verifications : [];
        // 回挂键与后端 (repo, path) 复合键一致：同 path 异 repo 不交叉绑定。
        const verifyByKey = new Map();
        verifications.forEach(v => {
            if (v && v.anchor) verifyByKey.set(`${v.anchor.repo || ''}/${v.anchor.path || ''}`, v);
        });
        const anchorHtml = anchors.length ? `
                <div class="dx-list-meta">锚点: ${anchors.map(a => {
                    const v = verifyByKey.get(`${a.repo || ''}/${a.path || ''}`);
                    const badge = v && v.evidence_level === 'path_matched'
                        ? '<span style="color:var(--success-color)">✔ 已在源码树找到同路径文件</span>'
                        : (v && v.evidence_level === 'path_missing'
                            ? '<span style="color:var(--danger-color)">✘ 源码树未命中</span>'
                            : '<span style="color:var(--text-secondary)">◌ 未验证</span>');
                    const label = [a.repo, a.path].filter(Boolean).join('/') || a.url || '';
                    return `<div>${badge} ${escapeHtml(label.slice(0, 120))}${a.revision ? ` @ ${escapeHtml(String(a.revision).slice(0, 16))}` : ''}</div>`;
                }).join('')}</div>` : '';
        return `
            <div class="dx-list-item">
                <div class="dx-list-head">
                    <div class="dx-list-title">${escapeHtml(item.title || '机制条目')}${item.chapter ? `<span class="dx-list-meta"> · ${escapeHtml(String(item.chapter))}</span>` : ''}</div>
                </div>
                <div class="dx-list-text">${escapeHtml((item.snippet || '').slice(0, 300))}</div>
                <div class="dx-list-meta">${escapeHtml(meta || 'provenance 缺失')}</div>
                <div class="dx-list-meta">来源: ${escapeHtml(item.source || 'android_internals')} @ ${escapeHtml((item.source_revision || '').slice(0, 8))} · ${escapeHtml(item.source_path || '')} · ${escapeHtml(item.license || '')}${sectionAnchorHtml(item)}</div>${anchorHtml}
            </div>
        `;
    }).join('');
    return `
        <section class="dx-section dx-background-section">
            <div class="dx-section-title">系统机制背景 <span>${results.length} 条</span></div>
            <div class="dx-list">${cards}</div>
            <div class="dx-list-meta dx-background-note">背景知识：解释系统机制，尚未通过设备/源码证据验证，不得作为已证实根因。</div>
        </section>
    `;
}
