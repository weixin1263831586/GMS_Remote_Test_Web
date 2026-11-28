function safeReportExternalUrl(value) {
    try {
        const parsed = new URL(String(value || ''), window.location.origin);
        return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '';
    } catch (error) {
        return '';
    }
}

function selectReportSource() {
    const modal = document.createElement('div');
    modal.id = 'report-source-modal';
    modal.className = 'modal';
    modal.style.cssText = 'z-index: 10000;';
    modal.innerHTML = `
        <div class="modal-content modal-xs tech-dialog tech-dialog--report-source"><svg class="tech-frame" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M16 1H986L999 14V584L984 599H16L1 584V16Z" vector-effect="non-scaling-stroke"/></svg><span class="tech-corner-mark tech-corner-mark--tl" aria-hidden="true"></span><span class="tech-corner-mark tech-corner-mark--br" aria-hidden="true"></span>
            <div class="modal-header tech-dialog-header"><svg class="tech-header-rule" viewBox="0 0 1000 16" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M0 15H490L506 1H916" vector-effect="non-scaling-stroke"/></svg>
                <div class="tech-title-group"><span class="tech-title-glyph" aria-hidden="true"><svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#upload"></use></svg></span><span class="tech-title-divider" aria-hidden="true"></span><span class="modal-title">选择上传方式</span></div>
                <button type="button" class="modal-close" aria-label="关闭" data-click="closeReportSourceModal">&times;</button>
            </div>
            <div class="modal-body">
                <p class="tech-intro">导入 Tradefed 测试结果，解析后进入报告分析。</p>
                <div class="report-import-choices">
                    <button type="button" class="report-import-choice" data-click="selectReportFile">
                        <svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#upload"></use></svg><strong>上传报告文件</strong>
                        <span>选择 XML 或 ZIP / RAR / TAR.GZ 压缩包</span>
                    </button>
                    <button type="button" class="report-import-choice" data-click="selectReportFolder">
                        <span class="report-choice-folder-icon" aria-hidden="true">📁</span><strong>上传报告文件夹</strong>
                        <span>选择包含 test_result.xml 的结果目录</span>
                    </button>
                </div>
                <div class="tech-notice"><svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#info"></use></svg>保留原始报告结构，系统自动识别测试类型和用例结果。</div>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    ModalManager.registerDynamic(modal);

    // 点击背景关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeReportSourceModal();
        }
    });
}

function closeReportSourceModal() {
    ModalManager.unregisterDynamic('report-source-modal');
}

function selectReportFile() {
    closeReportSourceModal();
    document.getElementById('report-file-input').click();
}

function selectReportFolder() {
    closeReportSourceModal();
    document.getElementById('report-folder-input').click();
}

async function handleReportDataTransfer(dataTransfer) {
    if (!dataTransfer) return;

    // 检查是否有 URL（从网页拖拽，如 Redmine 附件）
    const url = dataTransfer.getData('URL') || dataTransfer.getData('text/uri-list');
    if (url) {
        debugLog('[Report Analysis] Detected URL drop:', url);
        const dropContext = extractRedmineDropContext(dataTransfer, url);
        await handleRedmineAttachment(url, dropContext);
        return;
    }

    const items = dataTransfer.items;

    // 如果有 items，尝试使用 DataTransferItem API（支持文件夹）
    if (items && items.length > 0) {
        const files = [];

        const readFileEntries = async (entries) => {
            for (const entry of entries) {
                if (entry.isFile) {
                    await new Promise((resolve) => {
                        entry.file((file) => {
                            // 保留相对路径
                            Object.defineProperty(file, 'webkitRelativePath', {
                                value: (entry.fullPath || '').replace(/^\//, ''),
                                writable: false
                            });
                            files.push(file);
                            resolve();
                        });
                    });
                } else if (entry.isDirectory) {
                    const reader = entry.createReader();
                    // readEntries 可能需要多次调用才能读取所有条目
                    let allEntries = [];
                    while (true) {
                        const batch = await new Promise((resolve) => {
                            reader.readEntries(resolve);
                        });
                        if (batch.length === 0) break;
                        allEntries.push(...batch);
                    }
                    await readFileEntries(allEntries);
                }
            }
        };

        const itemEntries = [];
        for (let i = 0; i < items.length; i++) {
            const item = items[i];
            if (item.kind === 'file') {
                const entry = item.webkitGetAsEntry?.();
                if (entry) {
                    itemEntries.push(entry);
                }
            }
        }

        if (itemEntries.length > 0) {
            await readFileEntries(itemEntries);

            if (files.length === 0) {
                showToast('未找到可上传的文件', 'warning');
                return;
            }

            if (files.length === 1 && !files[0].webkitRelativePath.includes('/')) {
                handleReportFile(files[0]);
            } else {
                handleReportFolder(files);
            }
            return;
        }
    }

    // 不支持目录条目 API 时使用 files 属性。
    const files = dataTransfer.files;
    if (files.length > 0) {
        if (files.length === 1) {
            handleReportFile(files[0]);
        } else {
            handleReportFolder(files);
        }
    }
}

function initReportAnalysis() {
    const uploadZone = $('report-upload-zone');
    const fileInput = $('report-file-input');
    const folderInput = $('report-folder-input');

    if (!uploadZone || !fileInput || !folderInput) return;

    // 初始化时添加上传空状态类（占满屏幕）
    uploadZone.classList.add('upload-empty');

    uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadZone.classList.add('drag-over');
    });

    uploadZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');
    });

    uploadZone.addEventListener('drop', async (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');
        await handleReportDataTransfer(e.dataTransfer);
    });

    fileInput.addEventListener('change', async (e) => {
        const selected = Array.from(e.target.files || []);
        e.target.value = '';
        if (selected.length === 1) await handleReportFile(selected[0]);
        else if (selected.length > 1) await handleReportFolder(selected);
    });

    folderInput.addEventListener('change', async (e) => {
        if (e.target.files.length > 0) {
            await handleReportFolder(e.target.files);
            e.target.value = '';
        }
    });
}

// 用于取消正在进行的请求
let currentRedmineRequest = null;
let currentReportUploadRequest = null;
let reportUploadGeneration = 0;

function extractRedmineDropContext(dataTransfer, url) {
    const candidates = [
        url,
        dataTransfer?.getData('text/plain') || '',
        dataTransfer?.getData('text/html') || '',
        dataTransfer?.getData('text/uri-list') || ''
    ];
    const issueMatch = candidates.join('\n').match(/\/issues\/(\d+)/);
    if (!issueMatch) return {};
    return {
        source_issue_id: issueMatch[1],
        source_issue_url: candidates.find(value => value.includes(`/issues/${issueMatch[1]}`)) || ''
    };
}

async function handleRedmineAttachment(url, context = {}) {
    const originalUrl = url;
    const activity = beginReportAnalysisActivity(
        '正在获取报告附件', '正在验证下载地址，随后由服务器解析报告'
    );
    const controller = new AbortController();
    currentRedmineRequest = controller;
    const signal = controller.signal;

    try {
        const redmineConfig = await getRedmineConfig();
        if (!isReportAnalysisActivityCurrent(activity)) return;
        const redmineDomain = redmineConfig.domain;
        const redmineBaseUrl = redmineConfig.base_url || `https://${redmineDomain}`;
        const redminePathUrl = /\/(?:issues|attachments)(?:\/|$)/.test(url);
        const isRedmineUrl = url.includes(redmineDomain);

        if (redminePathUrl && !isRedmineUrl) {
            const publicUrl = url.replace(/^https?:\/\/[^/]+/, redmineBaseUrl.replace(/\/$/, ''));
            const message = '请使用公网 Redmine 地址：' + publicUrl;
            finishReportAnalysisActivity(activity, message);
            notifyOperationResult('报告分析失败', message, 'warning', 'report-analysis',
                { source: 'url' });
            return;
        }

        if (isRedmineUrl) {
            const attachmentMatch = url.match(/\/attachments\/(\d+)/);
            const issueMatch = url.match(/\/issues\/(\d+)/);
            if (issueMatch && !attachmentMatch) {
                setReportAnalysisActivityPhase(activity,
                    '正在获取 Redmine 附件信息', '正在查找问题单关联的测试报告');
                const extractResponse = await fetch('/api/reports/extract-redmine-attachment', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ issue_url: url }),
                    signal
                });
                if (!isReportAnalysisActivityCurrent(activity)) return;
                const extractResult = await extractResponse.json();
                if (!extractResponse.ok || !extractResult.success || !extractResult.attachment_url) {
                    throw new Error(extractResult.error || '无法提取附件');
                }
                url = extractResult.attachment_url;
                context.source_issue_id = context.source_issue_id || issueMatch[1];
                context.source_issue_url = context.source_issue_url || originalUrl;
            }
        }

        setReportAnalysisActivityPhase(activity, '服务器正在下载并解析报告',
            '下载、解压与测试结果统计在服务端执行；大报告可能耗时较长');
        const response = await fetch('/api/reports/analyze-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(isRedmineUrl
                ? {
                    url,
                    source_issue_id: context.source_issue_id || '',
                    source_issue_url: context.source_issue_url || '',
                    use_redmine_auth: true
                }
                : { url }),
            signal
        });
        if (!isReportAnalysisActivityCurrent(activity)) return;
        const result = await response.json();
        if (!isReportAnalysisActivityCurrent(activity)) return;
        if (response.ok && result.success) {
            displayReportAnalysis(result.data);
            finishReportAnalysisActivity(activity);
            notifyOperationResult('报告分析完成', result.filename || '附件分析完成',
                'success', 'report-analysis', { source: 'url', filename: result.filename || '' });
        } else if (result.requires_auth && isRedmineUrl) {
            finishReportAnalysisActivity(activity);
            const zone = $('report-upload-zone');
            showRedmineAuthDialog(url, zone, zone?.querySelector('.report-upload-content'),
                $('report-upload-progress'), $('report-progress-fill'), context);
        } else {
            const reason = result.error || result.message || '服务端报告解析失败';
            finishReportAnalysisActivity(activity, reason);
            notifyOperationResult('报告分析失败', reason, 'error', 'report-analysis',
                { source: 'url' });
        }
    } catch (error) {
        if (!isReportAnalysisActivityCurrent(activity) || error?.name === 'AbortError') return;
        console.error('URL attachment analysis error:', error);
        finishReportAnalysisActivity(activity, error.message || '请求失败');
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis',
            { source: 'url' });
    } finally {
        if (currentRedmineRequest === controller) currentRedmineRequest = null;
    }
}

