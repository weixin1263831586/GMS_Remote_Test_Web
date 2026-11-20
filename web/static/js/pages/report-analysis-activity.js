/* Persistent report progress shared by uploads, saved reports, suite logs and URL analysis.
   Loaded as a classic deferred script after report-analysis.js. */
// Persistent state for upload and server-side parsing.
let reportAnalysisActivityToken = 0;
let reportAnalysisActivityStarted = 0;
let reportAnalysisActivityTimer = null;
let currentReportAnalysisFetch = null;

function isReportAnalysisActivityCurrent(token) {
    return token === reportAnalysisActivityToken;
}

function updateReportAnalysisElapsed() {
    const clock = $('report-analysis-activity-time');
    if (!clock) return;
    const elapsed = Math.floor(Math.max(0, Date.now() - reportAnalysisActivityStarted) / 1000);
    const hours = Math.floor(elapsed / 3600);
    const minutes = String(Math.floor((elapsed % 3600) / 60)).padStart(2, '0');
    const seconds = String(elapsed % 60).padStart(2, '0');
    clock.textContent = '已等待 ' + (hours ? hours + ':' : '') + minutes + ':' + seconds;
}

function setReportAnalysisActivityPhase(token, title, detail, uploadedPercent = null) {
    if (!isReportAnalysisActivityCurrent(token)) return;
    const heading = $('report-analysis-activity-title');
    const description = $('report-analysis-activity-detail');
    if (heading) heading.textContent = title;
    if (description) description.textContent = detail;
    const fill = $('report-progress-fill');
    const progress = $('report-upload-progress');
    // Only show actual transfer progress.
    if (fill) fill.style.width = uploadedPercent === null ? '0%' : Math.round(uploadedPercent) + '%';
    if (progress) progress.style.opacity = uploadedPercent === null ? '0' : '1';
}

function beginReportAnalysisActivity(title, detail) {
    const token = ++reportAnalysisActivityToken;
    if (reportAnalysisActivityTimer !== null) clearInterval(reportAnalysisActivityTimer);
    // Supersede older requests without repainting their results.
    if (currentReportUploadRequest && currentReportUploadRequest.readyState !== 4) {
        reportUploadGeneration += 1;
        currentReportUploadRequest.abort();
        currentReportUploadRequest = null;
    }
    if (currentRedmineRequest) {
        currentRedmineRequest.abort();
        currentRedmineRequest = null;
    }
    if (currentReportAnalysisFetch) {
        currentReportAnalysisFetch.abort();
        currentReportAnalysisFetch = null;
    }
    const zone = $('report-upload-zone');
    const status = $('report-analysis-activity');
    const active = $('report-active-file');
    const result = $('report-analysis-result');
    if (active) active.hidden = true;
    if (result) result.style.display = 'none';
    if (zone) {
        zone.classList.add('upload-empty', 'report-analysis-busy');
        zone.classList.remove('report-analysis-error');
    }
    if (status) status.hidden = false;
    const cancel = zone?.querySelector('.report-analysis-cancel');
    if (cancel) {
        cancel.textContent = '取消等待';
        cancel.title = '停止等待并清除当前分析；已经在服务端运行的任务可能继续';
    }
    const progress = $('report-upload-progress');
    if (progress) progress.style.opacity = '0';
    reportAnalysisActivityStarted = Date.now();
    setReportAnalysisActivityPhase(token, title, detail);
    updateReportAnalysisElapsed();
    reportAnalysisActivityTimer = setInterval(updateReportAnalysisElapsed, 1000);
    return token;
}

function finishReportAnalysisActivity(token, error = '') {
    if (!isReportAnalysisActivityCurrent(token)) return false;
    if (reportAnalysisActivityTimer !== null) clearInterval(reportAnalysisActivityTimer);
    reportAnalysisActivityTimer = null;
    const zone = $('report-upload-zone');
    const status = $('report-analysis-activity');
    if (zone) zone.classList.remove('report-analysis-busy');
    if (error) {
        if (zone) zone.classList.add('report-analysis-error');
        setReportAnalysisActivityPhase(token, '报告分析失败', String(error));
        const cancel = zone?.querySelector('.report-analysis-cancel');
        if (cancel) {
            cancel.textContent = '返回上传';
            cancel.title = '返回初始上传区域，以便重新选择测试报告';
        }
        const time = $('report-analysis-activity-time');
        if (time) time.textContent = time.textContent.replace('已等待', '本次耗时');
    } else {
        if (zone) zone.classList.remove('report-analysis-error');
        if (status) status.hidden = true;
    }
    resetReportUploadProgress();
    return true;
}

function clearReportAnalysisActivity() {
    ++reportAnalysisActivityToken;
    if (currentReportAnalysisFetch) {
        currentReportAnalysisFetch.abort();
        currentReportAnalysisFetch = null;
    }
    if (reportAnalysisActivityTimer !== null) clearInterval(reportAnalysisActivityTimer);
    reportAnalysisActivityTimer = null;
    const zone = $('report-upload-zone');
    if (zone) zone.classList.remove('report-analysis-busy', 'report-analysis-error');
    const status = $('report-analysis-activity');
    if (status) status.hidden = true;
}
