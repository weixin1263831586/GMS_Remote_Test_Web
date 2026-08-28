// ==================== APK Analysis ====================

window.apkCurrentTaskId = null;
window.apkPollInterval = null;
window.apkStatusPollInFlight = false;
window.apkNotifiedTaskId = null;
window.apkPendingOpenTarget = null;
window.apkOpenFiles = new Map();
window.apkActiveFilePath = null;
window.apkTaskHistory = [];
window.apkTaskHistoryPromise = null;
const APK_ANALYSIS_TAB_STORAGE_KEY = 'gms_apk_analysis_tab';
const APK_ANALYSIS_TABS = new Set(['manifest', 'permissions', 'source']);

function storedApkAnalysisTab() {
    try {
        const saved = window.sessionStorage.getItem(APK_ANALYSIS_TAB_STORAGE_KEY) || '';
        return APK_ANALYSIS_TABS.has(saved) ? saved : 'manifest';
    } catch (_error) {
        return 'manifest';
    }
}

function stopApkPolling() {
    clearInterval(window.apkPollInterval);
    window.apkPollInterval = null;
    window.apkStatusPollInFlight = false;
}

function setApkUploadEmpty(empty) {
    const uploadZone = $('apk-upload-zone');
    if (uploadZone) {
        uploadZone.classList.toggle('upload-empty', empty);
    }
}

function initApkAnalysisPage() {
    const uploadZone = $('apk-upload-zone');
    const fileInput = $('apk-file-input');

    if (!uploadZone || !fileInput) return;

    setApkUploadEmpty(!window.apkCurrentTaskId);
    initApkSourceResizer();
    switchApkTab(storedApkAnalysisTab(), {persist: false});
    void loadApkTaskHistory(false);

    if (uploadZone.dataset.initialized === 'true') return;
    uploadZone.dataset.initialized = 'true';

    // 绑定拖拽事件
    uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadZone.classList.add('drag-over');
    });
    uploadZone.addEventListener('dragleave', () => {
        uploadZone.classList.remove('drag-over');
    });
    uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');
        if (e.dataTransfer.files.length > 0) {
            handleApkFile(e.dataTransfer.files[0]);
        }
    });

    // 绑定文件选择事件
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleApkFile(e.target.files[0]);
        }
    });
}

const APK_TASK_STATUS_LABELS = {
    uploaded: '待分析',
    analyzing: '分析中',
    completed: '已完成',
    error: '失败'
};

async function loadApkTaskHistory(force = false) {
    const select = $('apk-task-history');
    const refresh = $('apk-task-history-refresh');
    if (!select) return [];
    if (window.apkTaskHistoryPromise) return window.apkTaskHistoryPromise;

    const previous = window.apkCurrentTaskId || select.value;
    select.setAttribute('aria-busy', 'true');
    if (refresh) {
        refresh.disabled = true;
        refresh.textContent = '刷新中…';
    }
    window.apkTaskHistoryPromise = (async () => {
        try {
            const result = await apiCall('/api/apk/tasks');
            if (!result.success) throw new Error(result.error || '任务列表读取失败');
            const tasks = Array.isArray(result.data?.tasks) ? result.data.tasks : [];
            window.apkTaskHistory = tasks;
            select.replaceChildren();
            select.add(new Option(tasks.length ? '选择最近任务…' : '暂无历史任务', ''));
            tasks.forEach(task => {
                const state = APK_TASK_STATUS_LABELS[task.status] || task.status || '未知';
                select.add(new Option(`${task.filename || task.task_id} · ${state}`, task.task_id));
            });
            if (previous && tasks.some(task => task.task_id === previous)) {
                select.value = previous;
            }

            // A running decompilation is authoritative and should survive a
            // browser refresh. Completed/error tasks stay opt-in via selector.
            const active = tasks.find(task => task.status === 'analyzing');
            if (!window.apkCurrentTaskId && active) {
                await restoreApkTask(active.task_id, {fromHistoryLoad: true});
            }
            return tasks;
        } catch (error) {
            select.replaceChildren(new Option('任务列表读取失败', ''));
            if (force) showToast(`APK任务刷新失败: ${error.message}`, 'error');
            return [];
        } finally {
            select.setAttribute('aria-busy', 'false');
            if (refresh) {
                refresh.disabled = false;
                refresh.textContent = '↻ 刷新';
            }
            window.apkTaskHistoryPromise = null;
        }
    })();
    return window.apkTaskHistoryPromise;
}