function showRedmineAuthDialog(url, uploadZone, content, progress, progressFill, context = {}) {
    window._pendingRedmineDropContext=context||{};
    const escapedUrl=escapeJsAttr(url);
    ModalManager.unregisterDynamic('redmine-auth-modal');
    const modal=document.createElement('div');
    modal.id='redmine-auth-modal';
    modal.className = 'modal';
    modal.style.cssText = 'z-index: 10000;';
    modal.innerHTML = `
        <div class="modal-content modal-xs tech-dialog tech-dialog--redmine-auth"><svg class="tech-frame" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M16 1H986L999 14V584L984 599H16L1 584V16Z" vector-effect="non-scaling-stroke"/></svg><span class="tech-corner-mark tech-corner-mark--tl" aria-hidden="true"></span><span class="tech-corner-mark tech-corner-mark--br" aria-hidden="true"></span>
            <div class="modal-header tech-dialog-header"><svg class="tech-header-rule" viewBox="0 0 1000 16" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M0 15H490L506 1H916" vector-effect="non-scaling-stroke"/></svg>
                <div class="tech-title-group"><span class="tech-title-glyph" aria-hidden="true"><svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#upload"></use></svg></span><span class="tech-title-divider" aria-hidden="true"></span><span class="modal-title">Redmine 认证</span></div>
                <button type="button" class="modal-close" aria-label="关闭" data-click="_actCloseRedmineAuthModal">&times;</button>
            </div>
            <div class="modal-body">
                <p style="margin-bottom: 15px;">请输入 Redmine 账号密码以自动下载附件：</p>
                <form data-submit="submitRedmineAuth" data-a0="${escapedUrl}" data-prevent autocomplete="off">
                <div class="modal-form-row">
                    <label>用户名</label>
                    <input type="text" id="redmine-username" placeholder="输入 Redmine 用户名" autocomplete="username">
                </div>
                <div class="modal-form-row">
                    <label>密码</label>
                    <input type="password" id="redmine-password" placeholder="输入 Redmine 密码" autocomplete="current-password"
                           data-keypress="submitRedmineAuth" data-a0="${escapedUrl}" data-key="Enter" data-prevent>
                </div>
                </form>
                <div class="modal-buttons tech-dialog-actions"><span class="tech-footer-decoration" aria-hidden="true"></span>
                    <button class="btn-xs" data-click="_actCloseRedmineAuthModal">取消</button>
                    <button class="btn-xs btn-primary" data-click="submitRedmineAuth" data-a0="${escapedUrl}">确定</button>
                </div>
                <p class="report-redmine-credential-note" style="color: var(--text-secondary); margin-top: 15px; text-align: center;">
                    💾 凭证将被加密存储，下次无需重新输入
                </p>
            </div>
        </div>
    `;
    ModalManager.registerDynamic(modal);

    // 聚焦到用户名输入框
    setTimeout(() => {
        const usernameInput = document.getElementById('redmine-username');
        if (usernameInput) usernameInput.focus();
    }, 100);
}

function resetReportUploadProgress() {
    const uploadZone = $('report-upload-zone');
    const content = uploadZone?.querySelector('.report-upload-content');
    const progress = $('report-upload-progress');
    const progressFill = $('report-progress-fill');

    if (progress) progress.style.opacity = '0';
    if (progressFill) progressFill.style.width = '0%';
    if (content) content.style.opacity = '1';
}

async function submitRedmineAuth(url) {
    const username = document.getElementById('redmine-username')?.value;
    const password = document.getElementById('redmine-password')?.value;
    if (!username || !password) {
        showToast('请输入用户名和密码', 'warning');
        return;
    }

    ModalManager.unregisterDynamic('redmine-auth-modal');
    const activity = beginReportAnalysisActivity(
        '服务器正在下载并解析 Redmine 报告',
        '已提交认证信息，正在服务端下载、解压和统计测试结果'
    );
    const controller = new AbortController();
    currentRedmineRequest = controller;
    try {
        const response = await fetch('/api/reports/analyze-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url,
                source_issue_id: window._pendingRedmineDropContext?.source_issue_id || '',
                source_issue_url: window._pendingRedmineDropContext?.source_issue_url || '',
                redmine_username: username,
                redmine_password: password
            }),
            signal: controller.signal
        });
        if (!isReportAnalysisActivityCurrent(activity)) return;
        const result = await response.json();
        if (!isReportAnalysisActivityCurrent(activity)) return;
        if (response.ok && result.success) {
            displayReportAnalysis(result.data);
            finishReportAnalysisActivity(activity);
            notifyOperationResult('报告分析完成', result.filename || '附件分析完成',
                'success', 'report-analysis', { source: 'redmine', filename: result.filename || '' });
        } else {
            const reason = result.error || result.message || 'Redmine 报告解析失败';
            finishReportAnalysisActivity(activity, reason);
            notifyOperationResult('报告分析失败', reason, 'error', 'report-analysis',
                { source: 'redmine' });
        }
    } catch (error) {
        if (!isReportAnalysisActivityCurrent(activity) || error?.name === 'AbortError') return;
        console.error('Redmine auth error:', error);
        finishReportAnalysisActivity(activity, error.message || '请求失败');
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis',
            { source: 'redmine' });
    } finally {
        if (currentRedmineRequest === controller) currentRedmineRequest = null;
    }
}

async function handleReportFile(file) {
    if (!file) return;
    const fileName = file.name || '测试报告';
    const activity = beginReportAnalysisActivity(
        '正在上传测试报告', fileName + ' · 上传完成后将继续进行服务端解析'
    );

    try {
        const formData = createFormData(AnalysisMode.UPLOAD, { file });
        const upload = await postFormDataWithProgress('/api/reports/analyze', formData, (percent) => {
            setReportAnalysisActivityPhase(
                activity, '正在上传测试报告 ' + Math.round(percent) + '%',
                fileName + ' · 此百分比仅代表文件传输', percent
            );
        }, activity);
        if (!isReportAnalysisActivityCurrent(activity)) return;

        const result = upload.result;
        if (result.success) {
            displayReportAnalysis(result.data);
            finishReportAnalysisActivity(activity);
            notifyOperationResult('报告分析完成', '成功分析 ' + fileName,
                'success', 'report-analysis', { filename: fileName });
        } else {
            const reason = result.error || result.message || '未知错误';
            finishReportAnalysisActivity(activity, reason);
            notifyOperationResult('报告分析失败', reason, 'error', 'report-analysis',
                { filename: fileName });
        }
    } catch (error) {
        if (!isReportAnalysisActivityCurrent(activity) || error?.name === 'AbortError') return;
        console.error('Report analysis error:', error);
        finishReportAnalysisActivity(activity, error.message || '请求失败');
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis',
            { filename: fileName });
    }
}

function postFormDataWithProgress(url, formData, onProgress, activityToken = null) {
    return new Promise((resolve, reject) => {
        const generation = ++reportUploadGeneration;
        if (currentReportUploadRequest && currentReportUploadRequest.readyState !== 4) {
            currentReportUploadRequest.abort();
        }

        const xhr = new XMLHttpRequest();
        currentReportUploadRequest = xhr;
        let settled = false;

        const isCurrent = () => (
            generation === reportUploadGeneration && currentReportUploadRequest === xhr
        );
        const cleanup = () => {
            if (currentReportUploadRequest === xhr) currentReportUploadRequest = null;
        };
        const abortError = () => {
            const error = new Error('上传已取消');
            error.name = 'AbortError';
            return error;
        };
        const settle = (callback, value) => {
            if (settled) return;
            settled = true;
            cleanup();
            callback(value);
        };

        xhr.upload.addEventListener('progress', (event) => {
            if (isCurrent() && event.lengthComputable && event.total > 0 && onProgress) {
                onProgress((event.loaded / event.total) * 100, event.loaded, event.total);
            }
        });
        xhr.upload.addEventListener('load', () => {
            if (isCurrent() && activityToken !== null) {
                setReportAnalysisActivityPhase(
                    activityToken, '服务端正在解析测试报告',
                    '文件上传完成，正在解压并统计测试结果；大报告可能耗时较长'
                );
            }
        });

        xhr.addEventListener('load', () => {
            if (!isCurrent()) {
                settle(reject, abortError());
                return;
            }

            let result = null;
            try {
                result = JSON.parse(xhr.responseText || '{}');
            } catch (error) {
                settle(reject, new Error('服务器返回无效JSON'));
                return;
            }

            if (xhr.status >= 200 && xhr.status < 300) {
                settle(resolve, { result, generation });
                return;
            }

            settle(reject, new Error(result.message || result.error || result.detail || `HTTP ${xhr.status}`));
        });

        xhr.addEventListener('error', () => {
            settle(reject, isCurrent() ? new Error('网络错误') : abortError());
        });
        xhr.addEventListener('abort', () => settle(reject, abortError()));

        xhr.open('POST', url);
        xhr.send(formData);
    });
}

