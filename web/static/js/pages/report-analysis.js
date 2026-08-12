// ==================== Report Analysis ====================

function selectReportSource() {
    // 创建选择对话框
    const modal = document.createElement('div');
    modal.id = 'report-source-modal';
    modal.className = 'modal';
    modal.style.cssText = 'z-index: 10000;';
    modal.innerHTML = `
        <div class="modal-content modal-xs">
            <div class="modal-header">
                <span class="modal-title">选择上传方式</span>
                <span class="modal-close" onclick="closeReportSourceModal()">&times;</span>
            </div>
            <div class="modal-body" style="padding: 20px;">
                <div style="display: flex; flex-direction: column; gap: 12px;">
                    <button class="btn-md" onclick="selectReportFile()" style="width: 100%; justify-content: center;">
                        📄 上传文件
                    </button>
                    <div style="font-size: 10px; color: var(--text-secondary); text-align: center;">
                        支持 .xml, .zip, .rar, .tar.gz
                    </div>
                    <button class="btn-md" onclick="selectReportFolder()" style="width: 100%; justify-content: center;">
                        📁 上传文件夹
                    </button>
                    <div style="font-size: 10px; color: var(--text-secondary); text-align: center;">
                        选择包含 test_result.xml 的文件夹
                    </div>
                </div>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    // 注册到 ModalManager
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

        // 递归读取文件夹中的所有文件
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

        // 处理所有 items
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
                // 单文件
                handleReportFile(files[0]);
            } else {
                // 文件夹或多文件
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

    // 拖拽事件
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

    // 文件选择事件
    fileInput.addEventListener('change', async (e) => {
        if (e.target.files.length > 0) {
            await handleReportFile(e.target.files[0]);
            e.target.value = '';
        }
    });

    // 文件夹选择事件
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
    const uploadZone = $('report-upload-zone');
    const content = uploadZone?.querySelector('.report-upload-content');
    const progress = $('report-upload-progress');
    const progressFill = $('report-progress-fill');

    if (!progress || !progressFill) return;

    // 取消之前的请求
    if (currentRedmineRequest) {
        currentRedmineRequest.abort();
        currentRedmineRequest = null;
    }

    // 显示进度
    if (content) content.style.opacity = '0.5';
    progress.style.opacity = '1';
    progressFill.style.width = '10%';

    try {
        // 首先获取 Redmine 配置（带缓存，减少API调用）
        let redmineDomain;
        let redmineBaseUrl;

        try {
            const redmineConfig = await getRedmineConfig();
            redmineDomain = redmineConfig.domain;
            redmineBaseUrl = redmineConfig.base_url || `https://${redmineDomain}`;
        } catch (configError) {
            console.error('[Redmine] 配置获取失败:', configError);
            showToast('❌ Redmine 配置错误，请联系管理员', 'error');
            return; // 终止处理
        }

        const redminePathUrl = /\/(?:issues|attachments)(?:\/|$)/.test(url);
        const isConfiguredRedmineUrl = url.includes(redmineDomain);
        if (redminePathUrl && !isConfiguredRedmineUrl) {
            const publicUrl = url.replace(/^https?:\/\/[^/]+/, redmineBaseUrl.replace(/\/$/, ''));
            notifyOperationResult('报告分析失败', `请使用公网 Redmine 地址：${publicUrl}`, 'warning', 'report-analysis', {
                source: 'url'
            });
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 1000);
            return;
        }

        // 检测是否为配置中的公网 Redmine URL
        const isRedmineUrl = isConfiguredRedmineUrl;
        if (isRedmineUrl) {
            // 检查是否是直接的附件 URL (如 /attachments/2604033)
            const attachmentMatch = url.match(/\/attachments\/(\d+)/);
            const issueMatch = url.match(/\/issues\/(\d+)/);

            if (attachmentMatch && !issueMatch) {
                // 直接的附件 URL，跳过提取步骤，直接使用 analyze-url
                showToast('📎 检测到 Redmine 附件 URL，直接分析...', 'info');
                // 直接跳到 analyze-url 调用，不执行下面的 issue 提取逻辑
            } else if (issueMatch) {
                // 是问题页面，尝试获取第一个附件
                showToast('📋 检测到 Redmine 问题页面，正在提取附件...', 'info');
                progressFill.style.width = '15%';

                try {
                    // 调用后端 API 提取附件
                    const extractResponse = await fetch('/api/reports/extract-redmine-attachment', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ issue_url: url })
                    });

                    const extractResult = await extractResponse.json();

                    if (extractResult.success && extractResult.attachment_url) {
                        showToast(`📎 找到附件: ${extractResult.filename || '未知'}`, 'info');
                        url = extractResult.attachment_url;
                        context.source_issue_id = context.source_issue_id || issueMatch[1];
                        context.source_issue_url = context.source_issue_url || originalUrl;
                        debugLog('[Report Analysis] Found attachment:', extractResult.filename);
                    } else {
                        throw new Error(extractResult.error || '无法提取附件');
                    }
                } catch (extractError) {
                    showToast(`❌ ${extractError.message}`, 'error');
                    setTimeout(() => {
                        if (progress) progress.style.opacity = '0';
                        if (content) content.style.opacity = '1';
                    }, 2000);
                    return;
                }
            }

            showToast('🔐 检测到 Redmine URL，使用服务器端处理...', 'info');
            progressFill.style.width = '20%';

            // 创建 AbortController 用于取消请求
            const controller = new AbortController();
            currentRedmineRequest = controller;

            // 调用后端 API（使用服务器端存储的凭证）
            const response = await fetch('/api/reports/analyze-url', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    url: url,
                    source_issue_id: context.source_issue_id || '',
                    source_issue_url: context.source_issue_url || '',
                    use_redmine_auth: true  // 使用存储的 Redmine 凭证
                }),
                signal: controller.signal
            });

            progressFill.style.width = '70%';

            const result = await response.json();

            progressFill.style.width = '100%';

            if (result.success) {
                currentRedmineRequest = null;  // 重置请求控制器
                setTimeout(() => {
                    if (progress) progress.style.opacity = '0';
                    if (content) content.style.opacity = '1';
                    displayReportAnalysis(result.data);
                    notifyOperationResult(
                        '报告分析完成',
                        result.filename || '附件分析完成',
                        'success',
                        'report-analysis',
                        { source: 'url', filename: result.filename || '' }
                    );
                }, 300);
            } else {
                currentRedmineRequest = null;  // 重置请求控制器
                // 如果需要凭证，显示凭证输入框
                if (result.requires_auth) {
                    showRedmineAuthDialog(url, uploadZone, content, progress, progressFill, context);
                } else {
                    notifyOperationResult('报告分析失败', result.error || '未知错误', 'error', 'report-analysis', {
                        source: 'url'
                    });
                    setTimeout(() => {
                        if (progress) progress.style.opacity = '0';
                        if (content) content.style.opacity = '1';
                    }, 2000);
                }
            }
            return;
        }

        // 非 Redmine URL，使用服务器端下载
        showToast('正在从 URL 下载附件...', 'info');

        progressFill.style.width = '30%';

        // 创建 AbortController 用于取消请求
        const controller = new AbortController();
        currentRedmineRequest = controller;

        const response = await fetch('/api/reports/analyze-url', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ url: url }),
            signal: controller.signal
        });

        progressFill.style.width = '80%';

        const result = await response.json();

        progressFill.style.width = '100%';

        if (result.success) {
            currentRedmineRequest = null;  // 重置请求控制器
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
                displayReportAnalysis(result.data);
                notifyOperationResult(
                    '报告分析完成',
                    result.filename || '附件分析完成',
                    'success',
                    'report-analysis',
                    { source: 'url', filename: result.filename || '' }
                );
            }, 300);
        } else {
            currentRedmineRequest = null;  // 重置请求控制器
            notifyOperationResult('报告分析失败', result.error || '未知错误', 'error', 'report-analysis', {
                source: 'url'
            });
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 2000);
        }
    } catch (error) {
        currentRedmineRequest = null;  // 重置请求控制器
        if (error.name === 'AbortError') {
            debugLog('请求被取消');
            return;
        }
        console.error('URL attachment analysis error:', error);
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis', { source: 'url' });
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
}