function resetApkTaskPanels(tabName = 'manifest') {
    const sourceTree = $('apk-source-tree');
    if (sourceTree) {
        sourceTree.dataset.loaded = '';
        sourceTree.innerHTML = '';
    }
    const permList = $('apk-permissions-list');
    if (permList) {
        permList.dataset.loaded = '';
        permList.innerHTML = '';
    }
    const manifestInfo = $('apk-manifest-info');
    if (manifestInfo) manifestInfo.innerHTML = '';
    const rawXml = $('apk-raw-xml');
    if (rawXml) rawXml.textContent = '';
    closeApkFileViewer();
    switchApkTab(tabName);
}

async function restoreApkTask(taskId, options = {}) {
    if (!taskId || taskId === window.apkCurrentTaskId) return;
    const task = window.apkTaskHistory.find(item => item.task_id === taskId) || {};
    stopApkPolling();
    window.apkCurrentTaskId = taskId;
    window.apkNotifiedTaskId = task.status === 'completed' ? taskId : null;
    setApkUploadEmpty(false);
    resetApkTaskPanels(options.fromHistoryLoad ? storedApkAnalysisTab() : 'manifest');

    if ($('apk-analysis-status')) $('apk-analysis-status').style.display = 'block';
    if ($('apk-analysis-result')) $('apk-analysis-result').style.display = 'none';
    if ($('apk-file-name')) $('apk-file-name').textContent = task.filename || taskId;
    if ($('apk-analysis-state')) $('apk-analysis-state').textContent = '正在恢复任务状态…';
    if ($('apk-analysis-progress-container')) $('apk-analysis-progress-container').style.display = 'block';
    if ($('apk-analysis-progress-bar')) $('apk-analysis-progress-bar').style.width = `${task.progress || 0}%`;
    const select = $('apk-task-history');
    if (select) select.value = taskId;

    const status = await pollApkStatus();
    if (status?.status === 'analyzing' && !window.apkPollInterval) {
        window.apkPollInterval = setInterval(pollApkStatus, STATUS_POLL_INTERVAL);
    }
    if (!options.fromHistoryLoad) {
        showToast(`已恢复 APK 任务: ${status?.filename || task.filename || taskId}`, 'info');
    }
}

function initApkSourceResizer() {
    const layout = $('apk-tab-source')?.querySelector('.apk-source-layout');
    const resizer = $('apk-source-resizer');
    if (!layout || !resizer || resizer.dataset.initialized === 'true') return;

    resizer.dataset.initialized = 'true';
    const savedWidth = Number(localStorage.getItem('apk_source_tree_width') || 0);
    if (savedWidth) {
        layout.style.setProperty('--apk-source-tree-width', `${Math.min(620, Math.max(180, savedWidth))}px`);
    }

    let dragging = false;
    const stopDrag = () => {
        if (!dragging) return;
        dragging = false;
        document.body.classList.remove('apk-resizing');
    };

    resizer.addEventListener('mousedown', (event) => {
        if (window.matchMedia('(max-width: 980px)').matches) return;
        event.preventDefault();
        dragging = true;
        document.body.classList.add('apk-resizing');
    });

    document.addEventListener('mousemove', (event) => {
        if (!dragging) return;
        const rect = layout.getBoundingClientRect();
        const width = Math.min(620, Math.max(180, event.clientX - rect.left));
        layout.style.setProperty('--apk-source-tree-width', `${width}px`);
        localStorage.setItem('apk_source_tree_width', String(Math.round(width)));
    });
    document.addEventListener('mouseup', stopDrag);
    document.addEventListener('mouseleave', stopDrag);
}

// APK/JAR 文件扩展名常量
const SUPPORTED_APK_EXTENSIONS = ['.apk', '.jar'];

function isSupportedApkFile(filename) {
    const nameLower = filename.toLowerCase();
    return SUPPORTED_APK_EXTENSIONS.some(ext => nameLower.endsWith(ext));
}

