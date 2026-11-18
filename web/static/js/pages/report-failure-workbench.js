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
    const reason = String(failure.reason || '无失败原因');
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

    const fields = document.createElement('div');
    fields.className = 'report-detail-fields';
    for (const [label, value, kind] of [
        ['模块', moduleName, 'module'], ['用例', testName, 'case'], ['状态', '失败', 'status']
    ]) {
        const field = document.createElement('div');
        field.className = 'report-detail-field ' + kind;
        const key = document.createElement('span');
        key.className = 'report-detail-label';
        key.textContent = label;
        const val = document.createElement('span');
        val.className = 'report-detail-value';
        val.textContent = value;
        val.title = value;
        field.append(key, val);
        fields.appendChild(field);
    }

    const stack = document.createElement('div');
    stack.className = 'report-stack';
    stack.setAttribute('role', 'region');
    stack.setAttribute('aria-label', '完整报错堆栈');
    reason.replace(/\r\n?/g, '\n').split('\n').forEach((line, lineIndex) => {
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
    host.append(title, fields, stack);
    // Selected-case actions stay visible above the scrollable stack trace.
    headerActions.replaceChildren(actions);
    detail.hidden = false;
}
