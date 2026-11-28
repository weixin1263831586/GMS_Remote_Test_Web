// 暴露到全局作用域
window.downloadTestSuite = async function downloadTestSuite() {
    const urlInput = $('suite-download-url');
    const downloadBtn = $('btn-download-suite');
    const extractBtn = $('btn-extract-suite');
    const progressDiv = $('suite-download-progress');
    const progressBar = $('suite-progress-bar');
    const progressPercent = $('suite-progress-percent');
    const progressStatus = $('suite-progress-status');
    const logDiv = $('suite-download-log');

    debugLog('[downloadTestSuite] urlInput:', urlInput);
    debugLog('[downloadTestSuite] downloadBtn:', downloadBtn);

    if (!urlInput || !urlInput.value) {
        showToast('请输入下载地址', 'error');
        return;
    }

    const url = urlInput.value.trim();

    debugLog('[downloadTestSuite] URL:', url);

    if (downloadBtn) {
        downloadBtn.disabled = true;
        downloadBtn.textContent = '下载中';
    }
    if (extractBtn) extractBtn.disabled = true;
    if (progressDiv) progressDiv.style.display = 'block';
    if (logDiv) {
        logDiv.style.display = 'block';
        logDiv.innerHTML = '';
    }

    let pollingStarted = false;

    const log = (msg) => {
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            // msg 可能携带服务端返回的 error/archive_path 等远端数据,
            // 必须转义后插入(与页面其余 escapeHtml 用法一致)。
            logDiv.innerHTML += `[${time}] ${escapeHtml(msg)}\n`;
            logDiv.scrollTop = logDiv.scrollHeight;
        }
        debugLog('[downloadTestSuite] ' + msg);
    };

    debugLog('[downloadTestSuite] 开始下载：', url);

    try {
        const suiteWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        if (!isLocalWorkspaceWorker(suiteWorkerId)) {
            const accepted = await apiCall('/api/cluster/suites/download', 'POST', {
                worker_id: suiteWorkerId, url
            });
            if (progressStatus) progressStatus.textContent = `正在由 ${suiteWorkerId} 下载...`;
            if (progressBar) progressBar.style.width = '10%';
            if (progressPercent) progressPercent.textContent = '10%';
            let command;
            while (true) {
                await new Promise(resolve => setTimeout(resolve, 1500));
                const status = await apiCall(`/api/cluster/commands/${encodeURIComponent(accepted.command_id)}`);
                command = status.command;
                if (['completed', 'failed', 'cancelled'].includes(command.status)) break;
            }
            if (command.status !== 'completed') throw new Error(command.error || 'Worker 下载失败');
            const downloaded = command.result || {};
            urlInput.dataset.lastArchivePath = downloaded.archive_path || '';
            if (progressBar) progressBar.style.width = '100%';
            if (progressPercent) progressPercent.textContent = '100%';
            if (progressStatus) progressStatus.textContent = '✅ 下载完成';
            log(`✅ ${suiteWorkerId} 下载完成：${downloaded.archive_path}`);
            log(`📦 文件大小：${((downloaded.file_size || 0) / 1024 / 1024).toFixed(2)} MB`);
            notifyOperationResult('测试套件下载完成', downloaded.message || '下载完成',
                'success', 'suite-download', {worker_id: suiteWorkerId, archive_path: downloaded.archive_path});
            return;
        }
        const result = await apiCall('/api/test/suites/download-url', 'POST', {
            url: url,
            save_dir: getDefaultSuitesPath()
        });
        debugLog('[downloadTestSuite] 响应结果:', result);

        if (result.success && result.task_id) {
            pollingStarted = true;
            sessionStorage.setItem('active_suite_download', JSON.stringify({
                task_id: result.task_id,
                archive_path: result.archive_path || ''
            }));
            await pollDownloadProgress(result.task_id);
        } else if (result.success) {
            log(`✅ 下载完成：${result.archive_path}`);
            log(`📦 文件大小：${(result.file_size / 1024 / 1024).toFixed(2)} MB`);

            if (progressBar) progressBar.style.width = '100%';
            if (progressPercent) progressPercent.textContent = '100%';
            if (progressStatus) progressStatus.textContent = '✅ 下载完成';

            notifyOperationResult(
                '测试套件下载完成',
                result.message || '下载完成',
                'success',
                'suite-download',
                { archive_path: result.archive_path }
            );

            await refreshTestSuiteBrowser();
        } else {
            log(`❌ 下载失败：${result.error}`);
            if (progressStatus) progressStatus.textContent = '❌ 下载失败';
            notifyOperationResult('测试套件下载失败', result.error, 'error', 'suite-download');
        }
    } catch (error) {
        console.error('[downloadTestSuite] 异常:', error);
        log(`❌ 错误：${error.message}`);
        if (progressStatus) progressStatus.textContent = '❌ 错误';
        notifyOperationResult('测试套件下载失败', error.message, 'error', 'suite-download');
    } finally {
        if (!pollingStarted) {
            if (downloadBtn) {
                downloadBtn.disabled = false;
                downloadBtn.textContent = '下载套件';
            }
            if (extractBtn) extractBtn.disabled = false;
        }
    }
};