async function handleApkFile(file) {
    if (!isSupportedApkFile(file.name)) {
        showToast('仅支持 .apk 和 .jar 文件', 'error');
        return;
    }

    const fileSizeMB = (file.size / (1024 * 1024)).toFixed(1);
    showToast(`正在上传 ${file.name} (${fileSizeMB}MB)...`, 'info');

    const uploadProgress = $('apk-upload-progress');
    const uploadProgressFill = $('apk-progress-fill');
    if (uploadProgress) uploadProgress.style.display = 'block';
    if (uploadProgressFill) uploadProgressFill.style.width = '0%';

    try {
        const data = await window.uploadFileWithProgress(file, '/api/apk/upload', {
            useChunkUpload: true,
            chunkSize: 32 * 1024 * 1024,
            onProgress: (percent) => {
                if (uploadProgressFill) {
                    uploadProgressFill.style.width = `${Math.min(100, Math.max(1, percent))}%`;
                }
            }
        });
        if (uploadProgressFill) uploadProgressFill.style.width = '100%';

        if (data.success && data.data) {
            stopApkPolling();
            window.apkCurrentTaskId = data.data.task_id;
            window.apkNotifiedTaskId = null;
            showToast(`上传成功: ${file.name}`, 'success');
            setApkUploadEmpty(false);

            $('apk-analysis-status').style.display = 'block';
            $('apk-file-name').textContent = `${file.name} (${fileSizeMB}MB)`;
            $('apk-analysis-state').textContent = '已上传，正在启动反编译';
            $('apk-btn-download').style.display = 'none';
            $('apk-analysis-result').style.display = 'none';
            $('apk-analysis-progress-container').style.display = 'none';

            resetApkTaskPanels();
            void loadApkTaskHistory(true);
            await startApkAnalysis();
        } else {
            showToast(`上传失败: ${data.error}`, 'error');
        }
    } catch (e) {
        showToast(`上传失败: ${e.message}`, 'error');
    } finally {
        setTimeout(() => {
            if (uploadProgress) uploadProgress.style.display = 'none';
            if (uploadProgressFill) uploadProgressFill.style.width = '0%';
        }, 500);
    }
}

async function startApkAnalysis() {
    if (!window.apkCurrentTaskId) {
        showToast('请先上传 APK 文件', 'error');
        return;
    }

    if ('Notification' in window && Notification.permission === 'default' && !state.browserNotificationsEnabled) {
        void requestBrowserNotificationPermission();
    }

    const btn = $('apk-btn-analyze');
    if (btn) {
        btn.style.display = 'inline-flex';
        btn.disabled = true;
        btn.textContent = '⏳ 分析中...';
    }
    $('apk-analysis-state').textContent = '正在反编译 APK...';
    $('apk-analysis-progress-container').style.display = 'block';
    $('apk-analysis-progress-bar').style.width = '5%';

    try {
        const data = await apiCall(`/api/apk/analyze/${window.apkCurrentTaskId}`, 'POST');

        if (data.success) {
            window.apkPollInterval = setInterval(pollApkStatus, STATUS_POLL_INTERVAL);
            void loadApkTaskHistory(true);
            await pollApkStatus();
        } else {
            showToast(`分析失败: ${data.error}`, 'error');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '🔬 开始分析';
            }
        }
    } catch (e) {
        showToast(`分析失败: ${e.message}`, 'error');
        if (btn) {
            btn.disabled = false;
            btn.textContent = '🔬 开始分析';
        }
    }
}

async function pollApkStatus() {
    if (!window.apkCurrentTaskId) return;
    if (window.apkStatusPollInFlight) return;
    window.apkStatusPollInFlight = true;

    try {
        const data = await apiCall(`/api/apk/status/${window.apkCurrentTaskId}`);

        if (!data.success) {
            stopApkPolling();
            $('apk-analysis-state').textContent = `状态查询失败: ${data.error || data.message || '未知错误'}`;
            const btn = $('apk-btn-analyze');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '🔬 重新分析';
            }
            return;
        }

        const status = data.data;
        if (!status || typeof status !== 'object') {
            stopApkPolling();
            $('apk-analysis-state').textContent = '状态查询失败: 响应数据为空';
            const btn = $('apk-btn-analyze');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '🔬 重新分析';
            }
            return;
        }
        $('apk-analysis-progress-bar').style.width = status.progress + '%';
        $('apk-analysis-state').textContent =
            status.status === 'analyzing' ? `正在反编译... (${status.progress}%)` :
            status.status === 'completed' ? '反编译完成' :
            status.status === 'error' ? `错误: ${status.error}` : status.status;
        const analyzeBtn = $('apk-btn-analyze');
        if (analyzeBtn) {
            analyzeBtn.style.display = ['uploaded', 'error', 'analyzing'].includes(status.status) ? 'inline-flex' : 'none';
            analyzeBtn.disabled = status.status === 'analyzing';
            analyzeBtn.textContent = status.status === 'analyzing'
                ? '⏳ 分析中...'
                : status.status === 'error' ? '🔬 重新分析' : '🔬 开始分析';
        }

        if (status.status === 'completed') {
            stopApkPolling();

            $('apk-btn-download').style.display = 'inline-block';
            $('apk-analysis-state').textContent = '反编译完成 - 可查看结果';
            $('apk-analysis-result').style.display = 'block';
            void loadApkTaskHistory(true);

            loadApkManifest();
            if (window.apkPendingOpenTarget?.filePath) {
                const target = window.apkPendingOpenTarget;
                window.apkPendingOpenTarget = null;
                setTimeout(() => {
                    openApkPendingSourceTarget(target)
                        .then(file => enhanceReportDiagnosisWithSource(file?.path || target.filePath, file?.content || ''))
                        .catch(() => {});
                }, 200);
            }
            if (window.apkNotifiedTaskId !== window.apkCurrentTaskId) {
                window.apkNotifiedTaskId = window.apkCurrentTaskId;
                notifyOperationResult(
                    'APK反编译已完成',
                    status.filename || '反编译完成，可查看结果',
                    'success',
                    'apk',
                    { task_id: window.apkCurrentTaskId }
                );
            }
        } else if (status.status === 'error') {
            stopApkPolling();
            window.apkPendingOpenTarget = null;

            showToast(`分析失败: ${status.error}`, 'error');
            void loadApkTaskHistory(true);
            if (window.apkNotifiedTaskId !== window.apkCurrentTaskId) {
                window.apkNotifiedTaskId = window.apkCurrentTaskId;
                notifyOperationResult(
                    'APK分析失败',
                    status.error || '反编译失败',
                    'error',
                    'apk',
                    {
                        task_id: window.apkCurrentTaskId
                    }
                );
            }
        }
        return status;
    } catch (e) {
        stopApkPolling();
        $('apk-analysis-state').textContent = `状态查询失败: ${e.message}`;
        const btn = $('apk-btn-analyze');
        if (btn) {
            btn.disabled = false;
            btn.textContent = '🔬 重新分析';
        }
    } finally {
        window.apkStatusPollInFlight = false;
    }
}