function showRedmineAuthDialog(url, uploadZone, content, progress, progressFill, context = {}) {
    window._pendingRedmineDropContext = context || {};
    // 显示 Redmine 凭证输入对话框
    const modal = document.createElement('div');
    modal.id = 'redmine-auth-modal';
    modal.className = 'modal show';
    modal.style.cssText = 'z-index: 10000;';
    modal.innerHTML = `
        <div class="modal-content modal-xs">
            <div class="modal-header">
                <span class="modal-title">🔐 Redmine 认证</span>
                <span class="modal-close" onclick="ModalManager.unregisterDynamic('redmine-auth-modal'); resetReportUploadProgress();">&times;</span>
            </div>
            <div class="modal-body">
                <p style="margin-bottom: 15px;">请输入 Redmine 账号密码以自动下载附件：</p>
                <form onsubmit="event.preventDefault(); submitRedmineAuth('${url}');" autocomplete="off">
                <div class="modal-form-row">
                    <label>用户名</label>
                    <input type="text" id="redmine-username" placeholder="输入 Redmine 用户名" autocomplete="username">
                </div>
                <div class="modal-form-row">
                    <label>密码</label>
                    <input type="password" id="redmine-password" placeholder="输入 Redmine 密码" autocomplete="current-password"
                           onkeypress="if(event.key === 'Enter') submitRedmineAuth('${url}')">
                </div>
                </form>
                <div class="modal-buttons">
                    <button class="btn-xs" onclick="ModalManager.unregisterDynamic('redmine-auth-modal'); resetReportUploadProgress();">取消</button>
                    <button class="btn-xs btn-primary" onclick="submitRedmineAuth('${url}')">确定</button>
                </div>
                <p style="font-size: 11px; color: var(--text-secondary); margin-top: 15px; text-align: center;">
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

    // 关闭对话框
    ModalManager.unregisterDynamic('redmine-auth-modal');

    // 显示进度
    const uploadZone = $('report-upload-zone');
    const content = uploadZone?.querySelector('.report-upload-content');
    const progress = $('report-upload-progress');
    const progressFill = $('report-progress-fill');

    if (content) content.style.opacity = '0.5';
    progress.style.opacity = '1';
    progressFill.style.width = '30%';

    try {
        showToast('⬇️ 正在从 Redmine 下载附件...', 'info');

        const response = await fetch('/api/reports/analyze-url', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                url: url,
                source_issue_id: window._pendingRedmineDropContext?.source_issue_id || '',
                source_issue_url: window._pendingRedmineDropContext?.source_issue_url || '',
                redmine_username: username,
                redmine_password: password
            })
        });

        progressFill.style.width = '80%';

        const result = await response.json();

        progressFill.style.width = '100%';

        if (result.success) {
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
                displayReportAnalysis(result.data);
                notifyOperationResult(
                    '报告分析完成',
                    result.filename || '附件分析完成',
                    'success',
                    'report-analysis',
                    { source: 'redmine', filename: result.filename || '' }
                );
            }, 300);
        } else {
            notifyOperationResult('报告分析失败', result.error || '未知错误', 'error', 'report-analysis', {
                source: 'redmine'
            });
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 2000);
        }
    } catch (error) {
        console.error('Redmine auth error:', error);
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis', { source: 'redmine' });
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
}

async function handleReportFile(file) {
    const fileName = file?.name || '测试报告';
    const uploadZone = $('report-upload-zone');
    const content = uploadZone?.querySelector('.report-upload-content');
    const progress = $('report-upload-progress');
    const progressFill = $('report-progress-fill');

    if (!progress || !progressFill) return;

    // 显示进度
    if (content) content.style.opacity = '0.5';
    progress.style.opacity = '1';
    progressFill.style.width = '0%';

    try {
        const formData = createFormData(AnalysisMode.UPLOAD, { file: file });

        const upload = await postFormDataWithProgress('/api/reports/analyze', formData, (percent) => {
            progressFill.style.width = `${Math.min(95, Math.max(5, percent * 0.95))}%`;
        });
        const result = upload.result;

        progressFill.style.width = '100%';

        if (result.success) {
            setTimeout(() => {
                if (upload.generation !== reportUploadGeneration) return;
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
                displayReportAnalysis(result.data);
                notifyOperationResult(
                    '报告分析完成',
                    `成功分析 ${fileName}`,
                    'success',
                    'report-analysis',
                    { filename: fileName }
                );
            }, 300);
        } else {
            notifyOperationResult('报告分析失败', result.error || '未知错误', 'error', 'report-analysis', {
                filename: fileName
            });
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 1000);
        }
    } catch (error) {
        if (error?.name === 'AbortError') return;
        console.error('Report analysis error:', error);
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis', { filename: fileName });
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
}

function postFormDataWithProgress(url, formData, onProgress) {
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
            if (isCurrent() && event.lengthComputable && onProgress) {
                onProgress((event.loaded / event.total) * 100, event.loaded, event.total);
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
        applyClientIdentityHeadersToXhr(xhr);
        xhr.send(formData);
    });
}

async function handleReportFolder(files) {
    const uploadZone = $('report-upload-zone');
    const content = uploadZone?.querySelector('.report-upload-content');
    const progress = $('report-upload-progress');
    const progressFill = $('report-progress-fill');

    if (!progress || !progressFill) return;

    // 显示进度
    if (content) content.style.opacity = '0.5';
    progress.style.opacity = '1';
    progressFill.style.width = '0%';

    let fileCount = 0;
    try {
        const formData = new FormData();
        formData.append('mode', 'upload');

        // 添加所有文件到 FormData，保持文件夹结构
        for (let i = 0; i < files.length; i++) {
            const file = files[i];

            // 使用 webkitRelativePath 或文件名
            const filename = file.webkitRelativePath || file.name;

            formData.append('files[]', file, filename);
            fileCount++;
        }

        debugLog(`Uploading ${fileCount} files...`);
        const upload = await postFormDataWithProgress('/api/reports/analyze', formData, (percent) => {
            progressFill.style.width = `${Math.min(95, Math.max(5, percent * 0.95))}%`;
        });
        const result = upload.result;

        progressFill.style.width = '100%';

        if (result.success) {
            setTimeout(() => {
                if (upload.generation !== reportUploadGeneration) return;
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
                displayReportAnalysis(result.data);
                notifyOperationResult(
                    '报告分析完成',
                    `成功分析 ${fileCount} 个文件`,
                    'success',
                    'report-analysis',
                    { file_count: fileCount }
                );
            }, 300);
        } else {
            notifyOperationResult('报告分析失败', result.error || '未知错误', 'error', 'report-analysis', {
                file_count: fileCount
            });
            if (result.message) {
                console.error('Analysis error details:', result.message);
            }
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 1000);
        }
    } catch (error) {
        if (error?.name === 'AbortError') return;
        console.error('Report folder analysis error:', error);
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis', { file_count: fileCount });
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
}

function ensureReportAnalysisResultStructure() {
    const resultDiv = $('report-analysis-result');
    if (!resultDiv) return null;

    if (!$('report-summary') || !$('report-details') || !$('report-failures') || !$('report-failure-list')) {
        resultDiv.innerHTML = `
            <div style="background: var(--light-bg); border-radius: 8px; border: 1px solid var(--border-color); padding: 20px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <div style="font-size: 16px; font-weight: 600;">📊 分析结果</div>
                    <button class="btn-xs" onclick="resetReportAnalysis()">清除</button>
                </div>
                <div id="report-summary" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-bottom: 20px;"></div>
                <div id="report-details" style="font-size: 12px; color: var(--text-primary);"></div>
            </div>
            <div id="report-failures" style="background: var(--light-bg); border-radius: 8px; border: 1px solid var(--border-color); padding: 20px; display: none;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                    <div style="font-size: 12px; font-weight: 600;">❌ 失败用例</div>
                </div>
                <div id="report-failure-list" style="max-height: 580px; overflow-y: auto;"></div>
            </div>
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
            worker_id: provenance.worker_id || workspaceWorkerId(),
            cluster_job_id: provenance.cluster_job_id || '',
            attempt_id: provenance.attempt_id || '',
            automation_run_id: provenance.automation_run_id || '',
            report_id: data.report_id || provenance.report_id || '',
            report_timestamp: data.report_timestamp || provenance.timestamp || '',
            artifact_id: provenance.artifact_id || '',
            gerrit_change_id: provenance.gerrit_change_id || '',
            gerrit_patchset: provenance.gerrit_patchset || '',
            redmine_issue_id: provenance.redmine_issue_id || '',
            suite_path: provenance.suite_path || '',
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

    // 清空之前的内容
    if (summaryDiv) summaryDiv.innerHTML = '';
    if (detailsDiv) detailsDiv.innerHTML = '';
    if (failureList) failureList.innerHTML = '';
    if (failuresDiv) failuresDiv.style.display = 'none';

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

    // 显示结果区域
    resultDiv.style.display = 'block';

    // 生成摘要
    if (summaryDiv && data.summary) {
        const summary = data.summary;

        const summaryHTML = `
            ${data.details && data.details.test_type ? `
                <div>
                    <span class="summary-label">测试类型：</span>
                    <span class="summary-value">${data.details.test_type}</span>
                </div>
            ` : ''}
            ${data.details && data.details.suite_version ? `
                <div>
                    <span class="summary-label">套件版本：</span>
                    <span class="summary-value">${data.details.suite_version}</span>
                </div>
            ` : ''}
            ${data.details && data.details.android_version ? `
                <div>
                    <span class="summary-label">Android版本：</span>
                    <span class="summary-value">${data.details.android_version}</span>
                </div>
            ` : ''}
            ${data.details && data.details.soc_platform ? `
                <div>
                    <span class="summary-label">SOC平台：</span>
                    <span class="summary-value">${data.details.soc_platform}</span>
                </div>
            ` : ''}
            <div>
                <span class="summary-label">总用例数：</span>
                <span class="summary-value">${summary.total || 0}</span>
            </div>
            <div>
                <span class="summary-label">通过：</span>
                <span class="summary-value pass">${summary.pass || 0}</span>
            </div>
            <div>
                <span class="summary-label">失败：</span>
                <span class="summary-value fail">${summary.fail || 0}</span>
            </div>
            <div>
                <span class="summary-label">通过率：</span>
                <span class="summary-value rate">${summary.pass_rate || '0%'}</span>
            </div>
            <div>
                <span class="summary-label">测试报告：</span>
                <span class="summary-value">${data.report_name || data.test_result?.test_name || 'N/A'}</span>
            </div>
        `;

        summaryDiv.innerHTML = summaryHTML;
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
            <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;border-top:1px solid var(--border-color);padding-top:10px;">
                ${fields.map(([label, value]) => `<span style="padding:3px 7px;border:1px solid var(--border-color);border-radius:10px;"><b>${escapeHtml(label)}</b> ${escapeHtml(value)}</span>`).join('')}
                ${provenance.cluster_job_id ? '<button class="btn-xs" data-provenance-page="cluster">打开集群任务</button>' : ''}
                ${provenance.automation_run_id ? '<button class="btn-xs" data-provenance-page="automation">打开 ATS</button>' : ''}
                ${provenance.gerrit_change_id ? '<button class="btn-xs" data-provenance-page="gerrit-dashboard">打开 Gerrit</button>' : ''}
                ${provenance.redmine_issue_id ? '<button class="btn-xs" data-provenance-page="redmine-agent">打开 Redmine</button>' : ''}
            </div>` : '';
        detailsDiv.querySelectorAll('[data-provenance-page]').forEach(button => {
            button.addEventListener('click', () => window.GmsWorkspace?.navigate(button.dataset.provenancePage));
        });
    }

    // 显示失败用例
    if (failuresDiv && failureList && data.failures && data.failures.length > 0) {
        failuresDiv.style.display = 'block';

        // 测试类型在循环外提取（每份报告固定不变）
        const reportTestType = escapeJsAttr((data.details && data.details.test_type) || '');

        const failuresHTML = data.failures.map((failure, idx) => {
            // 解析失败信息
            const reasonText = failure.reason || '无失败原因';

            // 使用后端返回的模块名，如果没有则使用默认值
            const moduleName = failure.module || '未知模块';

            // 使用后端返回的测试用例名
            const testCaseName = failure.name || '未知用例';

            // 格式化完整堆栈信息，保留换行和缩进
            const formattedStackTrace = (reasonText || '无失败原因')
                .split('\n')
                .map(line => '&nbsp;&nbsp;&nbsp;&nbsp;' + line
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                )
                .join('<br>');

            // 从 report_name 中提取 Redmine issue ID（使用预编译的正则表达式）
            const reportName = window.currentReportName || '';
            const redmineIssueMatch = reportName.match(/^Redmine-(\d+)-/);
            const issueIdFromReport = redmineIssueMatch ? redmineIssueMatch[1] : '';

            // 转义用于 onclick 属性的参数
            const escModuleName = escapeJsAttr(moduleName);
            const escTestCaseName = escapeJsAttr(testCaseName);

            return `
                <div class="report-failure-card">
                    <div class="report-failure-card-head">
                        <div class="report-failure-title">
                            <div>测试模块: <span>${escapeHtml(moduleName)}</span></div>
                            <div>测试用例: <code>${escapeHtml(testCaseName)}</code></div>
                        </div>
                        <div class="report-failure-actions">
                            ${issueIdFromReport ? `<button class="report-failure-action reply" onclick="openRedmineReplyModal('${escModuleName}', '${escTestCaseName}', '${idx}', '${issueIdFromReport}')" data-reason="${encodeURIComponent(reasonText)}">Redmine回复</button>` : ''}
                            <button class="report-failure-action test" onclick="goToTestCase('${reportTestType}', '${escModuleName}', '${escTestCaseName}')">单测用例</button>
                            <button class="report-failure-action diagnose" onclick="openReportDiagnosisModal(${idx})">报错诊断</button>
                        </div>
                    </div>
                    <div>
                        <div class="report-failure-reason-label">报错信息</div>
                        <div class="failure-reason" id="failure-reason-${idx}" style="font-size: 11px; font-family: 'Courier New', monospace; white-space: pre-wrap; word-wrap: break-word;">${formattedStackTrace}</div>
                        <div class="failure-reason-raw" id="failure-reason-raw-${idx}" style="display: none;">${escapeHtml(reasonText)}</div>
                    </div>
                </div>
            `;
        }).join('');

        failureList.innerHTML = failuresHTML;
    } else if (failuresDiv) {
        failuresDiv.style.display = 'block';
        if (failureList) {
            failureList.innerHTML = `
                <div class="report-empty-success">
                    <b>未发现失败用例</b>
                    <span>这份报告没有可诊断的失败项，可以清除后继续分析下一份报告。</span>
                </div>
            `;
        }
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
                    <span class="dx-status-pill">诊断中</span>
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
                <div class="dx-loading-card"><span>3</span>OpenGrok 源码搜索</div>
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

function switchReportDiagnosisPanel(panelName) {
    document.querySelectorAll('[data-dx-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.dxTab === panelName);
    });
    document.querySelectorAll('[data-dx-panel]').forEach(panel => {
        panel.classList.toggle('active', panel.dataset.dxPanel === panelName);
    });
}

function renderDxMetric(label, value) {
    return `
        <div class="dx-metric">
            <span class="dx-metric-label">${escapeHtml(label)}</span>
            <span class="dx-metric-value">${escapeHtml(value || '无')}</span>
        </div>
    `;
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

    if (diagnosticSummary) {
        diagnosticSummary.innerHTML = `
            <div class="dx-hero">
                <div class="dx-title-row">
                    <div class="dx-title-main">${escapeHtml(data.test_name || data.report_name || '诊断工作台')}</div>
                    <div class="dx-pill-row">
                        <span class="dx-status-pill">${escapeHtml(aiStatusLabel)}</span>
                        <span class="dx-status-pill">${escapeHtml(suiteTarget.test_type || data.test_type || '未知类型')}</span>
                        <span class="dx-status-pill">${escapeHtml(suiteTarget.suite_version || data.suite_version || '未知版本')}</span>
                    </div>
                </div>
                <div class="dx-compact-line">${escapeHtml([data.module || currentFailure.module || '', suiteArtifactPath, displaySourcePath].filter(Boolean).join(' | ') || '当前失败项诊断')}</div>
            </div>
        `;
    }

    const sourceCards = sourceResults.length > 0
        ? sourceResults.map(item => `
            <div class="dx-list-item${item.url ? ' dx-clickable' : ''}" ${item.url ? `onclick="window.open('${escapeJsAttr(item.url)}', '_blank')"` : ''}>
                <div class="dx-list-head">
                    <div class="dx-list-title">${escapeHtml(item.type || 'source')}</div>
                    ${item.url ? `<a class="dx-link" href="${escapeHtml(item.url)}" target="_blank" onclick="event.stopPropagation()">打开 OpenGrok</a>` : ''}
                </div>
                <div class="dx-list-path dx-list-path-inline">${escapeHtml(item.path || item.display_path || '')}${item.line ? `<span>:${escapeHtml(String(item.line))}</span>` : ''}</div>
            </div>
        `).join('')
        : renderDxEmpty('未检索到 OpenGrok 源码结果');

    const kbCards = kbResults.length > 0
        ? kbResults.map(item => `
            <div class="dx-list-item">
                <div class="dx-list-title">#${escapeHtml(String(item.id || ''))} ${escapeHtml(item.subject || '')}</div>
                <div class="dx-list-meta">${escapeHtml(item.status_name || '')} | ${escapeHtml(item.updated_on || '')}</div>
                <div class="dx-list-text">${escapeHtml((item.solution_summary || item.description || '').slice(0, 260))}</div>
            </div>
        `).join('')
        : renderDxEmpty('未命中知识库');

    const candidateCards = !hasExactArtifact && artifactCandidates.length > 0
        ? `<details class="dx-details"><summary>候选构件 (${artifactCandidates.length})</summary><div class="dx-list">${
            artifactCandidates.slice(0, 5).map((item, idx) => `
                <button class="dx-candidate" onclick="openReportDiagnosisArtifactCandidate(${idx})">
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
                <span class="dx-exempt-badge">✓ 已豁免</span>
                <span class="dx-exempt-title">命中 Google Mainline 已知豁免（通常无需本地修复）</span>
            </div>
            <div class="dx-exempt-list">
                ${exemptions.map(item => `
                    <div class="dx-exempt-item">
                        <div class="dx-exempt-item-head">
                            <b class="dx-exempt-id">exemption ${escapeHtml(String(item.exemption_id || ''))}</b>
                            <span class="dx-exempt-meta">${escapeHtml([item.issue_type, item.test_module].filter(Boolean).join(' · '))}</span>
                            ${item.match_kind === 'fuzzy' ? '<span class="dx-exempt-kind">模糊匹配</span>' : ''}
                            ${item.source_url ? `<a class="dx-link" href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener">来源</a>` : ''}
                        </div>
                        <div class="dx-exempt-case">${escapeHtml(item.test_case || '')}</div>
                        ${item.issue_text ? `<div class="dx-exempt-text">${escapeHtml(String(item.issue_text).slice(0, 280))}</div>` : ''}
                    </div>
                `).join('')}
            </div>
        </section>`
        : '';

    const actionPanel = `
        <section class="dx-section dx-action-section">
            <div class="dx-section-title">下一步动作</div>
            <button type="button" class="dx-action-card" onclick="openReportDiagnosisTestCase('${escapeJsAttr(reportTestType)}', '${escapeJsAttr(data.module || currentFailure.module || '')}', '${escapeJsAttr(data.test_name || currentFailure.name || '')}')">
                <b>执行单测复现</b>
                <span>跳到测试页并填入模块/用例</span>
            </button>
            ${suiteArtifact ? `<button type="button" class="dx-action-card" onclick="openReportDiagnosisSuiteBrowser()"><b>打开测试套件</b><span>${escapeHtml(suiteArtifact.path || '')}</span></button>` : ''}
            ${issueIdFromReport ? `<button type="button" class="dx-action-card" onclick="openReportDiagnosisRedmineReply()"><b>Redmine 回复</b><span>基于诊断结论生成回复草稿</span></button>` : ''}
            ${issueIdFromReport ? `<button type="button" class="dx-action-card" onclick="openReportAnalysisRedmineAgent('${escapeJsAttr(issueIdFromReport)}')"><b>Redmine 工作台</b><span>查看工单历史、附件证据和相似案例</span></button>` : ''}
            <button type="button" class="dx-action-card" onclick="saveDiagnosisToWiki()"><b>📥 存为Wiki</b><span>把诊断结论沉淀到知识库</span></button>
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
                        <b>OpenGrok 源码或测试套件反编译</b>
                        <em>把定位依据、候选构件和源码搜索放在一起</em>
                    </div>
                </div>
                <div class="dx-source-layout">
                    <div class="dx-section dx-section-large">
                        <div class="dx-section-head">
                            <div class="dx-section-title">套件源码定位</div>
                            ${suiteArtifact ? `<button class="btn-xxs btn-primary" onclick="openReportDiagnosisSourcePreview()">反编译并预览源码</button>` : ''}
                        </div>
                        <div class="dx-locator-list">
                            ${renderDxLocatorRow('测试套件', suiteArtifactPath || '未定位')}
                            ${renderDxLocatorRow('源码路径猜测', displaySourcePath || '未推断')}
                        </div>
                        ${candidateCards}
                    </div>
                    <div class="dx-section">
                        <div class="dx-section-title">OpenGrok 源码搜索 <span>${sourceResults.length} 结果</span></div>
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
                        <pre class="dx-code">${escapeHtml(patchDraft || '无')}</pre>
                    </div>
                    <div class="dx-section">
                        <div class="dx-section-title">GMS 认证知识库</div>
                        <div class="dx-list">${kbCards}</div>
                    </div>
                </div>
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

// 提取类名的辅助函数
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
        const lineNumber = stackFileMatch[3]; // 行号

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

// 从堆栈跟踪中提取实际的失败位置信息
function extractFailureLocation(errorMessage) {
    // 匹配格式: at com.example.ClassName.method(ClassName.kt:294)
    // 或者: at com.example.ClassName.method(Class.java:100)
    const patterns = [
        /at\s+[\w.$]+\.run\(([\w.]+)\.(kt|java):(\d+)\)/,  // .kt:294 或 .java:100
        /at\s+[\w.$]+\.(\w+)\(([\w.]+)\.(kt|java):(\d+)\)/,  // 备用模式
    ];

    for (const pattern of patterns) {
        const match = errorMessage.match(pattern);
        if (match) {
            // 根据匹配组提取信息
            let fileName, fileType, lineNumber;

            if (match.length === 4) {
                // 第一个模式: match[1]=文件名, match[2]=扩展名, match[3]=行号
                fileName = match[1];
                fileType = match[2];
                lineNumber = match[3];
            } else if (match.length === 5) {
                // 第二个模式: match[2]=文件名, match[3]=扩展名, match[4]=行号
                fileName = match[2];
                fileType = match[3];
                lineNumber = match[4];
            }

            if (fileName && fileType && lineNumber) {
                const location = {
                    file_name: fileName,
                    file_type: fileType,  // 'kt' 或 'java'
                    line_number: lineNumber
                };

                debugLog(`[源码搜索] 📍 从堆栈跟踪提取失败位置:`, location);
                return location;
            }
        }
    }

    debugLog(`[源码搜索] ⚠️ 堆栈跟踪中未找到文件位置信息`);
    return null;
}

// 从错误信息中提取搜索关键词（优化版）
function extractKeywordsFromError(testCaseName, errorMessage) {
    debugLog(`[源码分析] 开始提取关键词，测试用例: ${testCaseName}`);

    // 1. 优先从测试用例名中提取核心功能名
    const functionMatch = testCaseName.match(/test(?:Atom|Statsd)_([A-Z][a-zA-Z0-9_]*)/);
    if (functionMatch) {
        const functionName = functionMatch[1];
        debugLog(`[源码分析] 提取到功能名: ${functionName}`);
        return functionName;
    }

    // 2. 从测试用例名中提取类名
    const classMatch = testCaseName.match(/([A-Z][a-zA-Z0-9_]*)Test/);
    if (classMatch) {
        const className = classMatch[1];
        debugLog(`[源码分析] 提取到类名: ${className}`);
        return className;
    }

    // 3. 从堆栈信息中提取失败的类名（排除工具类）
    const stackLines = errorMessage.split('\n');
    for (const line of stackLines) {
        const stackMatch = line.match(/at\s+([\w.$]+)\(([\w.]+):(\d+)\)/);
        if (stackMatch) {
            const fullClassName = stackMatch[1];
            const fileName = stackMatch[2];

            if (!fileName.includes('TestUtil') &&
                !fileName.includes('TestRunner') &&
                !fileName.includes('Assert') &&
                !fileName.includes('Mock')) {

                const classNameParts = fullClassName.split('.');
                const mainClassName = classNameParts[classNameParts.length - 1];
                const cleanClassName = mainClassName.split('$')[0];

                if (cleanClassName.length > 3 &&
                    !cleanClassName.includes('Util') &&
                    !cleanClassName.includes('Helper')) {

                    debugLog(`[源码分析] 从堆栈提取类名: ${cleanClassName}`);
                    return cleanClassName;
                }
            }
        }
    }

    // 4. 默认返回测试用例名的前部分
    const parts = testCaseName.split(/[.#_]/);
    const fallback = parts[parts.length - 1] || testCaseName;
    debugLog(`[源码分析] 使用默认关键词: ${fallback}`);
    return fallback;
}

// 源码分析失败用例（根据堆栈信息定位）
async function analyzeFailureWithSource(testName, errorMessage) {
    const modalId = 'source-analysis-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.style.cssText = 'z-index: 10000;';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 900px; max-height: 90vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">🔍 源码分析 - 正在定位失败位置...</span>
                <span class="modal-close" onclick="ModalManager.close('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; margin-bottom: 20px;">🔍</div>
                    <div style="color: var(--text-secondary); margin-bottom: 12px;">正在分析堆栈信息...</div>
                    <div style="font-size: 12px; color: var(--text-secondary);">自动提取文件位置并搜索源码</div>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    ModalManager.open(modalId);

    try {
        // 从堆栈跟踪提取失败位置
        const failureLocation = extractFailureLocation(errorMessage);

        // 提取搜索关键词
        const classNames = extractClassNames(testName, errorMessage);
        const keywords = classNames.length > 0 ? classNames[0] : extractKeywordsFromError(testName, errorMessage);

        // 构建快速访问卡片（等后端返回后再构建，使用实际路径）
        let quickLinksHtml = '';

        // 调用 AI 分析获取源码搜索结果
        const formData = createFormData(AnalysisMode.AI, {
            test_name: testName,
            error_message: errorMessage,
            stack_trace: errorMessage,
            class_names: JSON.stringify(classNames),
            failure_location: failureLocation ? JSON.stringify(failureLocation) : '',
            include_source_search: 'true'
        });

        const response = await fetch('/api/reports/analyze', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        if (!response.ok) {
            const errorDetail = result.detail || result.error || '未知错误';
            showModalError(modal, `分析失败: ${errorDetail}`);
            return;
        }

        modal.querySelector('.modal-title').textContent = '🔍 源码分析结果';

        if (result.success) {
            const data = result.data;
            let content = '';

            // 如果有失败位置，构建快速访问卡片（使用后端返回的实际路径）
            if (failureLocation && data.source_search_results && data.source_search_results.length > 0) {
                // 找到匹配失败位置的搜索结果
                const exactMatch = data.source_search_results.find(item =>
                    item.path.includes(failureLocation.file_name) &&
                    item.file_type === failureLocation.file_type
                );

                if (exactMatch) {
                    let openGrokUrl = exactMatch.url || buildOpenGrokUrl(exactMatch.path, exactMatch.line);

                    if (openGrokUrl) {
                        content += `
                            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                                <div style="color: white; font-size: 14px; font-weight: 600; margin-bottom: 12px;">🎯 快速访问 - 失败位置</div>
                                <div style="background: rgba(255, 255, 255, 0.1); border-radius: 6px; padding: 12px; margin-bottom: 10px;">
                                    <div style="color: rgba(255, 255, 255, 0.8); font-size: 11px; margin-bottom: 4px;">📁 失败位置</div>
                                    <div style="color: white; font-family: 'Courier New', monospace; font-size: 13px; margin-bottom: 8px;">
                                        ${exactMatch.path.split('/').pop()} :${failureLocation.line_number}
                                    </div>
                                    <a href="${openGrokUrl}" target="_blank" style="display: inline-block; padding: 6px 12px; background: white; color: #667eea; text-decoration: none; border-radius: 4px; font-size: 12px; font-weight: 600;">
                                        🚀 直接跳转到源码 ↗
                                    </a>
                                </div>
                            </div>
                        `;
                    }
                }
            }

            // 显示源码搜索结果
            if (data.source_search_results && data.source_search_results.length > 0) {
                content += '<div style="margin-top: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px; border-left: 3px solid #9c27b0;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: #9c27b0;">🔍 AI 智能源码搜索</div>';
                content += '<div style="max-height: 400px; overflow-y: auto;">';

                data.source_search_results.forEach(item => {
                    const fileIcon = item.file_type === 'kt' ? '🔷' : (item.file_type === 'java' ? '☕' : '📄');
                    // 优先使用 item.url，如果没有则根据配置生成
                    let itemUrl = item.url;
                    if (!itemUrl) {
                        itemUrl = buildOpenGrokUrl(item.path, item.line);
                    }

                    const linkHtml = itemUrl ?
                        `<a href="${itemUrl}" target="_blank" style="font-size: 11px; color: #667eea; text-decoration: none; white-space: nowrap; font-weight: 600;">
                            在 OpenGrok 中查看 →
                        </a>` :
                        '<span style="font-size: 10px; color: #999;">无链接</span>';

                    content += `
                        <div style="background: white; border-radius: 4px; padding: 10px; margin-bottom: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                <div style="display: flex; align-items: center; gap: 6px;">
                                    <span style="font-size: 14px;">${fileIcon}</span>
                                    <span style="font-family: monospace; font-size: 12px; color: #1976d2; font-weight: 600;">
                                        ${item.type}
                                    </span>
                                </div>
                                ${linkHtml}
                            </div>
                            <div style="font-family: monospace; font-size: 11px; color: #616161; margin-bottom: 4px;">
                                📁 ${item.path}
                            </div>
                            <div style="font-family: monospace; font-size: 10px; color: #424242; background: #f5f5f5; padding: 6px; border-radius: 3px;">
                                行 ${item.line || 'N/A'} ${item.project ? '· 项目：' + item.project : ''}
                            </div>
                        </div>
                    `;
                });

                content += '</div></div>';
            }

            modal.querySelector('.modal-body').innerHTML = content || '<div style="padding: 20px; text-align: center;">未找到源码搜索结果</div>';
        }
    } catch (error) {
        showModalError(modal, `分析失败: ${error.message}`);
    }
}

// AI分析失败用例（自动搜索源码）
async function aiAnalyzeFailureReport(testName, errorMessage) {
    const modalId = 'ai-analysis-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';  // 不直接添加 show 类
    modal.style.cssText = 'z-index: 10000;';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 800px; max-height: 85vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">🤖 正在分析报错并搜索源码...</span>
                <span class="modal-close" onclick="ModalManager.close('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; margin-bottom: 20px;">🤖</div>
                    <div style="color: var(--text-secondary); margin-bottom: 12px;">正在分析失败原因，请稍候...</div>
                    <div style="font-size: 12px; color: var(--text-secondary);">自动提取类名并搜索相关源码</div>
                </div>
            </div>
        </div>
    `;

    // 添加到 DOM
    document.body.appendChild(modal);

    // 使用 ModalManager 打开（这样 Esc 键才会生效）
    ModalManager.open(modalId);

    try {
        // 自动提取类名
        const classNames = extractClassNames(testName, errorMessage);

        // 从堆栈跟踪提取失败位置
        const failureLocation = extractFailureLocation(errorMessage);

        // 更新模态框显示正在搜索源码
        // 将类名列表格式化为多行显示
        const classNamesList = classNames.map((name, index) => {
            const prefix = index === 0 ? '' : '├── ';
            return `${prefix}${name}`;
        }).join('<br>');

        modal.querySelector('.modal-body').innerHTML = `
            <div style="text-align: center; padding: 40px;">
                <div style="font-size: 30px; margin-bottom: 20px;">🔍</div>
                <div style="color: var(--text-secondary); margin-bottom: 12px;">正在搜索相关源码...</div>
                <div style="font-size: 16px; color: var(--text-secondary); margin-bottom: 8px;">找到 ${classNames.length} 个相关类</div>
                <div style="font-size: 16px; font-family: 'Courier New', monospace; color: var(--primary-color); text-align: left; display: inline-block; max-width: 90%;">${classNamesList}</div>
                ${failureLocation ? `<div style="font-size: 16px; color: var(--success-color); margin-top: 8px;">📍 失败位置: ${failureLocation.file_name}.${failureLocation.file_type}:${failureLocation.line_number}</div>` : ''}
            </div>
        `;

        const formData = createFormData(AnalysisMode.AI, {
            test_name: testName,
            error_message: errorMessage,
            stack_trace: errorMessage,
            class_names: JSON.stringify(classNames),
            failure_location: failureLocation ? JSON.stringify(failureLocation) : '',
            include_source_search: 'true'  // 启用源码搜索
        });

        const response = await fetch('/api/reports/analyze', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        debugLog('[AI Analysis] API响应:', result);

        // 检查HTTP状态码
        if (!response.ok) {
            // 处理HTTP错误（FastAPI的HTTPException返回 {detail: "error message"}）
            const errorDetail = result.detail || result.error || '未知错误';
            console.error('[AI Analysis] HTTP错误:', response.status, errorDetail);
            showModalError(modal, `分析失败: ${errorDetail}`);
            return;
        }

        // 更新模态框内容
        modal.querySelector('.modal-title').textContent = '🤖 报错分析结果';

        if (result.success) {
            const data = result.data;

            // 验证必需字段
            if (!data.root_cause && !data.analysis && !data.suggestions) {
                console.error('[AI Analysis] 返回数据缺少必需字段:', data);
                showModalError(modal, 'AI分析结果格式异常，缺少必需字段。请查看后端日志了解详情。');
                return;
            }

            let content = '';

            // 根本原因
            if (data.root_cause) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px; border-left: 3px solid var(--warning-color);">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--warning-color);">🎯 根本原因</div>';
                content += `<div style="font-size: 13px; line-height: 1.6;">${escapeHtml(data.root_cause)}</div>`;
                content += '</div>';
            }

            // 详细分析
            if (data.analysis) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--primary-color);">📊 详细分析</div>';
                content += `<div style="font-size: 13px; line-height: 1.6; white-space: pre-wrap;">${escapeHtml(data.analysis)}</div>`;
                content += '</div>';
            }

            // 解决建议
            if (data.suggestions && data.suggestions.length > 0) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--success-color);">✅ 解决建议</div>';
                content += '<ol style="margin: 4px 0; padding-left: 20px; font-size: 13px; line-height: 1.8;">';
                data.suggestions.forEach((suggestion, index) => {
                    content += `<li style="margin-bottom: 6px;">${escapeHtml(suggestion)}</li>`;
                });
                content += '</ol></div>';
            }

            // 相关文档
            if (data.related_docs && data.related_docs.length > 0) {
                content += '<div style="padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--info-color);">📚 相关文档</div>';
                content += '<div style="display: flex; flex-direction: column; gap: 8px;">';
                data.related_docs.forEach(doc => {
                    content += `<a href="${doc.url}" target="_blank" style="display: block; padding: 8px 12px; background: var(--info-color); color: white; text-decoration: none; border-radius: 4px; font-size: 12px; transition: opacity 0.2s;" onmouseover="this.style.opacity='0.8'" onmouseout="this.style.opacity='1'">${doc.title} ↗</a>`;
                });
                content += '</div></div>';
            }

            // OpenGrok源码搜索结果
            if (data.opengrok_results && data.opengrok_results.length > 0) {
                content += '<div style="margin-top: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px; border-left: 3px solid #9c27b0;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: #9c27b0;">🔍 相关源码 (OpenGrok)</div>';
                content += '<div style="max-height: 300px; overflow-y: auto;">';

                data.opengrok_results.forEach(item => {
                    let opengrokUrl = '';
                    if (OPENGROK_CONFIG.isValid) {
                        opengrokUrl = `${OPENGROK_CONFIG._baseUrl}/xref/${item.file}#${item.line}`;
                    }

                    content += `
                        <div style="background: var(--light-bg); border: 1px solid var(--border-color); border-radius: 4px; padding: 8px; margin-bottom: 8px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                                <div style="font-family: monospace; font-size: 11px; color: #1976d2; font-weight: 600;">
                                    ${item.class_name}
                                </div>
                                ${opengrokUrl ? `<a href="${opengrokUrl}" target="_blank" style="font-size: 10px; color: #9c27b0; text-decoration: none; white-space: nowrap;">
                                    查看源码 ↗
                                </a>` : '<span style="font-size: 10px; color: #999;">无链接</span>'}
                            </div>
                            <div style="font-family: monospace; font-size: 10px; color: var(--text-secondary); margin-bottom: 4px;">
                                ${item.file}:${item.line}
                            </div>
                            <div style="font-family: monospace; font-size: 10px; color: #424242; background: white; padding: 4px; border-radius: 3px; overflow-x: auto;">
                                ${escapeHtml(item.context)}
                            </div>
                        </div>
                    `;
                });

                content += '</div></div>';
            }

            // OpenGrok源码搜索结果
            if (data.source_search_results && data.source_search_results.length > 0) {
                content += '<div style="margin-top: 16px; padding: 12px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 6px; border-left: 3px solid #9c27b0;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: white;">🔍 OpenGrok源码搜索</div>';
                content += '<div style="max-height: 400px; overflow-y: auto;">';

                data.source_search_results.forEach(item => {
                    // 优先使用 item.url，如果没有则根据配置生成
                    let itemUrl = item.url;
                    if (!itemUrl) {
                        itemUrl = buildOpenGrokUrl(item.path, item.line);
                    }

                    // 调试信息
                    if (!itemUrl && DEBUG) {
                        console.debug('[OpenGrok] No URL for item:', {
                            hasItemUrl: !!item.url,
                            configValid: OPENGROK_CONFIG.isValid,
                            path: item.path
                        });
                    }

                    // 使用 display_path（如果有），否则使用 path
                    const displayPath = item.display_path || item.path;
                    content += `
                        <div style="background: white; border-radius: 4px; padding: 10px; margin-bottom: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                <div style="font-family: monospace; font-size: 12px; color: #1976d2; font-weight: 600;">
                                    ${item.type}
                                </div>
                                ${itemUrl ? `<a href="${itemUrl}" target="_blank" style="font-size: 11px; color: #667eea; text-decoration: none; white-space: nowrap; font-weight: 600;">
                                    在 OpenGrok 中查看 →
                                </a>` : '<span style="font-size: 10px; color: #999;">无链接</span>'}
                            </div>
                            <div style="font-family: monospace; font-size: 11px; color: #616161; margin-bottom: 4px;">
                                📁 ${displayPath}
                            </div>
                            <div style="font-family: monospace; font-size: 10px; color: #424242; background: #f5f5f5; padding: 6px; border-radius: 3px; overflow-x: auto;">
                                行 ${item.line} ${item.project ? '· 项目: ' + item.project : ''}
                            </div>
                        </div>
                    `;
                });

                content += '</div></div>';
            }


            // AI标记
            if (data.ai_enabled === false) {
                content += '<div style="margin-top: 12px; padding: 8px; background: rgba(255, 193, 7, 0.1); border-radius: 4px; text-align: center;">';
                content += '<div style="font-size: 11px; color: var(--text-secondary);">💡 基于规则的分析（AI未配置或不可用）</div>';
                content += '</div>';
            }

            modal.querySelector('.modal-body').innerHTML = content;
        } else {
            // 处理业务逻辑错误（success: false）
            const errorDetail = result.error || result.detail || '未知错误';
            modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color); padding: 20px; text-align: center;">分析失败: ${errorDetail}</div>`;
        }

    } catch (error) {
        showModalError(modal, `请求失败: ${error.message}`);
    }
}

