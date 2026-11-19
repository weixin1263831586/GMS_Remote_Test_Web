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
