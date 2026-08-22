// ==================== Tailscale 内网地址 ====================

/**
 * 复制部署脚本命令
 */
function copyDeployCommand() {
    // 发布包由 install.sh package 生成并签名。用通配符匹配最新的包，避免用户
    // 手填 <VERSION>（bash 会把尖括号当成重定向报错，且版本号默认是时间戳）。
    const deployCommand =
        'PKG=$(ls -t gms-web-app-*.tar.gz | head -1) && ' +
        'sha256sum -c "${PKG}.sha256" && ' +
        'gpg --verify "${PKG}.sig" "${PKG}" && ' +
        'tar -xzf "${PKG}" && cd gms-web-app && sudo ./install.sh';

    const clipboardWrite = navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(deployCommand)
        : Promise.reject(new Error('Clipboard API unavailable'));

    clipboardWrite.then(() => {
        showToast('✓ 已复制签名发布包部署命令', 'success');
    }).catch(() => {
        // 备用复制方案
        const textArea = document.createElement('textarea');
        textArea.value = deployCommand;
        textArea.style.position = 'fixed';
        textArea.style.left = '-9999px';
        document.body.appendChild(textArea);
        textArea.select();
        try {
            document.execCommand('copy');
            showToast('✓ 已复制签名发布包部署命令', 'success');
        } catch (e) {
            showToast('复制失败', 'error');
        }
        document.body.removeChild(textArea);
    });
}

/**
 * 显示 Tailscale 信息弹框，自动检测并启动 Tailscale
 * 缓存结果 5 分钟，避免每次打开弹框都调用 API
 */
let _tailscaleCache = { url: null, ts: 0 };
const TAILSCALE_CACHE_TTL = 5 * 60 * 1000;

async function showTailscaleInfoModal() {
    const display = document.getElementById('tailscale-url-display');
    ModalManager.open('tailscale-info-modal');

    if (_tailscaleCache.url && Date.now() - _tailscaleCache.ts < TAILSCALE_CACHE_TTL) {
        display.value = _tailscaleCache.url;
        return;
    }

    display.value = '正在检查 Tailscale...';
    try {
        const granted = await requestElevatedAccess('启动或检查 Tailscale');
        if (!granted) {
            display.value = '已取消管理员提权';
            return;
        }
        const data = await apiCall('/api/tailscale/ensure', 'POST');

        if (!data.success || !data.public_url) {
            throw new Error(data.error || 'Tailscale 不可用');
        }

        window.tailscaleUrl = data.public_url;
        _tailscaleCache = { url: data.public_url, ts: Date.now() };
        display.value = data.public_url;
    } catch (error) {
        _tailscaleCache = { url: null, ts: 0 };
        display.value = '未连接';
        showToast('Tailscale 未连接，请在终端执行 sudo tailscale up 授权登录', 'warning');
    }
}

function closeTailscaleInfoModal() {
    ModalManager.close('tailscale-info-modal');
}

function copyTailscaleAccessUrl() {
    if (window.tailscaleUrl) {
        copyText(window.tailscaleUrl, { successMsg: '✓ Tailscale 地址已复制' });
    } else {
        showToast('暂无可用地址', 'error');
    }
}


// 跳转到测试界面，自动填入测试类型、测试模块、测试用例，并匹配测试套件
function goToTestCase(testType, moduleName, testCaseName) {
    try {
        // 切换到测试界面
        switchPage('test');

        // 等待页面切换完成后填充数据
        setTimeout(() => {
            debugLog(`[goToTestCase] 填充数据: testType=${testType}, module=${moduleName}, testCase=${testCaseName}`);

            // 设置测试类型
            const testTypeSelect = document.getElementById('test-type');
            if (testTypeSelect && testType) {
                testTypeSelect.value = testType;
                debugLog(`[goToTestCase] 已设置测试类型: ${testType}`);
            }

            // 填入测试模块
            const testModuleInput = document.getElementById('test-module');
            if (testModuleInput && moduleName && moduleName !== '未知模块') {
                testModuleInput.value = moduleName;
                debugLog(`[goToTestCase] 已设置测试模块: ${moduleName}`);
            }

            // 填入测试用例
            const testCaseInput = document.getElementById('test-case');
            if (testCaseInput && testCaseName && testCaseName !== '未知用例') {
                testCaseInput.value = testCaseName;
                debugLog(`[goToTestCase] 已设置测试用例: ${testCaseName}`);
            }

            // 互斥：填入模块/用例时清空测试报告
            enforceFieldExclusion('module_case');

            // 根据测试类型自动选择测试套件
            if (testType && typeof autoSelectTestSuite === 'function') {
                autoSelectTestSuite(testType);
                debugLog(`[goToTestCase] 已自动匹配测试套件: ${testType}`);
            }

            showToast(`已跳转到测试界面，请选择设备后开始测试`, 'success');
        }, 200);
    } catch (error) {
        console.error('[goToTestCase] Error:', error);
        showToast('跳转失败: ' + error.message, 'error');
    }
}