async function pollTaskProgress({ statusUrl, progressBar, progressPercent, progressStatus, completedLabel, activeLabel }) {
    let lastPercent = -1;
    let lastStatus = '';
    while (true) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const resp = await fetch(statusUrl);
        const result = await resp.json();
        if (!result.success) {
            throw new Error(result.error || '任务状态查询失败');
        }
        const task = result.task;
        const percent = Math.max(0, Math.min(100, Number(task.progress || 0)));
        if (progressBar && percent !== lastPercent) progressBar.style.width = `${percent}%`;
        if (progressPercent && percent !== lastPercent) progressPercent.textContent = `${percent.toFixed(1)}%`;
        const statusText = task.status === 'completed' ? completedLabel : activeLabel;
        if (progressStatus && statusText !== lastStatus) {
            progressStatus.textContent = statusText;
            lastStatus = statusText;
        }
        lastPercent = percent;
        if (task.status === 'completed') return task;
        if (task.status === 'error') throw new Error(task.error || '任务失败');
    }
}

async function pollDownloadProgress(taskId) {
    const progressDiv = $('suite-download-progress');
    const progressBar = $('suite-progress-bar');
    const progressPercent = $('suite-progress-percent');
    const progressStatus = $('suite-progress-status');
    const logDiv = $('suite-download-log');
    const downloadBtn = $('btn-download-suite');
    const extractBtn = $('btn-extract-suite');
    const urlInput = $('suite-download-url');

    if (downloadBtn) { downloadBtn.disabled = true; downloadBtn.textContent = '下载中'; }
    if (extractBtn) extractBtn.disabled = true;
    if (progressDiv) progressDiv.style.display = 'block';

    try {
        const statusUrl = `/api/test/suites/download-status/${encodeURIComponent(taskId)}`;
        const completedTask = await pollTaskProgress({
            statusUrl,
            progressBar, progressPercent, progressStatus,
            completedLabel: '✅ 下载完成',
            activeLabel: '下载中...'
        });

        const sizeMb = ((completedTask.downloaded_size || 0) / 1024 / 1024).toFixed(2);
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] ✅ 下载完成：${escapeHtml(completedTask.archive_path)}\n`;
            logDiv.innerHTML += `[${time}] 📦 文件大小：${sizeMb} MB\n`;
        }
        notifyOperationResult(
            '测试套件下载完成',
            completedTask.message || '下载完成',
            'success',
            'suite-download',
            { task_id: taskId, archive_path: completedTask.archive_path }
        );
        if (urlInput) urlInput.dataset.lastArchivePath = completedTask.archive_path || '';
        await refreshTestSuiteBrowser();
    } catch (error) {
        notifyOperationResult(
            '测试套件下载失败',
            error.message,
            'error',
            'suite-download',
            { task_id: taskId }
        );
        if (progressStatus) progressStatus.textContent = `❌ ${error.message}`;
    } finally {
        sessionStorage.removeItem('active_suite_download');
        if (downloadBtn) { downloadBtn.disabled = false; downloadBtn.textContent = '下载套件'; }
        if (extractBtn) extractBtn.disabled = false;
    }
}

async function resumeSuiteDownloadIfNeeded() {
    const saved = sessionStorage.getItem('active_suite_download');
    if (!saved) return;
    try {
        const { task_id } = JSON.parse(saved);
        if (!task_id) return;
        const resp = await fetch(`/api/test/suites/download-status/${encodeURIComponent(task_id)}`);
        const result = await resp.json();
        if (!result.success || !result.task) {
            sessionStorage.removeItem('active_suite_download');
            return;
        }
        const task = result.task;
        if (task.status === 'completed' || task.status === 'error') {
            sessionStorage.removeItem('active_suite_download');
            return;
        }
        // Active download found — resume polling
        await pollDownloadProgress(task_id);
    } catch (e) {
        sessionStorage.removeItem('active_suite_download');
    }
}

// 显示添加本地测试套件路径弹框
window.showAddLocalSuiteDialog = function showAddLocalSuiteDialog() {
    const modal = $('add-local-suite-modal');
    if (modal) {
        ModalManager.open('add-local-suite-modal');
        const input = $('local-suite-path-input');
        if (input) {
            input.value = '';
            input.focus();
        }
    }
};

// 关闭弹框
window.closeAddLocalSuiteModal = function closeAddLocalSuiteModal() {
    ModalManager.close('add-local-suite-modal');
};

// 浏览服务器目录，选择本地测试套件目录后回填到输入框
window.browseLocalSuitePath = async function browseLocalSuitePath() {
    state.fileBrowser.mode = 'local-suite';
    state.fileBrowser.targetInputId = 'local-suite-path-input';
    state.fileBrowser.selectedFile = null;
    document.getElementById('file-browser-title').textContent = '选择测试套件目录';
    ModalManager.open('file-browser-modal');

    await loadFileDirectory(getDefaultSuitesPath());
};

// 处理 Esc 键关闭弹框
window.handleAddLocalSuiteKeydown = function handleAddLocalSuiteKeydown(event) {
    if (event.key === 'Escape') {
        closeAddLocalSuiteModal();
    }
    // 回车键提交
    if (event.key === 'Enter') {
        submitAddLocalSuite();
    }
};

// 提交添加本地测试套件
window.submitAddLocalSuite = async function submitAddLocalSuite() {
    const pathInput = $('local-suite-path-input');
    if (!pathInput || !pathInput.value) {
        showToast('请输入本地路径', 'error');
        return;
    }

    const localPath = pathInput.value.trim();
    debugLog('[submitAddLocalSuite] 本地路径:', localPath);

    try {
        const result = await apiCall('/api/test/suites/add-local', 'POST', { path: localPath });
        debugLog('[submitAddLocalSuite] 响应结果:', result);

        if (result.success) {
            showToast(`添加成功：${result.message}`, 'success');
            closeAddLocalSuiteModal();
            await refreshTestSuiteBrowser();
        } else {
            showToast(`添加失败：${result.error}`, 'error');
        }
    } catch (error) {
        console.error('[submitAddLocalSuite] 异常:', error);
        showToast(`添加失败：${error.message}`, 'error');
    }
};

function deriveSuiteFolderNameFromArchivePath(archivePath) {
    const filename = (archivePath || '').split('/').pop() || '';
    const extensions = ['.tar.bz2', '.tar.gz', '.tgz', '.zip', '.tar'];
    for (const ext of extensions) {
        if (filename.endsWith(ext)) return filename.slice(0, -ext.length);
    }
    return filename.replace(/\.[^.]+$/, '') || 'test-suite';
}

// Archive selection and extraction share the download workbench and progress polling.
window.extractTestSuite = async function extractTestSuite() {
    await showExtractSuiteModal();
};

window.showExtractSuiteModal = async function showExtractSuiteModal() {
    const urlInput = $('suite-download-url');
    const modal = $('extract-suite-modal');
    const select = $('extract-suite-archive-select');
    const pathInput = $('extract-suite-archive-path');
    const folderInput = $('extract-suite-folder-name');
    if (!modal || !select || !pathInput || !folderInput) return;

    ModalManager.open('extract-suite-modal');
    select.innerHTML = '<option value="">正在加载压缩包...</option>';

    try {
        const suiteWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        const result = await apiCall(
            isLocalWorkspaceWorker(suiteWorkerId)
                ? '/api/test/suites/archives'
                : `/api/cluster/suites/archives?worker_id=${encodeURIComponent(suiteWorkerId)}`,
            'GET'
        );
        const archives = result.success ? (result.archives || []) : [];
        select.innerHTML = '<option value="">手动输入压缩包路径</option>' + archives.map(archive => {
            const sizeMb = ((archive.size || 0) / 1024 / 1024).toFixed(1);
            return `<option value="${escapeHtml(archive.path)}" data-folder="${escapeHtml(archive.default_dir_name || '')}">${escapeHtml(archive.name)} (${sizeMb} MB)</option>`;
        }).join('');

        const lastArchivePath = urlInput?.dataset?.lastArchivePath || '';
        const defaultPath = lastArchivePath || (archives[0]?.path || '');
        if (defaultPath) {
            pathInput.value = defaultPath;
            const option = Array.from(select.options).find(opt => opt.value === defaultPath);
            if (option) select.value = defaultPath;
        } else if (urlInput && urlInput.value) {
            pathInput.value = `${getDefaultSuitesPath()}/${urlInput.value.split('/').pop()}`;
        } else {
            pathInput.value = '';
        }
        folderInput.value = deriveSuiteFolderNameFromArchivePath(pathInput.value);
        folderInput.focus();
        folderInput.select();
    } catch (error) {
        select.innerHTML = '<option value="">手动输入压缩包路径</option>';
        showToast(`加载压缩包列表失败：${error.message}`, 'warning');
    }
};

window.closeExtractSuiteModal = function closeExtractSuiteModal() {
    ModalManager.close('extract-suite-modal');
};

window.handleExtractSuiteKeydown = function handleExtractSuiteKeydown(event) {
    if (event.key === 'Escape') closeExtractSuiteModal();
    if (event.key === 'Enter') submitExtractSuite();
};

window.handleExtractArchiveSelectChange = function handleExtractArchiveSelectChange() {
    const select = $('extract-suite-archive-select');
    const pathInput = $('extract-suite-archive-path');
    const folderInput = $('extract-suite-folder-name');
    if (!select || !pathInput || !folderInput || !select.value) return;
    pathInput.value = select.value;
    folderInput.value = select.selectedOptions[0]?.dataset?.folder || deriveSuiteFolderNameFromArchivePath(select.value);
};

window.submitExtractSuite = async function submitExtractSuite() {
    const archiveInput = $('extract-suite-archive-path');
    const folderInput = $('extract-suite-folder-name');
    const downloadBtn = $('btn-download-suite');
    const extractBtn = $('btn-extract-suite');
    const submitBtn = $('btn-submit-extract-suite');
    const logDiv = $('suite-download-log');
    const progressDiv = $('suite-download-progress');
    const progressBar = $('suite-progress-bar');
    const progressPercent = $('suite-progress-percent');
    const progressStatus = $('suite-progress-status');

    try {
        const archivePath = (archiveInput?.value || '').trim();
        const folderName = (folderInput?.value || '').trim();

        if (!archivePath) {
            showToast('请选择或输入压缩包路径', 'error');
            return;
        }
        if (!folderName) {
            showToast('请输入解压后的文件夹名称', 'error');
            return;
        }

        if (extractBtn) {
            extractBtn.disabled = true;
            extractBtn.textContent = '解压中...';
        }
        if (downloadBtn) downloadBtn.disabled = true;
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = '解压中...';
        }

        closeExtractSuiteModal();
        if (progressDiv) progressDiv.style.display = 'block';
        if (progressBar) progressBar.style.width = '0%';
        if (progressPercent) progressPercent.textContent = '0%';
        if (progressStatus) progressStatus.textContent = '正在解压...';

        if (logDiv) {
            logDiv.style.display = 'block';
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] 开始解压：${escapeHtml(archivePath)}\n`;
        }

        const suiteWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        const result2 = await apiCall(
            isLocalWorkspaceWorker(suiteWorkerId)
                ? '/api/test/suites/extract-start'
                : '/api/cluster/suites/extract',
            'POST',
            isLocalWorkspaceWorker(suiteWorkerId) ? {
                archive_path: archivePath,
                extract_dir: getDefaultSuitesPath(),
                target_dir_name: folderName
            } : {
                worker_id: suiteWorkerId,
                archive_path: archivePath,
                target_dir_name: folderName
            }
        );

        if (result2.success && (result2.task_id || result2.command_id)) {
            let completedTask;
            if (result2.command_id) {
                while (true) {
                    await new Promise(resolve => setTimeout(resolve, 1000));
                    const state = await apiCall(`/api/cluster/commands/${encodeURIComponent(result2.command_id)}`);
                    if (['completed', 'failed', 'cancelled'].includes(state.command.status)) {
                        if (state.command.status !== 'completed') throw new Error(state.command.error || 'Worker 解压失败');
                        completedTask = state.command.result || {};
                        break;
                    }
                }
                if (progressBar) progressBar.style.width = '100%';
                if (progressPercent) progressPercent.textContent = '100%';
                if (progressStatus) progressStatus.textContent = '✅ 解压完成';
            } else {
                const statusUrl = `/api/test/suites/extract-status/${encodeURIComponent(result2.task_id)}`;
                completedTask = await pollTaskProgress({statusUrl, progressBar, progressPercent, progressStatus,
                    completedLabel: '✅ 解压完成', activeLabel: '正在解压...'});
            }
            if (logDiv) {
                const time = new Date().toLocaleTimeString();
                logDiv.innerHTML += `[${time}] ✅ 解压完成：${escapeHtml(completedTask.extracted_path)}\n`;
            }
            notifyOperationResult(
                '测试套件解压完成',
                completedTask.message || '解压完成',
                'success',
                'suite-extract',
                { task_id: result2.task_id || result2.command_id, extracted_path: completedTask.extracted_path }
            );

            debugLog('[submitExtractSuite] refreshing suite browser, extracted_path:', completedTask.extracted_path);
            await refreshTestSuiteBrowser(completedTask.extracted_path || '');
        } else {
            if (logDiv) {
                const time = new Date().toLocaleTimeString();
                logDiv.innerHTML += `[${time}] ❌ 解压失败：${escapeHtml(result2.error)}\n`;
            }
            notifyOperationResult(
                '测试套件解压失败',
                result2.error,
                'error',
                'suite-extract',
                { archive_path: archivePath }
            );
        }
    } catch (error) {
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] ❌ 错误：${escapeHtml(error.message)}\n`;
        }
        notifyOperationResult(
            '测试套件解压失败',
            error.message,
            'error',
            'suite-extract',
            { archive_path: archivePath }
        );
    } finally {
        if (extractBtn) {
            extractBtn.disabled = false;
            extractBtn.textContent = '解压套件';
        }
        if (downloadBtn) downloadBtn.disabled = false;
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = '开始解压';
        }
    }
};