async function loadApkManifest() {
    if (!window.apkCurrentTaskId) return;

    try {
        const data = await apiCall(`/api/apk/manifest/${window.apkCurrentTaskId}`);

        if (!data.success) {
            $('apk-manifest-info').innerHTML = `<div style="color: var(--danger-color);">加载失败: ${escapeHtml(data.error)}</div>`;
            return;
        }

        const manifest = data.data.manifest;
        const rawXml = data.data.raw_xml;

        const version = [
            manifest.versionName ? `版本名 ${manifest.versionName}` : '',
            manifest.versionCode ? `版本号 ${manifest.versionCode}` : ''
        ].filter(Boolean).join(' / ') || '-';
        const sdk = [
            manifest.minSdkVersion ? `min ${manifest.minSdkVersion}` : '',
            manifest.targetSdkVersion ? `target ${manifest.targetSdkVersion}` : ''
        ].filter(Boolean).join(' / ') || '-';
        const fields = [
            { label: '包名', value: manifest.package || '-', icon: '📦' },
            { label: '版本', value: version, icon: '🏷️' },
            { label: 'SDK', value: sdk, icon: '📱' },
        ];

        if (manifest.launchActivity) {
            fields.push({ label: '启动 Activity', value: manifest.launchActivity, icon: '🚀' });
        }

        $('apk-manifest-info').innerHTML = `<div class="apk-manifest-row">
            <div class="apk-manifest-label">📦 包名</div>
            <div class="apk-manifest-value">${escapeHtml(manifest.package || '-')}</div>
            <div class="apk-manifest-label">🏷️ 版本</div>
            <div class="apk-manifest-value">${escapeHtml(version)}</div>
            <div class="apk-manifest-label">📱 SDK</div>
            <div class="apk-manifest-value">${escapeHtml(sdk)}</div>
        </div>`;

        $('apk-raw-xml').textContent = rawXml;
    } catch (e) {
        $('apk-manifest-info').innerHTML = `<div style="color: var(--danger-color);">加载失败: ${escapeHtml(e.message)}</div>`;
    }
}

async function loadApkPermissions() {
    if (!window.apkCurrentTaskId) return;

    try {
        const data = await apiCall(`/api/apk/permissions/${window.apkCurrentTaskId}`);

        if (!data.success) {
            $('apk-permissions-list').innerHTML = `<div style="color: var(--danger-color); padding: 20px; text-align: center;">加载失败: ${escapeHtml(data.error)}</div>`;
            return;
        }

        const permissions = data.data.permissions;
        $('apk-perm-count').textContent = permissions.length;

        if (permissions.length === 0) {
            $('apk-permissions-list').innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-secondary);">未发现权限声明</div>';
            return;
        }

        $('apk-permissions-list').innerHTML = permissions.map((p, i) =>
            `<div class="apk-permission-item">
                <div class="apk-perm-left">
                    <span class="apk-perm-index">${i + 1}.</span>
                    <span class="apk-perm-name">${escapeHtml(p.name)}</span>
                </div>
                <span class="apk-perm-short">${escapeHtml(p.short_name)}</span>
            </div>`
        ).join('');
    } catch (e) {
        $('apk-permissions-list').innerHTML = `<div style="color: var(--danger-color); padding: 20px;">加载失败: ${escapeHtml(e.message)}</div>`;
    }
}