// Redmine 回复对话框
function openRedmineReplyModal(moduleName, testCaseName, failureIndex, issueIdFromReport) {
    const modalId = 'redmine-reply-modal-' + Date.now();
    const issueInputId = `${modalId}-issue-id`;
    const replyTextId = `${modalId}-reply-text`;
    const fileInputId = `${modalId}-files`;
    const fileListId = `${modalId}-file-list`;
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.style.cssText = 'z-index: 10001;';

    // 从隐藏的原始数据元素中获取完整的错误信息（保留换行和格式）
    const failureReasonElement = document.getElementById(`failure-reason-raw-${failureIndex}`);
    const failureReason = failureReasonElement ? failureReasonElement.textContent.trim() : '';

    // 生成默认回复模板
    const defaultReply = '**测试模块**: ' + moduleName + '\n\n' +
        '**测试用例**: ' + testCaseName + '\n\n' +
        '**报错信息**:\n' +
        '<pre>\n' + failureReason + '\n</pre>';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 700px; max-height: 85vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">📝 Redmine回复</span>
                <span class="modal-close" onclick="ModalManager.close('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
                <div style="margin-bottom: 16px;">
                    <label style="display: block; margin-bottom: 6px; font-size: 13px; font-weight: 600; color: var(--text-primary);">Redmine Issue ID</label>
                    <input type="text" id="${issueInputId}" data-redmine-issue-input value="${issueIdFromReport}" placeholder="输入 Redmine Issue ID"
                           style="width: 100%; padding: 10px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--darker-bg); color: var(--text-primary); font-size: 14px; font-family: 'Courier New', monospace;">
                </div>
                <div style="margin-bottom: 16px;">
                    <label style="display: block; margin-bottom: 6px; font-size: 13px; font-weight: 600; color: var(--text-primary);">回复内容</label>
                    <textarea id="${replyTextId}" data-redmine-reply-text rows="10" placeholder="输入回复内容..."
                              style="width: 100%; padding: 10px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--darker-bg); color: var(--text-primary); font-size: 13px; font-family: 'Courier New', monospace; white-space: pre-wrap; resize: vertical;">${defaultReply}</textarea>
                </div>
                <div style="margin-bottom: 16px;">
                    <label style="display: block; margin-bottom: 6px; font-size: 13px; font-weight: 600; color: var(--text-primary);">📎 附件</label>
                    <input type="file" id="${fileInputId}" data-redmine-files multiple
                           style="display: none;"
                           onchange="updateRedmineFileList('${fileInputId}', '${fileListId}')">
                    <div id="${fileInputId}-drop" class="redmine-drop-zone" data-redmine-drop
                         onclick="document.getElementById('${fileInputId}').click()"
                         style="padding: 20px 14px; background: var(--secondary-bg); color: var(--text-muted); border: 2px dashed var(--border-color); border-radius: 6px; cursor: pointer; font-size: 12px; width: 100%; text-align: center; transition: all 0.2s; user-select: none;">
                        📎 拖拽文件到此处，或点击选择文件
                    </div>
                    <div id="${fileListId}" style="margin-top: 8px;"></div>
                </div>
                <div style="display: flex; gap: 10px; justify-content: flex-end;">
                    <button onclick="ModalManager.close('${modalId}')"
                            style="padding: 8px 16px; background: var(--secondary-bg); color: var(--text-primary); border: none; border-radius: 6px; cursor: pointer; font-size: 13px;">取消</button>
                    <button onclick="confirmAndSendRedmineReply('${modalId}')"
                            style="padding: 8px 16px; background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; box-shadow: 0 2px 4px rgba(245, 87, 108, 0.3);">确认并发送</button>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    ModalManager.open(modalId);

    // 绑定拖拽事件
    const dropZone = document.getElementById(`${fileInputId}-drop`);
    if (dropZone) {
        dropZone.addEventListener('dragover', (e) => { e.preventDefault(); e.stopPropagation(); dropZone.classList.add('drag-over'); });
        dropZone.addEventListener('dragleave', (e) => { e.preventDefault(); e.stopPropagation(); dropZone.classList.remove('drag-over'); });
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault(); e.stopPropagation();
            dropZone.classList.remove('drag-over');
            if (!e.dataTransfer?.files?.length) return;
            const input = document.getElementById(fileInputId);
            const dt = new DataTransfer();
            if (input.files) { for (const f of input.files) dt.items.add(f); }
            for (const f of e.dataTransfer.files) dt.items.add(f);
            input.files = dt.files;
            updateRedmineFileList(fileInputId, fileListId);
        });
    }
    return modalId;
}

function updateRedmineFileList(fileInputId, fileListId) {
    const input = document.getElementById(fileInputId);
    const container = document.getElementById(fileListId);
    if (!input || !container) return;
    const files = input.files;
    if (!files || !files.length) { container.innerHTML = ''; return; }
    container.innerHTML = Array.from(files).map((f, i) => {
        const size = f.size >= 1048576 ? (f.size / 1048576).toFixed(1) + ' MB' : (f.size / 1024).toFixed(0) + ' KB';
        return `<div class="redmine-file-item">
            <span class="redmine-file-name">📎 ${escapeHtml(f.name)} <span class="redmine-file-size">(${size})</span></span>
            <span class="redmine-file-remove" onclick="removeRedmineFile('${fileInputId}', '${fileListId}', ${i})">✕</span>
        </div>`;
    }).join('');
}

function removeRedmineFile(fileInputId, fileListId, index) {
    const input = document.getElementById(fileInputId);
    if (!input) return;
    const dt = new DataTransfer();
    const files = input.files;
    for (let i = 0; i < files.length; i++) {
        if (i !== index) dt.items.add(files[i]);
    }
    input.files = dt.files;
    updateRedmineFileList(fileInputId, fileListId);
}