/**
 * 使用 AI 分析测试失败
 * @param {string} testName - 测试用例名称
 * @param {string} errorMessage - 错误消息
 * @param {string} module - 测试模块
 */

async function aiAnalyzeFailure(testName, errorMessage, module = '') {
    try {
        // 显示加载提示
        showToast('🤖 报错分析...', 'info');

        // 提取类名和堆栈信息
        const classNames = extractClassNames(testName, errorMessage);
        const stackTrace = errorMessage; // errorMessage 包含完整的错误信息

        const formData = createFormData(AnalysisMode.AI, {
            test_name: testName,
            error_message: errorMessage,
            stack_trace: stackTrace,
            module: module,
            class_names: JSON.stringify(classNames)
        });

        const response = await fetch('/api/reports/analyze', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        if (result.success) {
            displayAIAnalysis(result.data, testName, errorMessage);
        } else {
            showToast('AI分析失败: ' + (result.error || result.detail || '未知错误'), 'error');
        }
    } catch (error) {
        console.error('AI分析错误:', error);
        showToast('AI分析请求失败', 'error');
    }
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
    modal.style.cssText = `
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        width: 100% !important;
        height: 100% !important;
        background: rgba(0, 0, 0, 0.7) !important;
        display: flex !important;
        justify-content: center !important;
        align-items: center !important;
        z-index: 10000 !important;
    `;

    let html = `
        <div style="background: var(--bg-color); border-radius: 12px; padding: 24px; max-width: 900px; max-height: 85vh; overflow-y: auto; width: 90%; box-shadow: 0 10px 40px rgba(0,0,0,0.3); margin: auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                <h2 style="margin: 0; font-size: 18px; font-weight: 600;">🤖 报错分析</h2>
                <div style="display: flex; align-items: center; gap: 10px;">
                    ${data.source_code_fetched ? '<span style="font-size: 10px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 3px 10px; border-radius: 4px;">✓ 源码已获取</span>' : ''}
                    ${data.ai_enabled === false ? '<span style="font-size: 10px; background: var(--warning-color); color: white; padding: 2px 8px; border-radius: 4px;">规则分析</span>' : '<span style="font-size: 10px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 2px 8px; border-radius: 4px;">AI增强</span>'}
                    ${data.ai_model ? `<span style="font-size: 10px; background: var(--success-color); color: white; padding: 2px 8px; border-radius: 4px;">${data.ai_model}</span>` : ''}
                    <button onclick="closeAIAnalysisModal('${modalId}')" style="background: none; border: none; font-size: 24px; cursor: pointer; color: var(--text-secondary);">×</button>
                </div>
            </div>
    `;

    // 源码信息
    if (data.source_code_fetched && data.source_url) {
        html += `
            <div style="background: linear-gradient(135deg, rgba(102, 126, 234, 0.1) 0%, rgba(118, 75, 162, 0.1) 100%); border-left: 4px solid #667eea; border-radius: 8px; padding: 14px; margin-bottom: 16px;">
                <div style="font-size: 13px; font-weight: 600; margin-bottom: 6px; color: #667eea;">💻 源码信息</div>
                <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 6px;">文件路径: ${data.source_file_path || 'N/A'}</div>
                <a href="${data.source_url}" target="_blank" style="font-size: 11px; color: #667eea; text-decoration: none; display: inline-flex; align-items: center; gap: 4px;">
                    🔗 查看源码
                    <svg style="width: 12px; height: 12px;" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path>
                    </svg>
                </a>
            </div>
        `;
    }


    // 根本原因
    if (data.root_cause) {
        html += `
            <div style="background: linear-gradient(135deg, rgba(245, 87, 108, 0.1) 0%, rgba(250, 177, 160, 0.1) 100%); border-left: 4px solid #f5576c; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 8px; color: #f5576c;">🎯 根本原因</div>
                <div style="font-size: 13px; color: var(--text-color); line-height: 1.6;">${data.root_cause}</div>
            </div>
        `;
    }

    // 详细分析
    if (data.analysis) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">📊 详细分析</div>
                <div style="font-size: 12px; line-height: 1.8; white-space: pre-wrap; word-break: break-word;">${data.analysis}</div>
            </div>
        `;
    }

    // 解决建议
    if (data.suggestions && data.suggestions.length > 0) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">💡 解决建议</div>
                <div style="display: flex; flex-direction: column; gap: 10px;">
                    ${data.suggestions.map((suggestion, idx) => `
                        <div style="display: flex; gap: 10px; align-items: flex-start;">
                            <span style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; flex-shrink: 0;">${idx + 1}</span>
                            <span style="font-size: 12px; line-height: 1.6; color: var(--text-color);">${suggestion}</span>
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    // 相关文档
    if (data.related_docs && data.related_docs.length > 0) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">📚 相关文档</div>
                <div style="display: flex; flex-direction: column; gap: 8px;">
                    ${data.related_docs.map(doc => `
                        <a href="${doc.url}" target="_blank" style="display: flex; align-items: center; gap: 10px; padding: 10px; background: var(--darker-bg); border-radius: 6px; text-decoration: none; color: var(--text-color); transition: all 0.2s;">
                            <span style="font-size: 16px;">📖</span>
                            <span style="font-size: 12px; flex: 1;">${doc.title}</span>
                            <span style="font-size: 10px; color: var(--primary-color);">查看 →</span>
                        </a>
                    `).join('')}
                </div>
            </div>
        `;
    }


    html += `
            <div style="display: flex; gap: 10px; margin-top: 20px;">
                <button onclick="closeAIAnalysisModal('${modalId}')" class="btn-xs">关闭</button>
                <button onclick="copyAIAnalysis('${modalId}')" class="btn-xs" style="background: var(--success-color);">📋 复制分析报告</button>
            </div>
        </div>
    `;

    modal.innerHTML = html;
    document.body.appendChild(modal);

    // 注册到 ModalManager
    ModalManager.registerDynamic(modal);

    // 点击外部关闭
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

    // 提取文本内容
    const textElements = modal.querySelectorAll('div[style*="font-size"]');
    let text = 'CTS测试失败AI分析报告\n';
    text += '=' .repeat(40) + '\n\n';

    textElements.forEach(el => {
        const content = el.textContent.trim();
        if (content && !content.startsWith('复制') && !content.startsWith('关闭')) {
            text += content + '\n\n';
        }
    });

    // 复制到剪贴板
    navigator.clipboard.writeText(text).then(() => {
        showToast('✓ 分析报告已复制', 'success');
    }).catch(() => {
        showToast('复制失败', 'error');
    });
}