async function loadApkSourceTree(path = '') {
    if (!window.apkCurrentTaskId) return;

    try {
        const data = await apiCall(`/api/apk/source/${window.apkCurrentTaskId}?path=${encodeURIComponent(path)}`);

        if (!data.success) {
            $('apk-source-tree').innerHTML = `<div style="color: var(--danger-color); padding: 20px;">加载失败: ${escapeHtml(data.error)}</div>`;
            return;
        }

        const items = data.data.items;

        // 不再在加载时构建索引，改为首次搜索时构建

        if (items.length === 0) {
            $('apk-source-tree').innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-secondary);">目录为空</div>';
            return;
        }

        if (!path) {
            $('apk-source-tree').innerHTML = '';
            renderApkSourceItems(items, $('apk-source-tree'), '');
        } else {
            const container = document.querySelector(`[data-apk-path="${path}"]`);
            if (container) {
                const childContainer = container.nextElementSibling;
                if (childContainer && childContainer.classList.contains('apk-tree-children')) {
                    childContainer.innerHTML = '';
                    renderApkSourceItems(items, childContainer, path);
                }
            }
        }
    } catch (e) {
        $('apk-source-tree').innerHTML = `<div style="color: var(--danger-color); padding: 20px;">加载失败: ${escapeHtml(e.message)}</div>`;
    }
}

function renderApkSourceItems(items, container, parentPath) {
    const fragment = document.createDocumentFragment();
    items.forEach(item => {
        const itemDiv = document.createElement('div');

        const itemHeader = document.createElement('div');
        itemHeader.className = `apk-tree-item ${item.type}`;
        itemHeader.setAttribute('data-apk-path', item.path);

        const nameSpan = document.createElement('span');
        nameSpan.textContent = item.name;
        itemHeader.appendChild(nameSpan);

        if (item.type === 'dir') {
            const childContainer = document.createElement('div');
            childContainer.className = 'apk-tree-children';

            itemHeader.addEventListener('click', async () => {
                if (childContainer.classList.contains('expanded')) {
                    childContainer.classList.remove('expanded');
                    return;
                }

                if (childContainer.children.length === 0) {
                    await loadApkSourceTree(item.path);
                }

                childContainer.classList.add('expanded');
            });

            itemDiv.appendChild(itemHeader);
            itemDiv.appendChild(childContainer);
        } else {
            itemHeader.addEventListener('click', () => viewApkFile(item.path));
            itemDiv.appendChild(itemHeader);
        }

        fragment.appendChild(itemDiv);
    });
    container.appendChild(fragment);
}

function getApkFileLabel(filePath) {
    const parts = String(filePath || '').split(/[\\/]/);
    return parts[parts.length - 1] || filePath || '-';
}

function renderApkFileTabs() {
    const tabsEl = $('apk-file-tabs');
    const viewer = $('apk-file-viewer');
    if (!tabsEl || !viewer) return;

    tabsEl.innerHTML = '';
    window.apkOpenFiles.forEach((file, path) => {
        const tab = document.createElement('button');
        tab.type = 'button';
        tab.className = `apk-file-tab${path === window.apkActiveFilePath ? ' active' : ''}`;
        tab.title = path;

        const label = document.createElement('span');
        label.className = 'apk-file-tab-label';
        label.textContent = getApkFileLabel(path);
        tab.appendChild(label);

        const closeBtn = document.createElement('span');
        closeBtn.className = 'apk-file-tab-close';
        closeBtn.textContent = '×';
        closeBtn.title = '关闭文件';
        closeBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            closeApkFileTab(path);
        });
        tab.appendChild(closeBtn);

        tab.addEventListener('click', () => activateApkFileTab(path));
        tabsEl.appendChild(tab);
    });

    viewer.style.display = window.apkOpenFiles.size ? 'flex' : 'none';
}

function activateApkFileTab(filePath, targetLine = null) {
    const file = window.apkOpenFiles.get(filePath);
    if (!file) return;

    const contentEl = $('apk-file-content');
    const pathEl = $('apk-file-path');
    window.apkActiveFilePath = filePath;
    pathEl.textContent = filePath;
    contentEl.dataset.currentPath = filePath;

    if (file.error) {
        contentEl.textContent = file.error;
    } else if (file.contentHtml) {
        contentEl.innerHTML = file.contentHtml;
        bindApkCodeNavigation(contentEl);
    } else {
        contentEl.textContent = '加载中...';
    }

    renderApkFileTabs();
    if (targetLine) {
        requestAnimationFrame(() => scrollApkCodeToLine(targetLine));
    }
}