// 确认并发送 Redmine 回复
async function confirmAndSendRedmineReply(modalId) {
    const modal = document.getElementById(modalId);
    const issueId = modal?.querySelector('[data-redmine-issue-input]')?.value?.trim();
    const replyText = modal?.querySelector('[data-redmine-reply-text]')?.value?.trim();
    const fileInput = modal?.querySelector('[data-redmine-files]');

    if (!issueId) {
        showToast('❌ 请输入 Redmine Issue ID', 'error');
        return;
    }

    if (!replyText) {
        showToast('❌ 回复内容不能为空', 'error');
        return;
    }

    const files = fileInput?.files;
    const hasFiles = files && files.length > 0;

    // 立即关闭弹窗，提升响应速度
    ModalManager.close(modalId);
    const attachHint = hasFiles ? `（含 ${files.length} 个附件）` : '';
    showToast('📤 正在发送回复' + attachHint + '...', 'info');

    // 构建 FormData
    const formData = new FormData();
    formData.append('issue_id', issueId);
    formData.append('reply_text', replyText);
    if (hasFiles) {
        for (const f of files) {
            formData.append('files', f);
        }
    }

    // 异步发送请求，不阻塞 UI
    fetch('/api/redmine/reply', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(result => {
        if (result.success) {
            const replyData = result.data || {};
            const attachMsg = replyData.attachments ? `，携带 ${replyData.attachments} 个附件` : '';
            showToast(`✅ 回复已成功发送到 Redmine #${issueId}${attachMsg}`, 'success');
            if (replyData.issue_url) {
                setTimeout(() => window.open(replyData.issue_url, '_blank', 'noopener'), 800);
            }
        } else {
            showToast('❌ 发送失败：' + (result.error || result.detail || '未知错误'), 'error');
        }
    })
    .catch(error => {
        console.error('[Redmine Reply] Error:', error);
        showToast('❌ 发送失败：' + error.message, 'error');
    });
}

function resetReportAnalysis() {
    // Invalidate every in-flight completion before clearing the DOM so an old
    // upload/URL request cannot repopulate the workbench after the user clicks
    // "清除".
    reportUploadGeneration += 1;
    if (currentReportUploadRequest && currentReportUploadRequest.readyState !== 4) {
        currentReportUploadRequest.abort();
    }
    currentReportUploadRequest = null;
    if (currentRedmineRequest) currentRedmineRequest.abort();
    currentRedmineRequest = null;

    const resultDiv = ensureReportAnalysisResultStructure();
    const uploadZone = $('report-upload-zone');
    const summaryDiv = $('report-summary');
    const detailsDiv = $('report-details');
    const failuresDiv = $('report-failures');
    const failureList = $('report-failure-list');

    // 清空分析结果但保留容器结构。
    if (resultDiv) resultDiv.style.display = 'none';
    if (summaryDiv) summaryDiv.innerHTML = '';
    if (detailsDiv) detailsDiv.innerHTML = '';
    if (failuresDiv) failuresDiv.style.display = 'none';
    if (failureList) failureList.innerHTML = '';

    // Reset upload zone to empty state
    if (uploadZone) {
        uploadZone.classList.add('upload-empty');
        const content = uploadZone.querySelector('.report-upload-content');
        if (content) content.style.opacity = '1';
    }
    resetReportUploadProgress();
    const fileInput = $('report-file-input');
    const folderInput = $('report-folder-input');
    if (fileInput) fileInput.value = '';
    if (folderInput) folderInput.value = '';

    window.currentReportName = '';
    window.currentReportAnalysisData = null;
    window.reportDiagnosis = null;
    ModalManager.close('report-diagnosis-modal');
    const minimized = $('report-diagnosis-minimized');
    if (minimized) minimized.style.display = 'none';

    debugLog('[resetReportAnalysis] Report analysis reset complete');
}

/**
 * 将当前报告分析结果作为 HTML 邮件发送。
 * 复用 POST /api/email/send（SMTP 配置来自 Redmine 看板设置）。
 */
async function sendReportAnalysisEmail() {
    const data = window.currentReportAnalysisData;
    if (!data || !data.summary) {
        showToast('请先生成报告分析结果', 'warning');
        return;
    }
    const to = prompt('收件人邮箱（多个用逗号或分号分隔）：', '');
    if (!to || !to.trim()) return;
    const cc = (prompt('抄送（可留空，多个用逗号或分号分隔）：', '') || '').trim();

    const esc = (s) => String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');

    const s = data.summary || {};
    const d = data.details || {};
    const reportName = data.report_name || data.test_result?.test_name || '测试报告';
    const rows = [
        ['测试类型', d.test_type],
        ['套件版本', d.suite_version],
        ['Android版本', d.android_version],
        ['SOC平台', d.soc_platform],
        ['总用例数', s.total],
        ['通过', s.pass],
        ['失败', s.fail],
        ['通过率', s.pass_rate],
    ].filter(([, v]) => v !== undefined && v !== null && v !== '');

    const summaryHtml = rows.map(([k, v]) =>
        `<tr><td style="padding:4px 12px 4px 0;color:#666;">${esc(k)}</td><td style="padding:4px 0;"><b>${esc(v)}</b></td></tr>`
    ).join('');

    const failures = Array.isArray(data.failures) ? data.failures : [];
    const failureHtml = failures.length ? `
        <h3 style="margin:18px 0 8px;">❌ 失败用例（${failures.length}）</h3>
        ${failures.map((f, i) => `
            <div style="border:1px solid #eee;border-radius:6px;padding:10px;margin-bottom:8px;">
                <div><b>${i + 1}. ${esc(f.name || '未知用例')}</b> <span style="color:#888;">[${esc(f.module || '未知模块')}]</span></div>
                <pre style="white-space:pre-wrap;background:#fafafa;padding:8px;margin-top:6px;font-size:12px;border-radius:4px;">${esc(f.reason || '无失败原因')}</pre>
            </div>
        `).join('')}
    ` : '<p style="color:#888;">无失败用例 🎉</p>';

    const body = `
        <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#222;max-width:760px;">
            <h2 style="margin:0 0 12px;">📊 测试报告分析：${esc(reportName)}</h2>
            <table style="border-collapse:collapse;font-size:13px;">${summaryHtml}</table>
            ${failureHtml}
        </div>`;

    const subject = `测试报告分析 - ${reportName}（通过率 ${s.pass_rate || 'N/A'}）`;
    try {
        const resp = await fetch('/api/email/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                to: to.trim(),
                cc: cc || undefined,
                subject,
                body,
                is_html: true,
                sender_name: '报告分析',
            }),
        });
        const result = await resp.json().catch(() => ({ success: false }));
        if (result.success) {
            showToast(`邮件已发送至 ${result.data.to.length} 位收件人`, 'success');
        } else {
            showToast('邮件发送失败：' + (result.error || '未知错误'), 'error');
        }
    } catch (err) {
        showToast('邮件发送失败：' + (err.message || err), 'error');
    }
}

