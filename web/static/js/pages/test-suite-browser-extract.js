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
            extractBtn.textContent = '📦 解压中...';
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
            extractBtn.textContent = '📦 解压套件';
        }
        if (downloadBtn) downloadBtn.disabled = false;
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = '开始解压';
        }
    }
};