function closeApkFileTab(filePath) {
    if (!window.apkOpenFiles.has(filePath)) return;

    const paths = Array.from(window.apkOpenFiles.keys());
    const closedIndex = paths.indexOf(filePath);
    window.apkOpenFiles.delete(filePath);

    if (window.apkActiveFilePath === filePath) {
        const remaining = Array.from(window.apkOpenFiles.keys());
        window.apkActiveFilePath = remaining[Math.max(0, Math.min(closedIndex, remaining.length - 1))] || null;
        if (window.apkActiveFilePath) {
            activateApkFileTab(window.apkActiveFilePath);
        } else {
            const contentEl = $('apk-file-content');
            const pathEl = $('apk-file-path');
            if (contentEl) contentEl.textContent = '';
            if (pathEl) pathEl.textContent = '';
        }
    }

    renderApkFileTabs();
}

async function viewApkFile(filePath) {
    return viewApkFileAt(filePath, null);
}

// Java 语法高亮常量。
const JAVA_KEYWORDS = new Set([
    'abstract', 'assert', 'boolean', 'break', 'byte', 'case', 'catch', 'char', 'class',
    'const', 'continue', 'default', 'do', 'double', 'else', 'enum', 'extends', 'final',
    'finally', 'float', 'for', 'goto', 'if', 'implements', 'import', 'instanceof', 'int',
    'interface', 'long', 'native', 'new', 'package', 'private', 'protected', 'public',
    'return', 'short', 'static', 'strictfp', 'super', 'switch', 'synchronized', 'this',
    'throw', 'throws', 'transient', 'try', 'void', 'volatile', 'while', 'true', 'false',
    'null'
]);
const JAVA_IDENTIFIER_RE = /[A-Za-z_$][A-Za-z0-9_$]*/g;