/**
 * 按分类筛选
 */
function filterByCategory(category) {
    currentCategoryFilter = category;

    // 更新按钮状态
    document.querySelectorAll('[data-category]').forEach(btn => {
        btn.classList.remove('active');
        if (btn.dataset.category === category) {
            btn.classList.add('active');
        }
    });

    applyFilters();
}

/**
 * 按方法筛选
 */
function filterByMethod(method) {
    currentMethodFilter = method;

    // 更新按钮状态
    document.querySelectorAll('[data-method]').forEach(btn => {
        btn.classList.remove('active');
        if (btn.dataset.method === method) {
            btn.classList.add('active');
        }
    });

    applyFilters();
}

/**
 * Debounce wrapper for search input
 */
let debounceTimer;
function debounceFilterApiDocs() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
        filterApiDocs();
    }, 300);
}

/**
 * 应用筛选
 */
function applyFilters() {
    const searchInput = $('api-search-input');
    const searchTerm = searchInput ? searchInput.value.toLowerCase() : '';

    // 筛选API
    const filteredApis = allApiDocs.filter(api => {
        // 搜索关键词匹配
        const matchesSearch = !searchTerm ||
            (api.path && api.path.toLowerCase().includes(searchTerm)) ||
            (api.description && api.description.toLowerCase().includes(searchTerm));

        // 分类匹配
        const matchesCategory = currentCategoryFilter === 'all' || api.category === currentCategoryFilter;

        // 方法匹配
        const matchesMethod = currentMethodFilter === 'all' || api.method === currentMethodFilter;

        return matchesSearch && matchesCategory && matchesMethod;
    });

    // 筛选结果保持原有顺序（allApiDocs已排序），无需重新排序
    displayApiDocs(filteredApis);

    // 更新筛选结果数量
    const filteredCountEl = $('filtered-apis-count');
    if (filteredCountEl) {
        filteredCountEl.textContent = filteredApis.length;
    }
}

/**
 * 筛选API文档（搜索框使用）
 */
function filterApiDocs() {
    applyFilters();
}

/**
 * 加载API文档列表（带缓存优化）
 * @param {boolean} forceRefresh - 强制刷新，绕过缓存
 */