async function handleReportFolder(files) {
    if (!files?.length) return;
    const fileCount = files.length;
    const activity = beginReportAnalysisActivity(
        '正在上传报告文件夹',
        fileCount + ' 个文件 · 上传完成后将继续进行服务端解析'
    );

    try {
        const formData = new FormData();
        formData.append('mode', 'upload');
        for (const file of files) {
            formData.append('files[]', file, file.webkitRelativePath || file.name);
        }
        const upload = await postFormDataWithProgress('/api/reports/analyze', formData, (percent) => {
            setReportAnalysisActivityPhase(
                activity, '正在上传报告文件夹 ' + Math.round(percent) + '%',
                fileCount + ' 个文件 · 此百分比仅代表文件传输', percent
            );
        }, activity);
        if (!isReportAnalysisActivityCurrent(activity)) return;

        const result = upload.result;
        if (result.success) {
            displayReportAnalysis(result.data);
            finishReportAnalysisActivity(activity);
            notifyOperationResult('报告分析完成', '成功分析 ' + fileCount + ' 个文件',
                'success', 'report-analysis', { file_count: fileCount });
        } else {
            const reason = result.error || result.message || '未知错误';
            finishReportAnalysisActivity(activity, reason);
            notifyOperationResult('报告分析失败', reason, 'error', 'report-analysis',
                { file_count: fileCount });
        }
    } catch (error) {
        if (!isReportAnalysisActivityCurrent(activity) || error?.name === 'AbortError') return;
        console.error('Report folder analysis error:', error);
        finishReportAnalysisActivity(activity, error.message || '请求失败');
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis',
            { file_count: fileCount });
    }
}

function ensureReportAnalysisResultStructure() {
    const resultDiv = $('report-analysis-result');
    if (!resultDiv) return null;

    if (!$('report-summary') || !$('report-details') || !$('report-failures') ||
        !$('report-failure-list') || !$('report-failure-header-actions')) {
        resultDiv.innerHTML = `
            <section class="report-result-card report-overview">
                <div class="report-overview-line">
                    <div id="report-summary" class="report-summary-grid"></div>
                    <div class="report-overview-actions" role="group" aria-label="报告操作">
                        <button class="btn-xs" data-click="sendReportAnalysisEmail" aria-label="发送邮件" title="发送报告分析邮件"><svg class="ui-icon" aria-hidden="true"><use href="/static/icons/ui.svg#mail"></use></svg></button>
                        <button class="btn-xs" data-click="resetReportAnalysis" aria-label="清除" title="清除当前报告"><svg class="ui-icon" aria-hidden="true"><use href="/static/icons/ui.svg#close"></use></svg></button>
                    </div>
                </div>
                <div id="report-details" class="report-details"></div>
            </section>
            <section id="report-failures" class="report-result-card report-failure-workspace" style="display:none">
                <div class="report-section-head report-failure-heading">
                    <div id="report-failures-title">失败用例</div>
                    <div id="report-failure-header-actions" class="report-failure-header-actions" role="group" aria-label="选中失败用例操作"></div>
                </div>
                <div class="report-failure-layout">
                    <div id="report-failure-list" class="report-failure-list"></div>
                    <section id="report-failure-detail" class="report-failure-detail" hidden>
                        <div id="report-failure-detail-content"></div>
                    </section>
                </div>
            </section>
        `;
    }

    return resultDiv;
}

function displayReportAnalysis(data) {
    if (DEBUG) debugLog('[displayReportAnalysis] Called with data:', data);

    // 保存当前报告名称到全局变量，供失败用例卡片使用（使用一次性状态）
    window.currentReportName = data.report_name || '';
    window.currentReportAnalysisData = data;
    const provenance = data.provenance || {};
    if (Object.keys(provenance).length) {
        window.GmsWorkspace?.update({
            cluster_job_id: provenance.cluster_job_id || '',
            attempt_id: provenance.attempt_id || '',
            automation_run_id: provenance.automation_run_id || '',
            report_id: data.report_id || provenance.report_id || '',
            report_timestamp: data.report_timestamp || provenance.timestamp || '',
            artifact_id: provenance.artifact_id || '',
            gerrit_change_id: provenance.gerrit_change_id || '',
            gerrit_patchset: provenance.gerrit_patchset || '',
            redmine_issue_id: provenance.redmine_issue_id || '',
            origin_page: 'report-analysis'
        }, {source: 'report-analysis'});
    }

    if (DEBUG) debugLog('[displayReportAnalysis] Current report name:', window.currentReportName);

    const resultDiv = ensureReportAnalysisResultStructure();
    const uploadZone = $('report-upload-zone');
    const summaryDiv = $('report-summary');
    const detailsDiv = $('report-details');
    const failuresDiv = $('report-failures');
    const failureList = $('report-failure-list');

    if (summaryDiv) summaryDiv.innerHTML = '';
    if (detailsDiv) detailsDiv.innerHTML = '';
    if (failureList) failureList.innerHTML = '';
    if (failuresDiv) failuresDiv.style.display = 'none';
    const failuresTitle = $('report-failures-title');
    if (failuresTitle) failuresTitle.textContent = '❌ 失败用例';

    // 移除上传空状态类（缩小到固定高度）
    if (uploadZone) uploadZone.classList.remove('upload-empty');

    if (DEBUG) debugLog('[displayReportAnalysis] Elements:', {
        resultDiv,
        summaryDiv,
        detailsDiv,
        failuresDiv,
        failureList
    });

    if (!resultDiv) {
        console.error('[displayReportAnalysis] resultDiv not found!');
        return;
    }

    resultDiv.style.display = 'flex';
    const reportActiveFile = $('report-active-file');
    const reportActiveName = $('report-active-name');
    const displayName = data.report_name || data.test_result?.test_name || '测试报告';
    if (reportActiveName) {
        reportActiveName.textContent = displayName;
        reportActiveName.title = displayName;
    }
    if (reportActiveFile) reportActiveFile.hidden = false;

    if (summaryDiv && data.summary) {
        const summary = data.summary;

        const summaryHTML = `
            ${data.details && data.details.test_type ? `
                <div>
                    <span class="summary-label">测试类型：</span>
                    <span class="summary-value">${escapeHtml(data.details.test_type)}</span>
                </div>
            ` : ''}
            ${data.details && data.details.suite_version ? `
                <div>
                    <span class="summary-label">套件版本：</span>
                    <span class="summary-value">${escapeHtml(data.details.suite_version)}</span>
                </div>
            ` : ''}
            ${data.details && data.details.android_version ? `
                <div>
                    <span class="summary-label">Android版本：</span>
                    <span class="summary-value">${escapeHtml(data.details.android_version)}</span>
                </div>
            ` : ''}
            ${data.details && data.details.soc_platform ? `
                <div>
                    <span class="summary-label">SOC平台：</span>
                    <span class="summary-value">${escapeHtml(data.details.soc_platform)}</span>
                </div>
            ` : ''}
            <div>
                <span class="summary-label">总用例数：</span>
                <span class="summary-value">${escapeHtml(summary.total || 0)}</span>
            </div>
            <div>
                <span class="summary-label">通过：</span>
                <span class="summary-value pass">${escapeHtml(summary.pass || 0)}</span>
            </div>
            <div>
                <span class="summary-label">失败：</span>
                <span class="summary-value fail">${escapeHtml(summary.fail || 0)}</span>
            </div>
            <div>
                <span class="summary-label">通过率：</span>
                <span class="summary-value rate">${escapeHtml(summary.pass_rate || '0%')}</span>
            </div>
            <div>
                <span class="summary-label">测试报告：</span>
                <span class="summary-value">${escapeHtml(data.report_name || data.test_result?.test_name || 'N/A')}</span>
            </div>
        `;

        summaryDiv.innerHTML = summaryHTML;
        // Compact stats first, provenance and build metadata second. The
        // underlying fields remain the same as the parsed report contract.
        const stats = document.createElement('div');
        stats.className = 'report-summary-stats';
        const context = document.createElement('div');
        context.className = 'report-summary-context';
        const statLabels = new Set(['总用例数：', '通过：', '失败：', '通过率：']);
        const sizingRoles = {
            '总用例数：': 'total', '通过：': 'pass', '失败：': 'fail',
            '通过率：': 'pass-rate', '测试类型：': 'test-type',
            '套件版本：': 'suite-version', 'Android版本：': 'android-version',
            'SOC平台：': 'soc-platform'
        };
        [...summaryDiv.children].forEach(field => {
            const label = field.querySelector('.summary-label')?.textContent?.trim();
            if (sizingRoles[label]) field.classList.add('report-summary-' + sizingRoles[label]);
            if (label === '测试报告：') {
                field.classList.add('report-summary-report');
                field.title = field.querySelector('.summary-value')?.textContent || '';
            }
            (statLabels.has(label) ? stats : context).appendChild(field);
        });
        summaryDiv.replaceChildren(stats, context);
    } else {
        console.error('[displayReportAnalysis] Summary not generated. summaryDiv:', summaryDiv, 'data.summary:', data.summary);
    }

    if (detailsDiv) {
        const fields = [
            ['Worker', provenance.worker_id], ['Job', provenance.cluster_job_id],
            ['Attempt', provenance.attempt_id], ['ATS Run', provenance.automation_run_id],
            ['Artifact', provenance.artifact_id], ['Gerrit', provenance.gerrit_change_id],
            ['Redmine', provenance.redmine_issue_id]
        ].filter(([, value]) => value);
        detailsDiv.innerHTML = fields.length ? `
            <div class="report-provenance">
                ${fields.map(([label, value]) => `<span class="report-provenance-chip" title="${escapeHtml(String(value))}"><b>${escapeHtml(label)}</b> ${escapeHtml(value)}</span>`).join('')}
                ${provenance.cluster_job_id ? '<button class="btn-xs report-provenance-link" data-provenance-page="cluster">打开集群任务</button>' : ''}
                ${provenance.automation_run_id ? '<button class="btn-xs report-provenance-link" data-provenance-page="automation">打开 ATS</button>' : ''}
                ${provenance.gerrit_change_id ? '<button class="btn-xs report-provenance-link" data-provenance-page="gerrit-dashboard">打开 Gerrit</button>' : ''}
                ${provenance.redmine_issue_id ? '<button class="btn-xs report-provenance-link" data-provenance-page="redmine-agent">打开 Redmine</button>' : ''}
            </div>` : '';
        detailsDiv.querySelectorAll('[data-provenance-page]').forEach(button => {
            button.addEventListener('click', () => window.GmsWorkspace?.navigate(button.dataset.provenancePage));
        });
    }

    const failureDetail = $('report-failure-detail');
    const failureDetailContent = $('report-failure-detail-content');
    const headerActions = $('report-failure-header-actions');
    if (headerActions) headerActions.replaceChildren();
    if (failureDetail) failureDetail.hidden = true;
    if (failureDetailContent) failureDetailContent.replaceChildren();
    if (failuresDiv && failureList && Array.isArray(data.failures) && data.failures.length) {
        failuresDiv.style.display = 'flex';
        if (failuresTitle) failuresTitle.textContent = '失败用例 (' + data.failures.length + ')';
        const fragment = document.createDocumentFragment();
        data.failures.forEach((failure, idx) => {
            const card = document.createElement('div');
            card.className = 'report-failure-card';
            const select = document.createElement('button');
            select.type = 'button';
            select.className = 'report-failure-select';
            select.dataset.reportFailureIndex = String(idx);
            select.setAttribute('aria-label', '查看失败用例 ' + (failure.name || '未知用例'));
            const top = document.createElement('span');
            top.className = 'report-failure-item-top';
            const moduleName = document.createElement('span');
            moduleName.className = 'report-failure-module';
            moduleName.textContent = failure.module || '未知模块';
            moduleName.title = moduleName.textContent;
            const failed = document.createElement('span');
            failed.className = 'report-failure-status';
            failed.textContent = '失败';
            top.append(moduleName, failed);
            const testName = document.createElement('span');
            testName.className = 'report-failure-case';
            testName.textContent = failure.name || '未知用例';
            testName.title = testName.textContent;
            // 左侧仅显示模块、失败状态和用例名；完整原文交给右侧。
            select.append(top, testName);
            card.appendChild(select);
            fragment.appendChild(card);
        });
        failureList.replaceChildren(fragment);
        if (!failureList.dataset.reportFailureBound) {
            failureList.dataset.reportFailureBound = 'true';
            failureList.addEventListener('click', event => {
                const button = event.target.closest('[data-report-failure-index]');
                if (button && failureList.contains(button)) {
                    selectReportFailure(Number(button.dataset.reportFailureIndex));
                }
            });
        }
        selectReportFailure(0);
    } else if (failuresDiv && failureList) {
        failuresDiv.style.display = 'flex';
        if (failuresTitle) failuresTitle.textContent = '✅ 测试结果';
        const success = document.createElement('div');
        success.className = 'report-empty-success';
        const heading = document.createElement('b');
        heading.textContent = '✅ 全部通过';
        const copy = document.createElement('span');
        const passRate = data.summary?.pass_rate || '';
        copy.textContent = passRate
            ? '这份报告通过率 ' + passRate + '，没有失败用例。'
            : '这份报告没有失败用例，可以清除后继续分析下一份报告。';
        success.append(heading, copy);
        failureList.replaceChildren(success);
    }
}