function renderApkCodeContent(content, filePath) {
    const source = String(content || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    const lightMode = source.length > 300000;
    const lines = source.split('\n');

    return lines.map((line, index) => {
        const lineNo = index + 1;
        let html;
        if (lightMode) {
            html = escapeHtml(line);
        } else {
            html = '';
            let lastIndex = 0;
            JAVA_IDENTIFIER_RE.lastIndex = 0;
            let match;
            while ((match = JAVA_IDENTIFIER_RE.exec(line)) !== null) {
                html += escapeHtml(line.slice(lastIndex, match.index));
                const token = match[0];
                if (JAVA_KEYWORDS.has(token)) {
                    html += `<span class="apk-code-keyword">${escapeHtml(token)}</span>`;
                } else {
                    html += `<span class="apk-code-symbol" data-symbol="${escapeHtml(token)}">${escapeHtml(token)}</span>`;
                }
                lastIndex = match.index + token.length;
            }
            html += escapeHtml(line.slice(lastIndex));
        }

        return `<div class="apk-code-line" id="apk-code-line-${lineNo}" data-line="${lineNo}">
            <span class="apk-code-line-no">${lineNo}</span><span class="apk-code-text">${html || ' '}</span>
        </div>`;
    }).join('');
}

async function jumpToApkDefinition(symbol, currentPath, currentLine) {
    if (!window.apkCurrentTaskId) return;

    if (!symbol) return;
    try {
        const params = new URLSearchParams({
            symbol,
            path: currentPath || '',
            line: String(currentLine || 0)
        });
        const data = await apiCall(`/api/apk/definition/${window.apkCurrentTaskId}?${params.toString()}`);

        if (!data.success || !data.data?.definition) {
            showToast(data.error || `未找到定义: ${symbol}`, 'warning');
            return;
        }

        const definition = data.data.definition;
        await viewApkFileAt(definition.path, definition.line);
    } catch (e) {
        showToast(`跳转失败: ${e.message}`, 'error');
    }
}

async function viewApkFileAt(filePath, targetLine = null) {
    if (!window.apkCurrentTaskId) return;

    const existingFile = window.apkOpenFiles.get(filePath);
    if (existingFile && (existingFile.contentHtml || existingFile.error)) {
        activateApkFileTab(filePath, targetLine);
        return existingFile;
    }

    window.apkOpenFiles.set(filePath, { loading: true });
    activateApkFileTab(filePath);

    try {
        const data = await apiCall(`/api/apk/source/${window.apkCurrentTaskId}?path=${encodeURIComponent(filePath)}&view=true`);

        if (data.success) {
            window.apkOpenFiles.set(filePath, {
                loading: false,
                content: data.data.content,
                contentHtml: renderApkCodeContent(data.data.content, filePath)
            });
        } else {
            window.apkOpenFiles.set(filePath, {
                loading: false,
                error: `加载失败: ${data.error}`
            });
        }
    } catch (e) {
        window.apkOpenFiles.set(filePath, {
            loading: false,
            error: `加载失败: ${e.message}`
        });
    }

    activateApkFileTab(filePath, targetLine);
    return window.apkOpenFiles.get(filePath);
}

async function openApkPendingSourceTarget(target) {
    const paths = Array.from(new Set([
        target?.filePath,
        ...(Array.isArray(target?.fallbackPaths) ? target.fallbackPaths : [])
    ].filter(Boolean)));
    if (paths.length) {
        switchApkTab('source');
    }
    let lastFile = null;
    for (const filePath of paths) {
        const file = await viewApkFileAt(filePath, target?.line || null);
        lastFile = file ? { ...file, path: filePath } : null;
        if (file && !file.error) return lastFile;
        if (paths.length > 1 && window.apkOpenFiles?.has(filePath)) {
            window.apkOpenFiles.delete(filePath);
        }
    }
    if (lastFile?.path) {
        window.apkOpenFiles.set(lastFile.path, lastFile);
        activateApkFileTab(lastFile.path, target?.line || null);
    }
    if (paths.length) {
        showToast(`未能自动打开源码: ${paths[0]}`, 'warning');
    }
    return lastFile;
}

function bindApkCodeNavigation(contentEl) {
    if (!contentEl || contentEl.dataset.navigationBound === 'true') return;
    contentEl.dataset.navigationBound = 'true';
    contentEl.addEventListener('click', async (event) => {
        const symbolEl = event.target.closest('.apk-code-symbol');
        if (!symbolEl || !event.ctrlKey) return;

        event.preventDefault();
        const lineEl = symbolEl.closest('.apk-code-line');
        const symbol = symbolEl.dataset.symbol;
        const currentPath = contentEl.dataset.currentPath || '';
        const currentLine = Number(lineEl?.dataset.line || 0);
        await jumpToApkDefinition(symbol, currentPath, currentLine);
    });
}

function scrollApkCodeToLine(line) {
    const contentEl = $('apk-file-content');
    const target = contentEl?.querySelector(`#apk-code-line-${line}`);
    if (!target) return;

    target.scrollIntoView({ block: 'center' });
    target.classList.add('apk-code-line-target');
    setTimeout(() => target.classList.remove('apk-code-line-target'), 1800);
}

function closeApkFileViewer() {
    if (!window.apkOpenFiles || typeof window.apkOpenFiles.clear !== 'function') {
        window.apkOpenFiles = new Map();
    } else {
        window.apkOpenFiles.clear();
    }
    window.apkActiveFilePath = null;
    const contentEl = $('apk-file-content');
    const pathEl = $('apk-file-path');
    if (contentEl) contentEl.textContent = '';
    if (pathEl) pathEl.textContent = '';
    renderApkFileTabs();
}

function switchApkTab(tabName, {persist = true} = {}) {
    const target = APK_ANALYSIS_TABS.has(tabName) ? tabName : 'manifest';
    if (persist) {
        try { window.sessionStorage.setItem(APK_ANALYSIS_TAB_STORAGE_KEY, target); } catch (_error) {}
    }
    document.querySelectorAll('[data-apk-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.apkTab === target);
    });

    $('apk-tab-manifest').style.display = target === 'manifest' ? 'flex' : 'none';
    $('apk-tab-permissions').style.display = target === 'permissions' ? 'block' : 'none';
    $('apk-tab-source').style.display = target === 'source' ? 'block' : 'none';

    if (target === 'permissions' && !$('apk-permissions-list').dataset.loaded) {
        $('apk-permissions-list').dataset.loaded = 'true';
        loadApkPermissions();
    }
    if (target === 'source' && !$('apk-source-tree').dataset.loaded) {
        initApkSourceResizer();
        $('apk-source-tree').dataset.loaded = 'true';
        loadApkSourceTree('');
    } else if (target === 'source') {
        initApkSourceResizer();
    }
}