async function loadApiDocs(forceRefresh = false) {
    debugLog('[API Docs] ===== loadApiDocs called =====');
    const tbody = $('api-docs-table-body');
    const hadRenderedDocs = Boolean(apiDocsCache && tbody?.children.length);
    if (tbody) tbody.setAttribute('aria-busy', 'true');
    try {
        // 检查DOM元素是否存在
        if (!tbody) {
            return;
        }

        // 检查缓存（除非强制刷新）
        const now = Date.now();
        if (!forceRefresh && apiDocsCache && (now - apiDocsCacheTime) < API_DOCS_CACHE_DURATION) {
            displayApiDocs(apiDocsCache);
            updateApiStats(apiDocsCache);
            return;
        }

        const resp = await fetch('/api/system/docs');

        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        }

        const data = await resp.json();

        if (data.apis && Array.isArray(data.apis)) {
            const filteredApis = data.apis.filter(api => api.path !== '/');

            // 为每个API添加分类信息
            const apisWithCategory = filteredApis.map(api => ({
                ...api,
                category: getApiCategory(api.path || '')
            }));

            // 按分类排序
            const sortedApis = sortApisByCategory(apisWithCategory);

            // 更新缓存
            apiDocsCache = sortedApis;
            allApiDocs = sortedApis;
            apiDocsCacheTime = now;

            displayApiDocs(sortedApis);
            updateApiStats(sortedApis);
        } else {
            throw new Error('Invalid response format: missing or invalid apis field');
        }
    } catch (e) {
        showToast('加载API文档失败: ' + e.message, 'error');

        // 显示错误状态
        if (tbody && !hadRenderedDocs) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="4" style="padding: 40px; text-align: center; color: var(--danger-color);">
                        ❌ 加载失败: ${escapeHtml(e.message)}
                    </td>
                </tr>
            `;
        }
    } finally {
        if (tbody) tbody.setAttribute('aria-busy', 'false');
    }
}

/**
 * 更新API统计数据
 */
function updateApiStats(apis) {
    const totalCount = apis.length;
    const getCount = apis.filter(api => api.method === 'GET').length;
    const postCount = apis.filter(api => api.method === 'POST').length;

    // 统计唯一的技能数量
    const uniqueSkills = new Set();
    apis.forEach(api => {
        if (api.skill && api.skill.trim()) {
            uniqueSkills.add(api.skill.trim());
        }
    });
    const skillsCount = uniqueSkills.size;

    const totalEl = $('total-apis-count');
    const getEl = $('get-apis-count');
    const postEl = $('post-apis-count');
    const filteredEl = $('filtered-apis-count');
    const skillsCountEl = $('skills-count');

    if (totalEl) totalEl.textContent = totalCount;
    if (getEl) getEl.textContent = getCount;
    if (postEl) postEl.textContent = postCount;
    if (filteredEl) filteredEl.textContent = totalCount;
    if (skillsCountEl) skillsCountEl.textContent = skillsCount;
}

// API 文档常量。
/**
 * Badge HTML generation utility
 */
function createBadge(text, colorVar, size = 'xs') {
    return `<span style="background: var(--${colorVar}); color: white; padding: ${BADGE_PADDINGS[size]}; border-radius: 3px; font-size: ${BADGE_SIZES[size]};">${escapeHtml(text)}</span>`;
}

/**
 * Get example value for parameter type
 */
function getExampleValue(type) {
    const examples = {
        'string': '"VALUE"',
        'number': '123',
        'array': '[]',
        'boolean': 'true',
        'file': '"/path/to/file"',
        'object': '{}'
    };
    return examples[type] || '"VALUE"';
}

/**
 * Format JSON response for display
 */
function formatJsonResponse(response) {
    try {
        // Try to parse as JSON
        const parsed = JSON.parse(response);
        // Format with 2-space indentation
        return JSON.stringify(parsed, null, 2);
    } catch (e) {
        // If not valid JSON, return as-is
        return response;
    }
}

/**
 * Normalize API path to handle path parameters
 */
function normalizeApiPath(apiPath) {
    const matched = PATH_PATTERNS.find(p => p.pattern.test(apiPath));
    return matched ? matched.template : apiPath;
}

/**
 * Get API details with caching
 */
function getApiDetails(apiPath) {
    // Single cache lookup (more efficient than has() + get())
    const cached = apiDetailsCache.get(apiPath);
    if (cached !== undefined) {
        return cached;
    }

    // Normalize path for path parameters
    const detailPath = normalizeApiPath(apiPath);

    // Get details or use default (frozen constant)
    const details = API_DETAILS_MAP[detailPath] || DEFAULT_API_DETAILS;

    // Cache the result
    apiDetailsCache.set(apiPath, details);
    return details;
}

/**
 * Generate curl command for an API endpoint
 * Moved to module level to avoid recreating on every render
 */
function generateCurlCommand(api, details) {
    const apiPath = api.path || '';
    if (api.method === 'GET') {
        if (apiPath === '/api/system/skills/install.sh') {
            const command = buildSkillInstallCommand();
            return {display: command, full: command};
        }
        // 特殊处理stream端点：使用 -N 而不是 -s
        const isStreamEndpoint = apiPath.includes('/api/test/logs/stream');
        // ZIP 离线包使用 -OJ；安装脚本在上方生成可直接执行的管道命令。
        const isDownloadEndpoint = apiPath === '/api/system/skills';

        let curlOptions = 'curl -s';
        if (isStreamEndpoint) {
            curlOptions = 'curl -N';
        } else if (isDownloadEndpoint) {
            curlOptions = 'curl -s -OJ';
        }

        let cmd = `${curlOptions} "${BASE_URL}${apiPath}"`;
        // Add query parameter example
        if (details.params && details.params.length > 0) {
            const queryParams = details.params.filter(p =>
                p.required && p.name !== 'force_refresh' || p.name === 'log_type' || p.name === 'report_timestamp'
            );
            if (queryParams.length > 0) {
                cmd += ` \\\n  -G \\\n  -d "${queryParams[0].name}=VALUE"`;
            }
        }
        // For GET requests, add continuation if there are params
        const displayCmd = cmd.includes('\\') ? cmd.split('\n')[0] : cmd;
        return { display: displayCmd, full: cmd };
    } else if (api.method === 'POST') {
        // 包含文件参数时使用 FormData。
        const hasFileParam = details.params && details.params.some(p => p.type === PARAM_TYPES.FILE);

        if (hasFileParam) {
            // Generate FormData format for file uploads
            let multiLineCmd = `curl -sX POST "${BASE_URL}${api.path || ''}"`;

            if (details.params && details.params.length > 0) {
                details.params.forEach(p => {
                    const placeholder = CURL_PLACEHOLDERS[p.type] || CURL_PLACEHOLDERS[PARAM_TYPES.STRING];

                    if (p.type === PARAM_TYPES.FILE) {
                        // File parameter: -F "name=@path"
                        multiLineCmd += ` \\\n  -F "${p.name}=@${placeholder}"`;
                    } else if (p.type === PARAM_TYPES.BOOLEAN) {
                        // Boolean parameter: -F "name=true"
                        multiLineCmd += ` \\\n  -F "${p.name}=${placeholder}"`;
                    } else {
                        // Other parameters: -F "name=value"
                        multiLineCmd += ` \\\n  -F "${p.name}=${placeholder}"`;
                    }
                });
            }

            const displayCmd = multiLineCmd.split('\n')[0];
            return { display: displayCmd, full: multiLineCmd };
        } else {
            // Generate JSON format for non-file uploads
            let multiLineCmd = `curl -sX POST "${BASE_URL}${api.path || ''}"`;

            // Generate request body example
            if (details.params && details.params.length > 0) {
                multiLineCmd += ` \\\n  -H "Content-Type: application/json"`;
                const bodyLines = ['{'];

                // Include all parameters including FILE type for documentation
                details.params.forEach((p, index) => {
                    // Include all parameters (both required and optional)
                    const placeholder = CURL_PLACEHOLDERS[p.type] || CURL_PLACEHOLDERS[PARAM_TYPES.STRING];

                    // Format the value based on type
                    let valueStr;
                    if (p.type === PARAM_TYPES.STRING) {
                        valueStr = `"${placeholder}"`;
                    } else if (p.type === PARAM_TYPES.NUMBER) {
                        valueStr = placeholder;
                    } else if (p.type === PARAM_TYPES.BOOLEAN) {
                        valueStr = placeholder;
                    } else if (p.type === PARAM_TYPES.ARRAY) {
                        valueStr = JSON.stringify(placeholder);
                    } else if (p.type === PARAM_TYPES.FILE) {
                        // For file type, still show in JSON format as placeholder
                        valueStr = `"${placeholder}"`;
                    } else {
                        valueStr = placeholder;
                    }

                    // Add comma if not last item
                    const comma = (index < details.params.length - 1) ? ',' : '';
                    bodyLines.push(`    "${p.name}": ${valueStr}${comma}`);
                });
                bodyLines.push('  }');

                if (bodyLines.length > 2) { // More than just '{' and '}'
                    multiLineCmd += ' \\\n  -d \'' + bodyLines.join('\n') + '\'';
                } else {
                    multiLineCmd += ` \\\n  -d '{}'`;
                }
            } else {
                // No parameters - don't add -d '{}' or Content-Type header
                // Just return the basic curl command
            }

            // Display version: only first line with continuation
            const displayCmd = multiLineCmd.split('\n')[0];

            return { display: displayCmd, full: multiLineCmd };
        }
    } else if (api.method === 'DELETE') {
        // Generate DELETE request
        let cmd = `curl -X DELETE "${BASE_URL}${api.path || ''}"`;

        // Add query parameters or request body
        if (details.params && details.params.length > 0) {
            const queryParams = details.params.filter(p => p.required || p.name === 'report_timestamp');
            if (queryParams.length > 0) {
                // Use query parameters for DELETE
                cmd += ` \\\n  -G \\\n  -d "${queryParams[0].name}=VALUE"`;
            }
        }

        const displayCmd = cmd.includes('\\') ? cmd.split('\n')[0] : cmd;
        return { display: displayCmd, full: cmd };
    } else if (api.method === 'WebSocket') {
        const wsPath = apiPath.replace('{client_id}', 'YOUR_CLIENT_ID');
        return { display: `wscat -c ${WS_BASE_URL}${wsPath}`, full: `wscat -c ${WS_BASE_URL}${wsPath}` };
    }
    return { display: `curl -s ${BASE_URL}${apiPath}`, full: `curl -s ${BASE_URL}${apiPath}` };
}

/**
 * Generate parameter descriptions HTML
 * Moved to module level to avoid recreating on every render
 */
function generateParamsHtml(details) {
    if (!details.params || details.params.length === 0) {
        return '<span style="color: var(--text-secondary);">无参数</span>';
    }

    // Use array.join() instead of string concatenation
    const parts = ['<div style="margin-top: 8px;">'];
    details.params.forEach(param => {
        const requiredBadge = createBadge(
            param.required ? '必需' : '可选',
            param.required ? 'danger-color' : 'info-color'
        );
        const typeBadge = createBadge(param.type, 'primary-color');

        parts.push(`
            <div style="margin-bottom: 4px; font-size: 10px;">
                <span style="font-family: monospace; font-weight: 600; color: var(--primary-color);">${escapeHtml(param.name)}</span>
                ${typeBadge} ${requiredBadge}
                <span style="color: var(--text-secondary); margin-left: 4px;">${escapeHtml(param.desc)}</span>
            </div>
        `);
    });
    parts.push('</div>');
    return parts.join('');
}

/**
 * Display API documentation list with collapsible details
 */
function displayApiDocs(apis) {
    const tbody = document.getElementById('api-docs-table-body');
    if (!tbody) return;

    // 批量拼接 HTML。
    const htmlParts = [];
    apis.forEach((api, index) => {
        const methodClass = api.method === 'GET' ? 'color: var(--success-color);' :
                           api.method === 'POST' ? 'color: var(--warning-color);' :
                           api.method === 'WebSocket' ? 'color: var(--primary-color);' :
                           'color: var(--text-secondary);';

        const categoryBadge = getCategoryName(api.category);

        // 获取API详细信息
        const details = getApiDetails(api.path || '');
        const curlCmdObj = generateCurlCommand(api, details);
        const paramsHtml = generateParamsHtml(details);

        // 将curl命令存储到data属性中,避免在onclick中直接传递复杂字符串
        const escapedCurlCmd = (curlCmdObj.full || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        const displayCurlCmd = curlCmdObj.display;

        htmlParts.push(`
            <tr style="border-bottom: 1px solid var(--border-color); ${index % 2 === 0 ? 'background: var(--bg-color);' : 'background: var(--light-bg);'}">
                <!-- Column 1: API Interface -->
                <td style="padding: 4px 8px; border-right: 1px solid var(--border-color); text-align: left; vertical-align: middle; width: 25%;">
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <span style="${methodClass} font-weight: 700; font-size: 13px; min-width: 90px; display: inline-block;">${api.method}</span>
                        <span style="font-family: monospace; font-size: 12px; color: var(--text-primary); word-break: break-all;">${escapeHtml(api.path || '')}</span>
                    </div>
                </td>

                <!-- Column 2: Description -->
                <td style="padding: 4px 8px; border-right: 1px solid var(--border-color); text-align: left; vertical-align: middle; width: 20%;">
                    <div style="display: flex; flex-direction: column; gap: 4px;">
                        <div style="font-size: 11px; color: var(--text-primary); font-weight: 600; line-height: 1.3;">
                            ${escapeHtml(details.title)}
                        </div>
                    </div>
                </td>

                <!-- Column 3: Skill Usage -->
                <td style="padding: 4px 8px; border-right: 1px solid var(--border-color); text-align: left; vertical-align: middle; width: 20%;">
                    <div style="display: flex; flex-direction: column; gap: 4px;">
                        <div style="font-size: 11px; color: var(--primary-color); font-weight: 600; line-height: 1.3; cursor: pointer; transition: all 0.2s;"
                             onclick="copySkillCommand(this)"
                             onmouseover="this.style.color='var(--success-color)';"
                             onmouseout="this.style.color='var(--primary-color)';"
                             title="点击复制 skill 命令">
                            ${api.skill ? escapeHtml(api.skill) : '<span style="color: var(--text-secondary);">-</span>'}
                        </div>
                    </div>
                </td>

                <!-- Column 4: Usage Method -->
                <td style="padding: 4px 8px; text-align: left; vertical-align: middle; width: 35%;">
                    <div style="display: flex; flex-direction: column; gap: 4px;">
                        <!-- Curl Command Row -->
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <pre
                                 data-cmd="${escapedCurlCmd}"
                                 style="margin: 0; padding: 2px 6px; font-family: 'Monaco', 'Menlo', monospace; font-size: 11px; color: var(--success-color); overflow-x: auto; white-space: nowrap; cursor: pointer; transition: all 0.2s; line-height: 1.3; display: block; flex: 1; background: transparent; border: none; text-overflow: ellipsis;"
                                 onclick="copyCurlCommandFromData(this)"
                                 onmouseover="this.style.color='var(--primary-color)';"
                                 onmouseout="this.style.color='var(--success-color)';"
                                 title="点击复制 curl 命令">${escapeHtml(displayCurlCmd)}</pre>
                            <button
                                id="expand-btn-${index}"
                                onclick="toggleApiDetails('${index}')"
                                style="background: var(--primary-color); color: white; border: none; padding: 2px 6px; border-radius: 3px; cursor: pointer; font-size: 12px; font-weight: 600; min-width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; transition: all 0.2s; flex-shrink: 0;"
                                title="点击展开/收起详情">
                                <span id="expand-icon-${index}">▶</span>
                            </button>
                        </div>

                        <!-- Expandable Details (Hidden by Default) -->
                        <div id="api-details-${index}" style="display: none;">
                            <div style="border-top: 1px solid var(--border-color); padding-top: 8px; margin-top: 4px;">
                                <!-- Full Curl Command -->
                                <div style="font-size: 11px; font-weight: 600; margin-bottom: 4px; color: var(--text-primary);">📜 完整curl命令:</div>
                                <pre style="font-family: 'Monaco', 'Menlo', monospace; font-size: 10px; color: var(--success-color); background: var(--darker-bg); padding: 6px; border-radius: 4px; margin-bottom: 8px; white-space: pre-wrap; word-break: break-all; cursor: pointer;" onclick="navigator.clipboard.writeText(this.textContent); this.style.background='var(--success-color)'; this.style.color='white'; setTimeout(() => { this.style.background='var(--darker-bg)'; this.style.color='var(--success-color)'; }, 200);" title="点击复制">${escapeHtml(curlCmdObj.full)}</pre>

                                <!-- Title with star if core API -->
                                <div style="font-size: 12px; font-weight: 700; color: var(--primary-color); margin-bottom: 6px;">
                                    ${details.usage.includes('⭐核心接口') ? '### ' : ''}${escapeHtml(details.title)} ${details.usage.includes('⭐核心接口') ? '⭐核心接口' : ''}
                                </div>

                                <!-- HTTP Method and Path -->
                                <div style="font-family: monospace; font-size: 11px; color: var(--text-primary); background: var(--darker-bg); padding: 6px; border-radius: 4px; margin-bottom: 8px; font-weight: 600;">
${api.method} ${api.path || ''}
${api.method === 'POST' ? 'Content-Type: application/json' : ''}
                                </div>

                                <!-- Parameters -->
                                ${details.params && details.params.length > 0 ? `
                                <div style="font-size: 11px; font-weight: 600; margin-bottom: 6px; color: var(--text-primary);">📋 请求参数说明:</div>
                                ${paramsHtml}
                                ` : ''}

                                <!-- Response Example -->
                                <div style="margin-top: 12px; font-size: 11px; font-weight: 600; margin-bottom: 4px; color: var(--text-secondary);">📤 响应示例:</div>
                                <div style="font-family: monospace; font-size: 10px; color: var(--success-color); background: var(--darker-bg); padding: 6px; border-radius: 4px; white-space: pre-wrap; word-break: break-all;">${escapeHtml(formatJsonResponse(details.response))}</div>
                            </div>
                        </div>
                    </div>
                </td>
            </tr>
        `);
    });

    tbody.innerHTML = htmlParts.join('');
}

/**
 * Toggle API details visibility
 */
window.toggleApiDetails = function(index) {
    const detailsDiv = document.getElementById(`api-details-${index}`);
    const iconSpan = document.getElementById(`expand-icon-${index}`);
    const button = document.getElementById(`expand-btn-${index}`);

    if (detailsDiv.style.display === 'none') {
        // Expand
        detailsDiv.style.display = 'block';
        iconSpan.textContent = '▼';
        button.style.background = 'var(--warning-color)';
    } else {
        // Collapse
        detailsDiv.style.display = 'none';
        iconSpan.textContent = '▶';
        button.style.background = 'var(--primary-color)';
    }
};

/**
 * 从data属性复制curl命令到剪贴板（自动添加jq格式化，但跳过纯文本端点）
 */
window.copyCurlCommandFromData = function(element) {
    const text = element.getAttribute('data-cmd');
    if (!text) {
        debugLog('[Copy] No data-cmd attribute found');
        showToast('✗ 复制失败: 未找到命令', 'error');
        return;
    }
    debugLog('[Copy] Attempting to copy:', text);

    let commandToCopy = text;
    let successMessage = '✓ curl命令已复制';

    // 检查是否为WebSocket端点（不需要jq格式化）
    const isWebSocketEndpoint = text.startsWith('wscat -c');

    // 检查是否为纯文本端点（不需要jq格式化）
    const isPlainTextEndpoint = text.includes('/api/test/logs/stream') ||
                                text.includes('/api/terminal/ws') ||
                                text.includes('/api/screen/ws') ||
                                // 匹配根路径（如 "http://localhost:5001/" 或 "http://192.168.1.10:5001/"）
                                (text.match(/http:\/\/[^\/]+:\d+\/"$/) !== null);

    if (isWebSocketEndpoint) {
        // WebSocket端点，不添加jq
        commandToCopy = text;
        successMessage = '✓ WebSocket命令已复制';
    } else if (isPlainTextEndpoint) {
        // 纯文本端点，不添加jq
        commandToCopy = text;
        successMessage = '✓ curl命令已复制';
    } else {
        // 其他JSON端点，使用 jq "."
        commandToCopy = text + ' | jq "."';
        successMessage = '✓ curl命令已复制 (含jq格式化)';
    }

    copyText(commandToCopy, { successMsg: successMessage });
};

/**
 * 显示使用实例弹窗
 */
function showUsageExamples() {
    ModalManager.open('usage-examples-modal');
}


/**
 * 关闭使用实例弹窗
 */
function closeUsageExamplesModal() {
    ModalManager.close('usage-examples-modal');
}

/**
 * 生成与当前 Controller 地址绑定的一键安装命令。
 */
function buildSkillInstallCommand() {
    const insecureOption = window.location.protocol === 'https:' ? '-k ' : '';
    return `curl ${insecureOption}-fsSL "${window.location.origin}/api/system/skills/install.sh" | bash`;
}

/**
 * 复制一键安装/更新命令。浏览器不能代替目标 Linux 主机执行该命令。
 */
function copySkillInstallCommand() {
    copyText(buildSkillInstallCommand(), {
        successMsg: '✓ Skill 一键安装/更新命令已复制',
    });
}

/**
 * 下载 Skill ZIP 离线包（不执行安装和命令链接迁移）。
 */
async function downloadSkillsZip() {
    try {
        const response = await fetch('/api/system/skills');
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || '下载失败');
        }
        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        triggerDownload(url, 'gms-remote-test-skills.zip', true);
        showToast('离线包已下载；在线安装请使用“安装/更新命令”', 'success');
    } catch (e) {
        console.error('[downloadSkillsZip] Error:', e);
        showToast('下载失败：' + e.message, 'error');
    }
}

/**
 * 复制文本到剪贴板（统一函数）
 * @param {string} text - 要复制的文本
 * @param {Object} options - 配置选项 { addJq: boolean, successMsg: string, element: HTMLElement }
 */
function copyText(text, options = {}) {
    const {
        addJq = false,
        successMsg = '✓ 命令已复制到剪贴板',
        element = null
    } = options;
    const textToCopy = addJq ? text + ' | jq "."' : text;

    debugLog('[Copy] Copying text:', textToCopy);

    const onSuccess = () => {
        debugLog('[Copy] Success');
        showToast(successMsg, 'success');
        if (element) {
            const originalColor = element.style.color;
            element.style.color = 'var(--success-color)';
            setTimeout(() => {
                if (element) {
                    element.style.color = originalColor || 'var(--primary-color)';
                }
            }, 500);
        }
    };

    const doFallback = () => {
        try {
            const textArea = document.createElement('textarea');
            textArea.value = textToCopy;
            textArea.style.position = 'fixed';
            textArea.style.left = '-999999px';
            document.body.appendChild(textArea);
            textArea.select();
            const successful = document.execCommand('copy');
            document.body.removeChild(textArea);
            if (successful) {
                onSuccess();
            } else {
                showToast('✗ 复制失败，请手动复制', 'error');
            }
        } catch (err) {
            console.error('[Copy] Fallback error:', err);
            showToast('✗ 复制失败：' + err.message, 'error');
        }
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(textToCopy).then(() => {
            onSuccess();
        }).catch(err => {
            console.error('[Copy] Clipboard API failed:', err);
            doFallback();
        });
    } else {
        doFallback();
    }
}

/**
 * 复制curl命令到剪贴板（自动添加jq格式化）
 */
window.copyCurlCommand = function(text) {
    copyText(text, { addJq: true, successMsg: '✓ curl命令已复制 (含jq格式化)' });
};

/**
 * 复制命令（使用示例专用）
 */
window.copyCommand = function(elementId) {
    const element = document.getElementById(elementId);
    if (!element) {
        console.error('[CopyCommand] Element not found:', elementId);
        showToast('✗ 找不到命令内容', 'error');
        return;
    }

    const text = element.textContent || element.innerText;
    debugLog('[CopyCommand] Copying from element:', elementId, text);

    copyText(text);
};

// 将API文档函数暴露到window对象
window.loadApiDocs = loadApiDocs;
window.filterApiDocs = filterApiDocs;
window.autoInstallSshd = autoInstallSshd;

/**
 * 复制 skill 命令到剪贴板
 */
window.copySkillCommand = function(element) {
    const text = element.textContent.trim();
    if (!text || text === '-') {
        showToast('✗ 无内容可复制', 'error');
        return;
    }
    copyText(text, {
        successMsg: '✓ 已复制：' + text,
        element: element
    });
};

/**
 * 复制文本到剪贴板（通用方法，用于 skill 命令等）
 * @param {string} text - 要复制的文本
 * @param {HTMLElement} element - 触发复制的元素
 */
window.copyToClipboard = function(text, element) {
    if (!text || text === '-') {
        showToast('✗ 无内容可复制', 'error');
        return;
    }
    copyText(text, {
        successMsg: '✓ 已复制：' + text,
        element: element
    });
};
