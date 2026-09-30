// Automation page workflow chunk; loaded by page.html in dependency order.

function renderSuiteOptions(preferredSuite = '') {
    const select = qs('automation-test-suite');
    const grouped = {};
    testSuites.forEach(suite => {
        const groupType = String(suite.test_type || '').trim().toUpperCase();
        if (!groupType) return;
        if (!grouped[groupType]) grouped[groupType] = [];
        grouped[groupType].push(suite);
    });
    const groups = Object.keys(grouped).sort().map(type => {
        const options = grouped[type].sort(compareSuitesNewest).map(suite => {
            const path = suite.tools_path || suite.full_path || '';
            return `<option value="${esc(path)}">${esc(path)}</option>`;
        }).join('');
        return `<optgroup label="${esc(type)}">${options}</optgroup>`;
    }).join('');
    select.innerHTML = groups || '<option value="" disabled>当前 Worker 暂无测试套件</option>';

    const preferred = String(preferredSuite || '');
    const hasPreferred = preferred && Array.from(select.options).some(option => option.value === preferred);
    if (hasPreferred) {
        select.value = preferred;
    } else {
        const suiteType = suiteTypeForTest(qs('automation-test-type').value);
        const latest = testSuites
            .filter(suite => String(suite.test_type || '').trim().toUpperCase() === suiteType)
            .sort(compareSuitesNewest)[0];
        select.value = latest ? (latest.tools_path || latest.full_path || '') : '';
    }
    invalidateRunPreflight();
    syncAutomationWorkspaceSelection();
    updateStepIndicators();
}