function downloadApkSource() {
    if (!window.apkCurrentTaskId) return;
    const link = document.createElement('a');
    link.href = `/api/apk/download/${window.apkCurrentTaskId}`;
    link.download = '';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

function resetApkAnalysis() {
    stopApkPolling();
    window.apkCurrentTaskId = null;
    window.apkNotifiedTaskId = null;
    window.apkPendingOpenTarget = null;
    window.apkLastSearchMatches = [];

    setApkUploadEmpty(true);
    $('apk-analysis-status').style.display = 'none';
    $('apk-analysis-result').style.display = 'none';
    $('apk-file-input').value = '';
    $('apk-upload-progress').style.display = 'none';
    $('apk-progress-fill').style.width = '0%';
    $('apk-analysis-progress-container').style.display = 'none';
    $('apk-analysis-progress-bar').style.width = '0%';
    const analyzeBtn = $('apk-btn-analyze');
    if (analyzeBtn) analyzeBtn.style.display = 'none';
    const history = $('apk-task-history');
    if (history) history.value = '';
    resetApkTaskPanels();
}

// ==================== APK 文件搜索功能 ====================

async function filterApkFiles() {
    const query = $('apk-file-search')?.value?.toLowerCase() || '';
    const resultsEl = $('apk-search-results');

    if (!query || query.length < 2) {
        if (resultsEl) resultsEl.style.display = 'none';
        return;
    }

    let matches = [];
    if (window.apkCurrentTaskId) {
        try {
            const data = await apiCall(`/api/apk/search/${window.apkCurrentTaskId}?q=${encodeURIComponent(query)}&limit=20`);
            matches = data.success ? (data.data.items || []) : [];
        } catch (e) {
            debugLog('[APK Search] backend search failed:', e.message);
        }
    }
    window.apkLastSearchMatches = matches;

    if (!resultsEl || matches.length === 0) {
        if (resultsEl) resultsEl.style.display = 'none';
        return;
    }

    resultsEl.innerHTML = '';
    for (const file of matches) {
        const item = document.createElement('div');
        item.className = 'apk-search-result-item';
        item.onclick = () => jumpToApkFile(file.path);
        item.innerHTML = `<span class="apk-search-result-name">${escapeHtml(file.name)}</span><span class="apk-search-result-path">${escapeHtml(file.path)}</span>`;
        resultsEl.appendChild(item);
    }
    resultsEl.style.display = 'block';

    const searchEl = $('apk-file-search');
    if (searchEl) {
        const rect = searchEl.getBoundingClientRect();
        resultsEl.style.position = 'absolute';
        resultsEl.style.top = (rect.bottom + window.scrollY) + 'px';
        resultsEl.style.left = (rect.left + window.scrollX) + 'px';
        resultsEl.style.width = rect.width + 'px';
    }
}

const debounceFilterApkFiles = debounce(filterApkFiles, 300);

function jumpToApkFile(selectedPath) {
    const query = $('apk-file-search')?.value?.toLowerCase() || '';
    const resultsEl = $('apk-search-results');
    let path = selectedPath;
    if (!path && query) {
        path = (window.apkLastSearchMatches || [])[0]?.path;
    }
    if (!path) {
        showToast('未找到匹配的文件', 'warning');
        return;
    }
    if (resultsEl) resultsEl.style.display = 'none';
    viewApkFile(path);
    expandApkTreeToPath(path);
}

function clearApkSearch() {
    const searchEl = $('apk-file-search');
    const resultsEl = $('apk-search-results');
    if (searchEl) searchEl.value = '';
    if (resultsEl) resultsEl.style.display = 'none';
}

function expandApkTreeToPath(filePath) {
    const parts = filePath.split('/');
    let currentPath = '';
    for (let i = 0; i < parts.length - 1; i++) {
        currentPath = (currentPath ? `${currentPath}/` : '') + parts[i];
        const container = document.querySelector(`[data-apk-path="${CSS.escape(currentPath)}"]`);
        const childContainer = container?.querySelector('.apk-tree-children');
        if (childContainer?.classList.contains('apk-tree-children')) {
            childContainer.classList.add('expanded');
        }
    }
}

document.addEventListener('click', (event) => {
    const resultsEl = $('apk-search-results');
    const searchEl = $('apk-file-search');
    if (resultsEl && searchEl && !resultsEl.contains(event.target) && event.target !== searchEl) {
        resultsEl.style.display = 'none';
    }
});

window.filterApkFiles = filterApkFiles;
window.jumpToApkFile = jumpToApkFile;
window.clearApkSearch = clearApkSearch;
window.expandApkTreeToPath = expandApkTreeToPath;
window.debounceFilterApkFiles = debounceFilterApkFiles;
window.handleApkFile = handleApkFile;
window.initApkAnalysisPage = initApkAnalysisPage;
window.loadApkTaskHistory = loadApkTaskHistory;
window.restoreApkTask = restoreApkTask;