function getReportFailureByIndex(failureIndex) {
    const report = window.currentReportAnalysisData;
    if (!report || !Array.isArray(report.failures)) return null;
    return report.failures[failureIndex] || null;
}

function openReportAnalysisRedmineAgent(issueId = '') {
    const frame = document.getElementById('redmine-agent-frame');
    const query = new URLSearchParams();
    query.set('tab', 'issues');
    if (issueId) query.set('issue', issueId);
    if (frame) window.setLazyFrameSource?.(frame, '/redmine-agent?' + query.toString());
    minimizeReportDiagnosisWorkbench();
    if (typeof switchPage === 'function') switchPage('redmine-agent', null);
}

function getReportDiagnosisKey(failureIndex = 0) {
    const report = window.currentReportAnalysisData || {};
    const failure = getReportFailureByIndex(failureIndex) || {};
    return [
        report.report_name || '',
        failureIndex,
        failure.name || failure.test_name || '',
        failure.module || ''
    ].join('|');
}

function openReportDiagnosisModal(failureIndex = 0) {
    const modal = $('report-diagnosis-modal');
    if (!modal) {
        showToast('诊断弹框未加载', 'error');
        return;
    }
    const minimized = $('report-diagnosis-minimized');
    if (minimized) minimized.style.display = 'none';
    modal.dataset.failureIndex = String(failureIndex);
    ModalManager.open('report-diagnosis-modal');

    const diagnosisKey = getReportDiagnosisKey(failureIndex);
    const diag = window.reportDiagnosis || {};
    if (diag.key === diagnosisKey && diag.data) {
        return;
    }
    window.reportDiagnosis = window.reportDiagnosis || {};
    window.reportDiagnosis.key = diagnosisKey;
    runReportDiagnosis(failureIndex);
}

function closeReportDiagnosisWorkbench() {
    ModalManager.close('report-diagnosis-modal');
    const minimized = $('report-diagnosis-minimized');
    if (minimized) minimized.style.display = 'none';
}

function minimizeReportDiagnosisWorkbench() {
    const modal = $('report-diagnosis-modal');
    if (!modal) return;
    ModalManager.close('report-diagnosis-modal');
    const minimized = $('report-diagnosis-minimized');
    const title = $('report-diagnosis-minimized-title');
    if (title) {
        const data = (window.reportDiagnosis || {}).data || {};
        title.textContent = data.test_name || data.report_name || '诊断工作台';
    }
    if (minimized) minimized.style.display = 'flex';
}

function restoreReportDiagnosisWorkbench() {
    const minimized = $('report-diagnosis-minimized');
    if (minimized) minimized.style.display = 'none';
    ModalManager.open('report-diagnosis-modal');
}

function rerunReportDiagnosis() {
    const modal = $('report-diagnosis-modal');
    const currentIndex = Number(
        modal?.dataset?.failureIndex ||
        (window.reportDiagnosis || {}).failureIndex ||
        0
    ) || 0;
    window.reportDiagnosis = {
        ...(window.reportDiagnosis || {}),
        key: null,
        data: null,
    };
    runReportDiagnosis(currentIndex);
}

function renderReportDiagnosisLoading(failure, classNames, errorMessage) {
    const diagnosticSummary = $('report-diagnostic-summary');
    const diagnosticResult = $('report-diagnostic-result');
    if (diagnosticSummary) {
        diagnosticSummary.innerHTML = `
            <div class="dx-hero">
                <div class="dx-title-row">
                    <div class="dx-title-main">${escapeHtml(failure.name || failure.test_name || '未知用例')}</div>
                    <span class="dx-status-pill ui-status is-warning">诊断中</span>
                </div>
                <div class="dx-compact-line">${escapeHtml((errorMessage || '').split('\n').slice(0, 2).join('\n') || '正在提取失败上下文...')}</div>
            </div>
        `;
    }
    if (diagnosticResult) {
        diagnosticResult.innerHTML = `
            <div class="dx-loading-grid">
                <div class="dx-loading-card"><span>1</span>提取失败堆栈</div>
                <div class="dx-loading-card"><span>2</span>定位套件构件</div>
                <div class="dx-loading-card"><span>3</span>源码检索</div>
                <div class="dx-loading-card"><span>4</span>AI 诊断和建议</div>
            </div>
        `;
    }
}

async function runReportDiagnosis(failureIndex = 0) {
    const report = window.currentReportAnalysisData;
    if (!report) {
        showToast('请先加载一份报告', 'warning');
        return;
    }

    const failure = getReportFailureByIndex(failureIndex);
    if (!failure) {
        showToast('当前报告没有可诊断的失败用例', 'warning');
        return;
    }

    const testName = failure.name || failure.test_name || report.report_name || '未知用例';
    const errorMessage = failure.reason || failure.stack_trace || '';
    const moduleName = failure.module || '';
    const classNames = extractClassNames(testName, errorMessage);
    renderReportDiagnosisLoading(failure, classNames, errorMessage);

    try {
        const result = await apiCall('/api/reports/diagnose', 'POST', {
            test_name: testName,
            error_message: errorMessage,
            stack_trace: errorMessage,
            module: moduleName,
            class_names: classNames,
            report_name: report.report_name || '',
            failure_index: failureIndex,
            test_type: report.details?.test_type || '',
            suite_version: report.details?.suite_version || '',
            android_version: report.details?.android_version || '',
            source_path: failure.source_path || failure.file_path || report.source_path || ''
        });
        if (!result.success) {
            throw new Error(result.error || result.message || '诊断失败');
        }
        renderReportDiagnosis(result.data || {});
        const aiFallback = result.data?.ai_result?.ai_enabled === false;
        const providerFallback = Boolean(result.data?.ai_result?.ai_fallback_used);
        notifyOperationResult(
            aiFallback
                ? '报告诊断已降级'
                : providerFallback ? '报告诊断使用备用模型' : '报告诊断完成',
            aiFallback
                ? `${testName} 本地 AI 不可用，当前显示规则分析`
                : providerFallback
                ? `${testName} 本地 AI 不可用，已由备用模型完成`
                : `${testName} 诊断已完成`,
            (aiFallback || providerFallback) ? 'warning' : 'success', 'report-diagnosis', {
            report_name: report.report_name || '',
            failure_index: failureIndex
        });
    } catch (error) {
        debugLog('[Report Diagnosis] Error:', error);
        const diagnosticResult = $('report-diagnostic-result');
        if (diagnosticResult) {
            diagnosticResult.innerHTML = `<div class="dx-error">诊断失败: ${escapeHtml(error.message)}</div>`;
        }
        notifyOperationResult('报告诊断失败', error.message, 'error', 'report-diagnosis');
    }
}

function renderDxLocatorRow(label, value) {
    return `
        <div class="dx-locator-row">
            <span class="dx-locator-label">${escapeHtml(label)}</span>
            <span class="dx-locator-value">${escapeHtml(value || '无')}</span>
        </div>
    `;
}

function renderDxEmpty(text) {
    return `<div class="dx-empty">${escapeHtml(text)}</div>`;
}

function joinSuiteArtifactPath(rootPath, artifactPath) {
    const artifact = String(artifactPath || '').trim();
    if (!artifact) return String(rootPath || '').trim();
    if (artifact.startsWith('/')) return artifact;
    const root = String(rootPath || '').trim().replace(/\/+$/, '');
    return root ? `${root}/${artifact.replace(/^\/+/, '')}` : artifact;
}

function normalizeDiagnosisSourceDisplayPath(path) {
    const text = String(path || '').trim().replace(/\\/g, '/');
    if (!text) return '';
    const srcIndex = text.lastIndexOf('/src/');
    if (srcIndex >= 0) return text.slice(srcIndex + 5);
    const packageMatch = text.match(/(?:^|\/)(com|android|org|libcore)\//);
    if (packageMatch && typeof packageMatch.index === 'number') {
        return text.slice(text[packageMatch.index] === '/' ? packageMatch.index + 1 : packageMatch.index);
    }
    return text;
}

function getDiagnosisDisplaySourcePath(sourceResults, sourceGuess, sourcePath) {
    const results = Array.isArray(sourceResults) ? sourceResults : [];
    const exact = results.find(item => item && item.is_exact_location && (item.path || item.display_path));
    const first = exact || results.find(item => item && (item.path || item.display_path));
    return normalizeDiagnosisSourceDisplayPath(
        (first && (first.path || first.display_path)) ||
        sourceGuess?.source_path ||
        sourcePath ||
        ''
    );
}

function getReportIssueIdFromName() {
    const reportName = window.currentReportName || window.currentReportAnalysisData?.report_name || '';
    const match = String(reportName || '').match(/^Redmine-(\d+)-/);
    return match ? match[1] : '';
}

function buildReportDiagnosisReplyText(data, patchDraft) {
    const aiResult = data.ai_result || {};
    const lines = [];
    const rootVerified = aiResult.root_cause_status === 'verified';
    const exemptions = Array.isArray(data.mainline_exemptions) ? data.mainline_exemptions : [];
    if (exemptions.length) {
        const ids = exemptions.map(item => item.exemption_id).filter(Boolean).join(', ');
        lines.push(
            `**Mainline 已知豁免**: 该用例命中 Google Mainline 已知豁免（exemption ${ids || '未知'}，${exemptions[0].test_module || data.module || ''}），通常无需本地修复。`,
            '',
        );
    }
    lines.push(
        `**测试模块**: ${data.module || '-'}`,
        '',
        `**测试用例**: ${data.test_name || '-'}`,
        '',
        '**已观察到的失败**:',
        aiResult.observed_failure || data.error_message || '-',
        '',
        rootVerified ? '**已验证根因**:' : '**初步判断（待验证）**:',
        aiResult.root_cause || data.summary || aiResult.analysis || '-',
    );
    if (aiResult.root_cause_note) lines.push('', `> ${aiResult.root_cause_note}`);
    const suggestions = aiResult.suggestions || [];
    if (suggestions.length) {
        lines.push('', '**处理建议**:', suggestions.map((item, idx) => `${idx + 1}. ${item}`).join('\n'));
    }
    if (patchDraft) {
        lines.push('', rootVerified ? '**补丁方向**:' : '**排查方向**:', '<pre>', patchDraft, '</pre>');
    }
    return lines.join('\n');
}

function renderReportDiagnosis(data) {
    const diagnosticSummary = $('report-diagnostic-summary');
    const diagnosticResult = $('report-diagnostic-result');
    if (!diagnosticResult) return;

    const aiResult = data.ai_result || {};
    const kbResults = data.knowledge_base_results || [];
    const sourceResults = data.source_search_results || [];
    const exemptions = Array.isArray(data.mainline_exemptions) ? data.mainline_exemptions : [];
    const patchDraft = data.patch_draft || '';
    const stackTrace = data.stack_trace || '';
    const suiteTarget = data.suite_target || {};
    const suiteArtifact = suiteTarget.artifact || null;
    const artifactCandidates = suiteTarget.artifact_candidates || [];
    const sourceGuess = suiteTarget.source_guess || {};
    const sourcePath = data.source_path || sourceGuess.source_path || '';
    const suiteArtifactPath = joinSuiteArtifactPath(suiteTarget.suite_root, suiteArtifact ? suiteArtifact.path : '');
    const displaySourcePath = getDiagnosisDisplaySourcePath(sourceResults, sourceGuess, sourcePath);
    const currentFailureIndex = Number(data.failure_index || 0) || 0;
    const suggestions = aiResult.suggestions || [];
    const issueIdFromReport = getReportIssueIdFromName();
    const currentFailure = getReportFailureByIndex(currentFailureIndex) || {};
    const aiFallback = aiResult.ai_enabled === false && aiResult.ai_attempted;
    const providerFallback = Boolean(aiResult.ai_fallback_used);
    const rootCauseStatus = aiResult.root_cause_status || 'hypothesis';
    const rootCauseVerified = rootCauseStatus === 'verified';
    const rootCauseLabel = rootCauseVerified ? '已验证根因' : '初步判断';
    const rootCauseTag = rootCauseVerified ? 'Verified root cause' : 'Hypothesis';
    const confidenceLabels = {high: '高置信度', medium: '中置信度', low: '低置信度'};
    const rootConfidence = confidenceLabels[aiResult.root_cause_confidence] || '低置信度';
    const observedFailure = aiResult.observed_failure || data.error_message || stackTrace.split('\n').find(Boolean) || '未提取到明确失败信息';
    const patchDraftTitle = rootCauseVerified ? '补丁草案' : '排查草案';
    const aiStatusLabel = aiFallback
        ? '规则分析（AI 不可用）'
        : `${aiResult.ai_model || 'AI'}${providerFallback ? '（备用）' : ''}`;
    const aiFallbackNotice = aiFallback
        ? `<div class="dx-error">
            <b>本地 AI 未完成分析，当前结果来自规则降级</b>
            <div>${escapeHtml(String(aiResult.ai_error || '模型调用失败').slice(0, 260))}</div>
        </div>`
        : '';
    const providerFallbackNotice = providerFallback
        ? `<div class="dx-error">
            <b>本地 AI 未完成分析，本次已由 ${escapeHtml(aiResult.ai_model || aiResult.ai_provider || '备用模型')} 完成</b>
            <div>${escapeHtml(String((aiResult.ai_provider_errors || []).join('; ') || '本地模型调用失败').slice(0, 260))}</div>
        </div>`
        : '';
    const reportTestType = (window.currentReportAnalysisData?.details && window.currentReportAnalysisData.details.test_type) || data.test_type || suiteTarget.test_type || '';
    const replyDraft = buildReportDiagnosisReplyText(data, patchDraft);
    const hasExactArtifact = Boolean(suiteArtifact && (
        (suiteArtifact.reasons || []).includes('exact-module-binary') ||
        (suiteArtifact.path || '').toLowerCase().endsWith('/' + String(data.module || currentFailure.module || '').toLowerCase() + '.apk') ||
        (suiteArtifact.path || '').toLowerCase().endsWith('/' + String(data.module || currentFailure.module || '').toLowerCase() + '.jar')
    ));

    window.reportDiagnosis = {
        data,
        target: suiteTarget,
        failureIndex: currentFailureIndex,
        key: (window.reportDiagnosis || {}).key,
        displaySourcePath,
        suiteArtifactPath,
        text: [
            `报告: ${data.report_name || ''}`,
            `用例: ${data.test_name || ''}`,
            `模块: ${data.module || ''}`,
            `测试类型: ${suiteTarget.test_type || data.test_type || ''}`,
            `套件版本: ${suiteTarget.suite_version || data.suite_version || ''}`,
            `套件: ${suiteTarget.suite_name || suiteTarget.suite_path || ''}`,
            `构件: ${suiteArtifact ? suiteArtifact.path : ''}`,
            `源码路径: ${displaySourcePath || sourcePath}`,
            `Mainline 豁免: ${exemptions.length ? exemptions.map(i => `${i.exemption_id}(${i.issue_type || ''})`).join(', ') : '无'}`,
            `失败现象: ${observedFailure}`,
            `${rootCauseLabel}: ${aiResult.root_cause || data.summary || ''}`,
            `结论置信度: ${rootConfidence}`,
            `分析: ${aiResult.analysis || ''}`,
            `建议: ${(aiResult.suggestions || []).join('\n')}`,
            `${patchDraftTitle}:\n${patchDraft}`,
            `堆栈:\n${stackTrace || '无'}`
        ].join('\n\n'),
        replyDraft,
        patchDraft
    };

    const reportSummary = window.currentReportAnalysisData?.summary || {};
    const summaryCards = [
        ['package', '测试类型', suiteTarget.test_type || data.test_type || '—', ''],
        ['archive', '套件版本', suiteTarget.suite_version || data.suite_version || '—', ''],
        ['check', '通过用例', reportSummary.pass ?? '—', 'success'],
        ['error', '失败用例', reportSummary.fail ?? '—', 'danger'],
        ['grid', '总用例数', reportSummary.total ?? '—', '']
    ].map(([icon, label, value, tone]) => `
        <div class="dx-summary-card ${tone}">
            <svg class="ui-icon" aria-hidden="true"><use href="/static/icons/ui.svg#${icon}"></use></svg>
            <span>${label}</span><strong>${escapeHtml(String(value))}</strong>
        </div>`).join('');

    if (diagnosticSummary) {
        diagnosticSummary.innerHTML = `
            <h3 class="tech-section-title"><svg class="ui-icon" aria-hidden="true"><use href="/static/icons/ui.svg#chart"></use></svg>报告摘要</h3>
            <div class="dx-summary-cards">${summaryCards}</div>
            <div class="dx-hero">
                <div class="dx-title-row">
                    <div class="dx-title-main">${escapeHtml(data.test_name || data.report_name || '诊断工作台')}</div>
                    <div class="dx-pill-row">
                        <span class="dx-status-pill ui-status ${aiFallback || providerFallback ? 'is-warning' : 'is-info'}">${escapeHtml(aiStatusLabel)}</span>
                        <span class="dx-status-pill ui-status">${escapeHtml(suiteTarget.test_type || data.test_type || '未知类型')}</span>
                        <span class="dx-status-pill">${escapeHtml(suiteTarget.suite_version || data.suite_version || '未知版本')}</span>
                    </div>
                </div>
                <div class="dx-compact-line">${escapeHtml([data.module || currentFailure.module || '', suiteArtifactPath, displaySourcePath].filter(Boolean).join(' | ') || '当前失败项诊断')}</div>
            </div>
        `;
    }

    const sourceCards = renderReportSourceCards(sourceResults);

    const kbCards = renderReportKbCards(kbResults);

    const backgroundSection = renderReportSystemBackgroundPanel(data.system_background_results);

    const candidateCards = !hasExactArtifact && artifactCandidates.length > 0
        ? `<details class="dx-details"><summary>候选构件 (${artifactCandidates.length})</summary><div class="dx-list">${
            artifactCandidates.slice(0, 5).map((item, idx) => `
                <button class="dx-candidate" data-click="openReportDiagnosisArtifactCandidate" data-a0="${idx}">
                    <span>${escapeHtml(item.path || item.name || '未知构件')}</span>
                    <b>${escapeHtml(String(item.score || 0))}</b>
                </button>
            `).join('')
        }</div></details>`
        : '';

    const suggestionCards = suggestions.length > 0
        ? suggestions.map((s, idx) => `
            <div class="dx-suggestion">
                <span>${idx + 1}</span>
                <div>${escapeHtml(s)}</div>
            </div>
        `).join('')
        : renderDxEmpty('暂无解决建议');

    const exemptionBanner = exemptions.length
        ? `<section class="dx-section dx-exempt-banner">
            <div class="dx-exempt-head">
                <span class="dx-exempt-badge ui-status is-success">✓ 已豁免</span>
                <span class="dx-exempt-title">命中 Google Mainline 已知豁免（通常无需本地修复）</span>
            </div>
            <div class="dx-exempt-list">
                ${exemptions.map(item => {
                    const sourceUrl = safeReportExternalUrl(item.source_url);
                    return `
                    <div class="dx-exempt-item">
                        <div class="dx-exempt-item-head">
                            <b class="dx-exempt-id">exemption ${escapeHtml(String(item.exemption_id || ''))}</b>
                            <span class="dx-exempt-meta">${escapeHtml([item.issue_type, item.test_module].filter(Boolean).join(' · '))}</span>
                            ${item.match_kind === 'fuzzy' ? '<span class="dx-exempt-kind">模糊匹配</span>' : ''}
                            ${sourceUrl ? `<a class="dx-link" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener">来源</a>` : ''}
                        </div>
                        <div class="dx-exempt-case">${escapeHtml(item.test_case || '')}</div>
                        ${item.issue_text ? `<div class="dx-exempt-text">${escapeHtml(String(item.issue_text).slice(0, 280))}</div>` : ''}
                    </div>
                `;
                }).join('')}
            </div>
        </section>`
        : '';

    const actionPanel = `
        <section class="dx-section dx-action-section">
            <div class="dx-section-title">下一步动作</div>
            <button type="button" class="dx-action-card" data-click="openReportDiagnosisTestCase" data-a0="${escapeJsAttr(reportTestType)}" data-a1="${escapeJsAttr(data.module || currentFailure.module || '')}" data-a2="${escapeJsAttr(data.test_name || currentFailure.name || '')}">
                <b>执行单测复现</b>
                <span>跳到测试页并填入模块/用例</span>
            </button>
            ${suiteArtifact ? `<button type="button" class="dx-action-card" data-click="openReportDiagnosisSuiteBrowser"><b>打开测试套件</b><span>${escapeHtml(suiteArtifact.path || '')}</span></button>` : ''}
            ${issueIdFromReport ? `<button type="button" class="dx-action-card" data-click="openReportDiagnosisRedmineReply"><b>Redmine 回复</b><span>基于诊断结论生成回复草稿</span></button>` : ''}
            ${issueIdFromReport ? `<button type="button" class="dx-action-card" data-click="openReportAnalysisRedmineAgent" data-a0="${escapeJsAttr(issueIdFromReport)}"><b>Redmine 工作台</b><span>查看工单历史、附件证据和相似案例</span></button>` : ''}
            <button type="button" class="dx-action-card" data-click="saveDiagnosisToWiki"><b>📥 存为Wiki</b><span>把诊断结论沉淀到知识库</span></button>
        </section>
    `;

    diagnosticResult.innerHTML = `
        <div class="dx-workbench-vertical">
            ${aiFallbackNotice}
            ${providerFallbackNotice}
            ${exemptionBanner}
            ${actionPanel}
            <div class="dx-workflow">
                <section class="dx-workflow-step dx-workflow-step-analysis">
                <div class="dx-step-label">
                    <span>1</span>
                    <div>
                        <b>详细分析</b>
                        <em>先区分失败现象，再验证上游原因</em>
                    </div>
                </div>
                <div class="dx-two-col">
                    <div class="dx-section dx-section-large">
                        <div class="dx-section-title">${rootCauseLabel}</div>
                        <div class="dx-observed-failure">
                            <span>已观察到的失败</span>
                            <div>${escapeHtml(observedFailure)}</div>
                        </div>
                        <div class="dx-root-cause">
                            <span>${rootCauseTag} · ${rootConfidence}</span>
                            <div>${escapeHtml(aiResult.root_cause || data.summary || '待分析')}</div>
                        </div>
                        ${aiResult.root_cause_note ? `<div class="dx-root-note">${escapeHtml(aiResult.root_cause_note)}</div>` : ''}
                        <div class="dx-preline">${escapeHtml(aiResult.analysis || '无')}</div>
                    </div>
                    <div class="dx-section dx-context-section">
                        <div class="dx-section-title">失败上下文</div>
                        <div class="dx-stack">${escapeHtml(stackTrace || data.error_message || '无')}</div>
                    </div>
                </div>
                </section>

                <section class="dx-workflow-step dx-workflow-step-source">
                <div class="dx-step-label">
                    <span>2</span>
                    <div>
                        <b>源码或测试套件反编译</b>
                        <em>把定位依据、候选构件和源码搜索放在一起</em>
                    </div>
                </div>
                <div class="dx-source-layout">
                    <div class="dx-section dx-section-large">
                        <div class="dx-section-head">
                            <div class="dx-section-title">套件源码定位</div>
                            ${suiteArtifact ? `<button class="btn-xxs btn-primary" data-click="openReportDiagnosisSourcePreview">反编译并预览源码</button>` : ''}
                        </div>
                        <div class="dx-locator-list">
                            ${renderDxLocatorRow('测试套件', suiteArtifactPath || '未定位')}
                            ${renderDxLocatorRow('源码路径猜测', displaySourcePath || '未推断')}
                        </div>
                        ${candidateCards}
                    </div>
                    <div class="dx-section">
                        <div class="dx-section-title">源码检索 <span>${sourceResults.length} 结果</span></div>
                        <div class="dx-list">${sourceCards}</div>
                    </div>
                </div>
                </section>

                <section class="dx-workflow-step dx-workflow-step-solution">
                <div class="dx-step-label">
                    <span>3</span>
                    <div>
                        <b>解决建议</b>
                        <em>建议、补丁草案和知识库证据集中收口</em>
                    </div>
                </div>
                <div class="dx-solution-layout">
                    <div class="dx-section dx-section-large">
                        <div class="dx-section-title">建议动作</div>
                        <div class="dx-list">${suggestionCards}</div>
                    </div>
                    <div class="dx-section">
                        <div class="dx-section-title">${patchDraftTitle}</div>
                        <pre class="dx-code ui-file-preview">${escapeHtml(patchDraft || '无')}</pre>
                    </div>
                    <div class="dx-section">
                        <div class="dx-section-title">GMS 认证知识库</div>
                        <div class="dx-list">${kbCards}</div>
                    </div>
                </div>
                ${backgroundSection}
                </section>
            </div>
        </div>
    `;
}

function openReportDiagnosisRedmineReply() {
    const diag = window.reportDiagnosis || {};
    const data = diag.data || {};
    const failureIndex = Number(data.failure_index || diag.failureIndex || 0) || 0;
    const issueId = getReportIssueIdFromName();
    if (!issueId) {
        showToast('当前报告名称未关联 Redmine Issue ID', 'warning');
        return;
    }
    const failure = getReportFailureByIndex(failureIndex) || {};
    const moduleName = data.module || failure.module || '未知模块';
    const testName = data.test_name || failure.name || failure.test_name || '未知用例';
    const modalId = openRedmineReplyModal(moduleName, testName, failureIndex, issueId);
    const modal = modalId ? document.getElementById(modalId) : null;
    const area = modal?.querySelector('[data-redmine-reply-text]');
    if (area && diag.replyDraft) area.value = diag.replyDraft;
}

function openReportDiagnosisTestCase(testType, moduleName, testCaseName) {
    minimizeReportDiagnosisWorkbench();
    goToTestCase(testType, moduleName, testCaseName);
}

async function copyReportDiagnosis() {
    const text = (window.reportDiagnosis || {}).text || '';
    if (!text) {
        showToast('暂无可复制的诊断结果', 'warning');
        return;
    }
    try {
        await navigator.clipboard.writeText(text);
        showToast('诊断结果已复制', 'success');
    } catch (error) {
        showToast('复制失败', 'error');
    }
}

function getCurrentReportDiagnosisTarget() {
    return (window.reportDiagnosis || {}).target || null;
}

async function saveDiagnosisToWiki() {
    const diag = window.reportDiagnosis || {};
    const data = diag.data || {};
    if (!data || Object.keys(data).length === 0) {
        showToast('暂无诊断结果可保存', 'warning');
        return;
    }
    const moduleName = data.module || '';
    const testName = data.test_name || '';
    const aiResult = data.ai_result || {};
    const reportName = data.report_name || (window.currentReportAnalysisData && window.currentReportAnalysisData.timestamp) || '';
    const reportTimestamp = (window.currentReportAnalysisData && window.currentReportAnalysisData.timestamp) || reportName;
    const issueId = getReportIssueIdFromName();
    const kbHit = (data.knowledge_base_results || []).map(k => `- ${k.subject || k.error_signature || ''}: ${k.solution_summary || k.root_cause || ''}`).join('\n');

    const content = [
        `# ${moduleName ? moduleName + (testName ? '#' + testName : '') : '测试诊断'}`,
        '',
        `**测试用例:** ${testName || '未知'}`,
        `**模块:** ${moduleName || '未知'}`,
        `**报告:** ${reportName || '未知'}`,
        '',
        '## 报错信息',
        '```',
        (data.error_message || '').slice(0, 4000),
        '```',
        aiResult.observed_failure ? `\n## 已观察到的失败\n${aiResult.observed_failure}` : '',
        aiResult.root_cause ? `\n## ${aiResult.root_cause_status === 'verified' ? '已验证根因' : '初步判断（待验证）'}\n${aiResult.root_cause}` : '',
        aiResult.root_cause_note ? `\n> ${aiResult.root_cause_note}` : '',
        aiResult.analysis ? `\n## 分析\n${aiResult.analysis}` : '',
        aiResult.suggestions && aiResult.suggestions.length ? `\n## 建议\n${aiResult.suggestions.map(s => '- ' + s).join('\n')}` : '',
        kbHit ? `\n## 知识库命中\n${kbHit}` : '',
    ].filter(Boolean).join('\n');

    const links = [];
    if (reportTimestamp) links.push({target_type:'test_report', target_id:String(reportTimestamp), title:String(reportTimestamp)});
    if (issueId) links.push({target_type:'redmine_issue', target_id:String(issueId), title:'#' + String(issueId)});
    if (moduleName) links.push({target_type:'test_case', target_id:moduleName + (testName ? '::' + testName : ''), title:moduleName});

    try {
        await window.saveToWiki({
            content,
            notebook: '测试问题库',
            links
        });
        showToast('已存入知识库「测试问题库」', 'success');
    } catch (e) {
        showToast('存为Wiki失败: ' + e.message, 'error');
    }
}

function buildReportDiagnosisSourcePath(target) {
    const guess = target?.source_guess || {};
    return guess.source_path || '';
}

function getReportDiagnosisSourceLocation() {
    const diag = window.reportDiagnosis || {};
    const data = diag.data || {};
    const target = getCurrentReportDiagnosisTarget();
    const sourcePath = diag.displaySourcePath || buildReportDiagnosisSourcePath(target);
    const fallbackSourcePath = buildReportDiagnosisSourcePath(target);
    const lineNumber = Number(
        data?.failure_location?.line_number ||
        target?.source_guess?.line_number ||
        0
    ) || null;
    return { sourcePath, fallbackSourcePath, lineNumber };
}

function _requireDiagnosisArtifact(msg) {
    const target = getCurrentReportDiagnosisTarget();
    if (!target || !target.artifact) {
        showToast(msg || '未找到可反编译的构件', 'warning');
        return null;
    }
    return target;
}

async function openReportDiagnosisSourcePreview() {
    if (!_requireDiagnosisArtifact()) return;
    minimizeReportDiagnosisWorkbench();
    const { sourcePath, fallbackSourcePath, lineNumber } = getReportDiagnosisSourceLocation();
    await openReportDiagnosisApkAnalysis({ sourcePath, fallbackSourcePath, lineNumber });
}

async function openReportDiagnosisArtifactCandidate(index = 0) {
    const target = getCurrentReportDiagnosisTarget();
    const candidate = target?.artifact_candidates?.[index];
    if (!target || !candidate) {
        showToast('候选构件不存在', 'warning');
        return;
    }
    window.reportDiagnosis.target = {
        ...target,
        artifact: candidate,
        artifact_confidence: candidate.score || 0
    };
    minimizeReportDiagnosisWorkbench();
    const { sourcePath, fallbackSourcePath, lineNumber } = getReportDiagnosisSourceLocation();
    await openReportDiagnosisApkAnalysis({ sourcePath, fallbackSourcePath, lineNumber });
}

async function openReportDiagnosisSuiteBrowser() {
    const target = getCurrentReportDiagnosisTarget();
    if (!target || !target.suite_path || !target.artifact) {
        showToast('未找到可打开的套件构件', 'warning');
        return;
    }
    minimizeReportDiagnosisWorkbench();
    const artifactPath = target.artifact.path || '';
    const directoryPath = getParentSuitePath(artifactPath);
    if (typeof switchPage === 'function') {
        switchPage('test-suites', null);
    }
    await initTestSuiteBrowserPage();
    setSuiteBrowserHighlightedPath(artifactPath);
    await selectTestSuiteForBrowser(target.suite_path, directoryPath || '', { preserveHighlight: true });
}

async function openReportDiagnosisSourceFile() {
    const target = getCurrentReportDiagnosisTarget();
    if (!target) {
        showToast('未推断出源码路径', 'warning');
        return;
    }
    const { sourcePath, fallbackSourcePath, lineNumber } = getReportDiagnosisSourceLocation();
    if (!sourcePath) {
        showToast('未推断出源码路径', 'warning');
        return;
    }
    minimizeReportDiagnosisWorkbench();
    await openReportDiagnosisApkAnalysis({ sourcePath, fallbackSourcePath, lineNumber });
}

async function openReportDiagnosisApkAnalysis(options = {}) {
    const target = _requireDiagnosisArtifact();
    if (!target) return;

    const data = (window.reportDiagnosis || {}).data || {};
    const sourcePath = options.sourcePath || buildReportDiagnosisSourcePath(target);
    const fallbackSourcePath = options.fallbackSourcePath || buildReportDiagnosisSourcePath(target);
    const lineNumber = Number(options.lineNumber || data?.failure_location?.line_number || target?.source_guess?.line_number || 0) || null;
    state.suiteBrowser.selectedSuitePath = target.suite_path || state.suiteBrowser.selectedSuitePath;
    await analyzeSuiteApk(target.artifact.path, {
        openSourcePath: sourcePath,
        openFallbackSourcePath: fallbackSourcePath,
        openSourceLine: lineNumber,
        diagnosisTarget: target
    });
}

async function enhanceReportDiagnosisWithSource(filePath, sourceCode) {
    const diag = window.reportDiagnosis || {};
    const data = diag.data || {};
    if (!data.test_name || !sourceCode || diag.enhanceInFlight) return;

    window.reportDiagnosis.enhanceInFlight = true;
    try {
        const result = await apiCall('/api/reports/diagnose', 'POST', {
            test_name: data.test_name || '',
            error_message: data.error_message || '',
            stack_trace: data.stack_trace || data.error_message || '',
            module: data.module || '',
            class_names: data.class_names || [],
            report_name: data.report_name || '',
            failure_index: data.failure_index || 0,
            test_type: data.suite_target?.test_type || '',
            suite_version: data.suite_target?.suite_version || '',
            android_version: data.android_version || '',
            source_path: filePath,
            source_code: sourceCode
        });
        if (result.success && result.data) {
            renderReportDiagnosis({
                ...result.data,
                suite_target: result.data.suite_target || data.suite_target
            });
            showToast('已结合反编译源码刷新 AI 诊断', 'success');
        }
    } catch (error) {
        debugLog('[Report Diagnosis] Source enhanced diagnosis failed:', error);
    } finally {
        window.reportDiagnosis.enhanceInFlight = false;
    }
}

function extractClassNames(testName, errorMessage) {
    const classNames = new Set();

    // 1. 从测试名称中提取类名（格式：com.android.test.ClassName#methodName）
    const testClassMatch = testName.match(/^([\w.]+)#/);
    if (testClassMatch) {
        classNames.add(testClassMatch[1]);
    }

    // 2. 从错误消息中提取实际的测试类（格式：ClassName#methodName）
    const errorTestMatch = errorMessage.match(/([\w.]+Test)#(\w+)/);
    if (errorTestMatch) {
        const actualTestClass = errorTestMatch[1];
        classNames.add(actualTestClass);
        debugLog(`[源码搜索] 从错误消息提取实际测试类: ${actualTestClass}`);
    }

    // 3. 从堆栈跟踪中提取实际失败的类（优先级最高）
    // 匹配格式: at com.example.ClassName.method(ClassName.kt:294)
    const stackTraceFilePattern = /at\s+[\w.$]+\.run\(([\w.]+)\.(kt|java):(\d+)\)/;
    const stackFileMatch = errorMessage.match(stackTraceFilePattern);
    if (stackFileMatch) {
        const actualFile = stackFileMatch[1]; // 如: AppFunctionManagerTest
        const extension = stackFileMatch[2];  // kt 或 java
        const lineNumber = stackFileMatch[3];

        // 从文件名提取类名（去掉内部类后缀）
        const actualClass = actualFile.split('$')[0];
        classNames.add(actualClass);
        debugLog(`[源码搜索] 从堆栈跟踪提取实际失败位置: ${actualClass}.${extension}:${lineNumber}`);
    }

    // 4. 从堆栈跟踪中提取所有相关类（at com.example.Class.method）
    const stackTracePattern = /at\s+([\w.]+)\./g;
    let match;
    while ((match = stackTracePattern.exec(errorMessage)) !== null) {
        const className = match[1];
        // 过滤掉常见的Java/Android框架类
        if (!className.startsWith('java.') &&
            !className.startsWith('javax.') &&
            !className.startsWith('android.') &&
            !className.startsWith('androidx.') &&
            !className.startsWith('com.google.')) {
            // 去掉内部类后缀（$1$2等）
            const cleanClassName = className.split('$')[0];
            classNames.add(cleanClassName);
        }
    }

    // 5. 从错误消息中提取其他类名（Java类名模式）
    const javaClassPattern = /(?:\s|^|at\s)([a-z][\w.]*\.[A-Z][\w\$]*)/g;
    while ((match = javaClassPattern.exec(errorMessage)) !== null) {
        const className = match[1];
        if (!className.startsWith('java.') &&
            !className.startsWith('javax.') &&
            !className.startsWith('android.') &&
            !className.startsWith('androidx.') &&
            !className.startsWith('com.google.')) {
            classNames.add(className);
        }
    }

    const result = Array.from(classNames).slice(0, 5);
    debugLog(`[源码搜索] 最终提取的类名列表: ${result.join(', ')}`);
    return result;
}

/**
 * 显示AI分析结果
 * @param {object} data - AI分析数据
 * @param {string} testName - 测试用例名称
 * @param {string} errorMessage - 错误消息
 */
function displayAIAnalysis(data, testName, errorMessage = '') {
    const modalId = 'ai-analysis-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    // 显示/隐藏由 ModalManager（registerDynamic → open）统一接管，
    // 这里不得写 style.display 覆盖其所有权。

    let html = `
        <div class="modal-content modal-xl tech-dialog tech-dialog--report-ai"><svg class="tech-frame" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M16 1H986L999 14V584L984 599H16L1 584V16Z" vector-effect="non-scaling-stroke"/></svg><span class="tech-corner-mark tech-corner-mark--tl" aria-hidden="true"></span><span class="tech-corner-mark tech-corner-mark--br" aria-hidden="true"></span>
            <div class="modal-header tech-dialog-header"><svg class="tech-header-rule" viewBox="0 0 1000 16" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M0 15H490L506 1H916" vector-effect="non-scaling-stroke"/></svg>
                <div class="tech-title-group"><span class="tech-title-glyph"><svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#chart"></use></svg></span><span class="tech-title-divider" aria-hidden="true"></span><h2 class="modal-title">报错分析</h2></div>
                <div style="display: flex; align-items: center; gap: 10px;">
                    ${data.source_code_fetched ? '<span class="ai-report-chip ui-status is-success">✓ 源码已获取</span>' : ''}
                    ${data.ai_enabled === false ? '<span class="ai-report-chip ui-status is-warning">规则分析</span>' : '<span class="ai-report-chip ui-status is-accent">AI增强</span>'}
                    ${data.ai_model ? `<span class="ai-report-chip ui-status is-info">${escapeHtml(data.ai_model)}</span>` : ''}
                    <button type="button" class="modal-close" aria-label="关闭" data-click="closeAIAnalysisModal" data-a0="${modalId}">×</button>
                </div>
            </div>
            <div class="modal-body">
    `;

    if (data.source_code_fetched && data.source_url) {
        html += `
            <div style="background: color-mix(in srgb, var(--primary-color) 12%, transparent); border-left: 4px solid var(--primary-color); border-radius: 8px; padding: 14px; margin-bottom: 16px;">
                <div class="ai-report-copy-text ai-report-body ai-report-source-heading" style="margin-bottom: 6px; color: var(--primary-light);">💻 源码信息</div>
                <div class="ai-report-copy-text ai-report-compact" style="color: var(--text-secondary); margin-bottom: 6px;">文件路径: ${escapeHtml(data.source_file_path || 'N/A')}</div>
                <a href="${escapeHtml(safeReportExternalUrl(data.source_url))}" target="_blank" rel="noopener" class="ai-report-source-link" style="color: var(--primary-light); text-decoration: none; display: inline-flex; align-items: center; gap: 4px;">
                    🔗 查看源码
                    <svg style="width: 12px; height: 12px;" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path>
                    </svg>
                </a>
            </div>
        `;
    }

    if (data.root_cause) {
        html += `
            <div style="background: color-mix(in srgb, var(--danger-color) 12%, transparent); border-left: 4px solid var(--danger-color); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div class="ai-report-copy-text ai-report-card" style="font-weight: var(--ui-type-weight-semibold); margin-bottom: 8px; color: var(--danger-color);">🎯 根本原因</div>
                <div class="ai-report-copy-text ai-report-body" style="color: var(--text-color); line-height: 1.6;">${escapeHtml(data.root_cause)}</div>
            </div>
        `;
    }

    if (data.analysis) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div class="ai-report-copy-text ai-report-card" style="font-weight: var(--ui-type-weight-semibold); margin-bottom: 12px;">📊 详细分析</div>
                <div class="ai-report-copy-text ai-report-small" style="line-height: 1.8; white-space: pre-wrap; word-break: break-word;">${escapeHtml(data.analysis)}</div>
            </div>
        `;
    }

    if (data.suggestions && data.suggestions.length > 0) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div class="ai-report-copy-text ai-report-card" style="font-weight: var(--ui-type-weight-semibold); margin-bottom: 12px;">💡 解决建议</div>
                <div style="display: flex; flex-direction: column; gap: 10px;">
                    ${data.suggestions.map((suggestion, idx) => `
                        <div style="display: flex; gap: 10px; align-items: flex-start;">
                            <span class="ai-report-index" style="background: var(--primary-gradient); color: var(--text-on-accent); width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0;">${idx + 1}</span>
                            <span class="ai-report-body" style="line-height: 1.6; color: var(--text-color);">${escapeHtml(suggestion)}</span>
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    if (data.related_docs && data.related_docs.length > 0) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div class="ai-report-copy-text ai-report-card" style="font-weight: var(--ui-type-weight-semibold); margin-bottom: 12px;">📚 相关文档</div>
                <div style="display: flex; flex-direction: column; gap: 8px;">
                    ${data.related_docs.map(doc => {
                        const docUrl = safeReportExternalUrl(doc.url);
                        if (!docUrl) return '';
                        return `
                        <a href="${escapeHtml(docUrl)}" target="_blank" rel="noopener" style="display: flex; align-items: center; gap: 10px; padding: 10px; background: var(--darker-bg); border-radius: 6px; text-decoration: none; color: var(--text-color); transition: all 0.2s;">
                            <span class="ai-report-icon" style="">📖</span>
                            <span class="ai-report-doc-title" style="flex: 1;">${escapeHtml(doc.title)}</span>
                            <span class="ai-report-link-hint" style="color: var(--primary-color);">查看 →</span>
                        </a>
                    `;
                    }).join('')}
                </div>
            </div>
        `;
    }


    html += `
            <div class="modal-buttons tech-dialog-actions"><span class="tech-footer-decoration" aria-hidden="true"></span>
                <button type="button" class="btn-xs" data-click="closeAIAnalysisModal" data-a0="${modalId}">关闭</button>
                <button data-click="copyAIAnalysis" data-a0="${modalId}" class="btn-xs">📋 复制分析报告</button>
            </div>
            </div>
        </div>
    `;

    modal.innerHTML = html;
    document.body.appendChild(modal);

    ModalManager.registerDynamic(modal);

    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeAIAnalysisModal(modalId);
        }
    });
}

/**
 * 关闭AI分析模态框
 * @param {string} modalId - 模态框ID
 */
function closeAIAnalysisModal(modalId) {
    ModalManager.unregisterDynamic(modalId);
}

/**
 * 复制AI分析报告
 * @param {string} modalId - 模态框ID
 */
function copyAIAnalysis(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    if (!navigator.clipboard) {
        showToast('当前环境不支持自动复制', 'error');
        return;
    }

    const textElements = modal.querySelectorAll(".ai-report-copy-text");
    let text = 'CTS测试失败AI分析报告\n';
    text += '=' .repeat(40) + '\n\n';

    textElements.forEach(el => {
        const content = el.textContent.trim();
        if (content && !content.startsWith('复制') && !content.startsWith('关闭')) {
            text += content + '\n\n';
        }
    });

    navigator.clipboard.writeText(text).then(() => {
        showToast('✓ 分析报告已复制', 'success');
    }).catch(() => {
        showToast('复制失败', 'error');
    });
}


// act-bridge 委托目标。
window._actCloseRedmineAuthModal = function () {
    ModalManager.unregisterDynamic('redmine-auth-modal');
    resetReportUploadProgress();
};

window._actOpenUrlBlank = function (url) {
    window.open(url, '_blank', 'noopener');
};

window._actStopPropagation = function (event) {
    event.stopPropagation();
};
