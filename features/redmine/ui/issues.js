// Redmine issues; loaded by page.html in dependency order.
// ---- Smart search: detect issue ID and fetch from Redmine ----
async function smartSearch() {
  var q = document.getElementById('searchInput').value.trim();
  if (!q) return loadIssues();
  // Detect issue ID pattern: #634227, 634227, or pure number
  var idMatch = q.match(/^#?(\d{4,})$/);
  if (idMatch) {
    var issueId = parseInt(idMatch[1]);
    // Check local DB first
    try {
      var local = await api('/api/redmine-agent/issues/' + issueId);
      if (local && local.issue_id) {
        return loadIssues();
      }
    } catch (_) {
      // Not found locally — fetch from Redmine
    }
    await fetchIssueFromRedmine(issueId);
  } else {
    return loadIssues();
  }
}

async function fetchIssueFromRedmine(issueId) {
  var btn = document.getElementById('scanBtn');
  var origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ 拉取 #' + issueId + '...';
  try {
    var result = await api('/api/redmine-agent/issues/' + issueId + '/fetch', {method: 'POST'});
    if (result.action === 'exists') {
      document.getElementById('searchInput').value = '';
      return loadIssues();
    }
    // Wait for analysis to complete
    btn.textContent = '⏳ 分析 #' + issueId + '...';
    await waitForRun(result.run_id, '拉取');
    document.getElementById('searchInput').value = '';
    return loadIssues();
  } catch (e) {
    notifyUser('拉取工单失败', e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

// ---- Issues list ----
async function loadIssues(page) {
  if (page) currentPage = page;
  const search = document.getElementById('searchInput').value.trim();
  const status = document.getElementById('statusFilter').value;
  const priority = document.getElementById('priorityFilter').value;
  const offset = (currentPage - 1) * pageSize;
  let url = `/api/redmine-agent/issues?limit=${pageSize}&offset=${offset}`;
  if (search) url += `&search=${encodeURIComponent(search)}`;
  if (status) url += `&status=${encodeURIComponent(status)}`;
  if (priority) url += `&priority=${encodeURIComponent(priority)}`;
  try {
    const data = await api(url);
    renderIssuesList(data.items || []);
    renderPagination(data.total || 0, data.limit, data.offset);
  } catch (e) {
    document.getElementById('issuesList').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

function renderIssuesList(issues) {
  const box = document.getElementById('issuesList');
  if (!issues.length) {
    box.innerHTML = '<div class="muted" style="padding:20px">暂无工单数据。请展开上方「高级同步」，使用「全量同步」拉取所有指派给你的 Redmine 工单。</div>';
    return;
  }
  box.innerHTML = issues.map(renderIssueCard).join('');
  if (pendingWorkspaceIssueId) {
    const card = box.querySelector(`.issue-card[data-issue-id="${CSS.escape(pendingWorkspaceIssueId)}"]`);
    if (card) setTimeout(function() { card.scrollIntoView({behavior: 'smooth', block: 'start'}); }, 0);
  }
}

function renderIssueCard(item) {
  const refs = item.references_json || [];
  const failures = item.failures_json || [];
  const ai = item.ai_json || {};
  const attachments = item.attachment_links || [];
  // 缓存原始富数据，供「存为Wiki」按钮按 issue_id 取回（避免序列化复杂对象进 onclick）
  if (item.issue_id) {
    window.__issueCardCache = window.__issueCardCache || {};
    window.__issueCardCache[String(item.issue_id)] = item;
  }

  // Extract seven fields
  const title = esc(item.subject || ai.title || '-');
  const problemDesc = _buildProblemDescription(item, attachments);
  const errorInfoRaw = item.error_info || _extractErrorHtml(failures) || '-';
  const errorAnalysis = item.error_analysis || ai.root_cause_guess || '-';
  const solutionRaw = item.solution || ai.solution || '-';
  const patchRaw = item.patch_direction || ai.patch_direction || '-';
  const attachmentLinks = attachments;
  const hasPatch = patchRaw && patchRaw !== '-' && patchRaw !== '需要进一步分析具体日志和源码'
    && !String(patchRaw).includes('未从现有证据中提取到明确补丁')
    && !String(patchRaw).includes('当前缺少可定位补丁');

  const statusClass = ['已关闭','Closed','已解决','Resolved'].includes(item.status_name) ? 'ok' :
                      ['紧急','Urgent'].includes(item.priority_name) ? 'high' :
                      ['高','High'].includes(item.priority_name) ? 'medium' : '';

  // 将测试模块、用例和错误堆栈合并为代码块。
  var errorInfoCombined = errorInfoRaw;
  if (failures && failures.length) {
    var f0 = failures[0];
    var header = '';
    if (f0.module) header += '测试模块: ' + f0.module + _NL;
    if (f0.name) header += '测试用例: ' + f0.name + _NL;
    if (header) header += _NL;
    // Prepend test info before the error code block
    // If errorInfoRaw starts with ```, insert after the opening fence
    if (errorInfoRaw.startsWith(_F3) || errorInfoRaw.startsWith(_BT+_BT+_BT)) {
      // Find the first newline after ```
      var nlIdx = errorInfoRaw.indexOf(_NL);
      if (nlIdx > 0) {
        errorInfoCombined = errorInfoRaw.substring(0, nlIdx + 1) + header + errorInfoRaw.substring(nlIdx + 1);
      } else {
        errorInfoCombined = header + errorInfoRaw;
      }
    } else {
      errorInfoCombined = header + errorInfoRaw;
    }
  }

  // Build references HTML — full display, no truncation
  let refsHtml = '';
  if (refs.length) {
    refsHtml = '<div class="ref-card-list">' + refs.map(renderReferenceCard).join('') + '</div>';
  } else {
    refsHtml = '<div class="muted">暂无参考单</div>';
  }

  // Detect issue type: GMS certification or SDK platform
  var issueType = 'SDK';
  var comp = (item.component || '').toUpperCase();
  var cat = (item.category || '').toUpperCase();
  var fv = (item.fixed_version || '').toUpperCase();
  if (comp.includes('GMS') || cat.includes('GMS') || fv.includes('GMS')) issueType = 'GMS';

  // Detect status display
  var statusName = item.status_name || '-';
  var statusIcon = '';
  if (['已关闭','Closed'].includes(statusName)) statusIcon = '✅ ';
  else if (['已解决','Resolved'].includes(statusName)) statusIcon = '✓ ';
  else if (['新建','New'].includes(statusName)) statusIcon = '🆕 ';
  var isClosed = ['已关闭','Closed','已解决','Resolved'].includes(statusName);

  return `<div class="issue-card" data-issue-id="${esc(item.issue_id)}">
    <h3>
      ${renderRedmineIssueLink(item.issue_id, {stopPropagation: false})}
      <span>${title}</span>
      <span style="margin-left:auto;font-size:12px;color:var(--muted)">${esc(item.priority_name || '-')}</span>
    </h3>

    <div class="field-label">📋 基本信息</div>
    <table class="info-table">
      <tr>
        <th>SoC</th><td><strong>${esc(item.soc_platform || '-')}</strong></td>
        <th>Android</th><td><strong>${esc(item.android_version || '-')}</strong></td>
        <th>类型</th><td>${esc(issueType)}</td>
        <th>分类</th><td>${esc(item.category || '-')}</td>
        <th>状态</th><td>${statusIcon}${esc(statusName)}</td>
        <th>指派</th><td>${esc(item.assigned_to_name || '-')}</td>
        <th>创建</th><td>${esc((item.created_on || '-').slice(0, 10))}</td>
      </tr>
    </table>

    <div class="field">
      <div class="field-label">📝 问题描述</div>
      ${renderIssueRichText(problemDesc)}
    </div>

    <div class="field">
      <div class="field-label">🔴 报错信息</div>
      ${renderIssueRichText(errorInfoCombined, 'rich-field error-rich')}
    </div>

    <div class="field">
      <div class="field-label">🔍 报错分析</div>
      ${renderIssueRichText(errorAnalysis)}
    </div>

    <div class="field">
      <div class="field-label">✅ 解决方案</div>
      <div class="solution-section">${renderMarkdownDoc(solutionRaw)}</div>
    </div>

    <div class="field">
      <div class="field-label">📎 Redmine附件 / 补丁</div>
      ${renderAttachmentLinks(item.issue_id, attachmentLinks)}
    </div>

    ${hasPatch ? `<div class="field">
      <div class="field-label">🔧 解决补丁</div>
      ${renderIssueRichText(patchRaw)}
    </div>` : ''}

    ${refs.length ? `<div class="field">
      <div class="field-label">📎 参考Redmine</div>
      ${refsHtml}
    </div>` : ''}

    <details class="issue-doc-details" data-toggle="loadIssueDocOnToggle" data-r0="el" data-a1="${item.issue_id}">
      <summary>📄 完整文档</summary>
      <div class="formatted-doc muted">展开后加载完整文档…</div>
    </details>

    <div class="knowledge-actions">
      <button class="ka-btn primary" data-click="agentReplyDraft" data-a0="${item.issue_id}" data-r1="el" title="复用报告分析风格生成 Redmine 回复草稿、根因和补丁方向">✉️ Redmine回复</button>
      <button class="ka-btn" data-click="refreshIssueMetadata" data-a0="${item.issue_id}" title="只刷新Redmine历史回复和附件元数据，不下载附件">🔄 刷新附件元数据</button>
      <button class="ka-btn" data-click="toggleIssueWorkbench" data-a0="${item.issue_id}" title="展开相似工单、历史回复和附件解析摘要">🧩 展开依据</button>
      <button class="ka-btn" data-click="saveIssueToWiki" data-a0="${item.issue_id}" title="把该工单存入 Wiki「Redmine问题沉淀」分类，并建立外链">📥 存为Wiki</button>
      <button class="ka-btn" data-click="navigateFromRedmineIssue" data-a0="reports" data-a1="${item.issue_id}" title="保留工单上下文并打开测试报告">📊 关联报告</button>
      <button class="ka-btn" data-click="navigateFromRedmineIssue" data-a0="automation" data-a1="${item.issue_id}" title="保留工单上下文并打开 GMS ATS">⚙️ 关联 ATS</button>
    </div>
    <div id="issue-workbench-${item.issue_id}" class="issue-workbench" style="display:none"></div>
  </div>`;
}

function renderReferenceCard(r) {
  const level = r.similarity_level || 'low';
  const score = Number(r.score || 0).toFixed(0);
  const levelText = level === 'high' ? '高' : level === 'medium' ? '中' : '低';
  return `<div class="ref-card">
    <div class="ref-item">
      ${renderRedmineIssueLink(r.issue_id, {stopPropagation: false})}
      <span class="ref-badge ${level}">${levelText} ${score}</span>
      <span class="ref-title">${esc(r.subject || '')}</span>
    </div>
  </div>`;
}

async function saveIssueToWiki(issueId) {
  const item = (window.__issueCardCache || {})[String(issueId)];
  if (!item) {
    notifyUser('数据缺失', '未找到该工单的富化数据，请重新打开工单后再试', 'error');
    return;
  }
  const subject = item.subject || ('Redmine #' + issueId);
  const module = item.module || '';
  const parts = [];
  parts.push('# ' + subject);
  parts.push('');
  parts.push('- **Redmine Issue**: #' + issueId);
  if (module) parts.push('- **模块**: ' + module);
  if (item.priority) parts.push('- **优先级**: ' + item.priority);
  if (item.status) parts.push('- **状态**: ' + item.status);
  parts.push('');
  const problemDesc = _buildProblemDescription(item, item.attachment_links || []);
  if (problemDesc && String(problemDesc).trim() && String(problemDesc).trim() !== '-') {
    parts.push('## 问题描述');
    parts.push('');
    parts.push(String(problemDesc).trim());
    parts.push('');
  }
  const errorAnalysis = item.error_analysis || (item.ai_json || {}).root_cause_guess || '';
  if (errorAnalysis && String(errorAnalysis).trim() && String(errorAnalysis).trim() !== '-') {
    parts.push('## 错误分析 / 根因');
    parts.push('');
    parts.push(String(errorAnalysis).trim());
    parts.push('');
  }
  const solution = item.solution || (item.ai_json || {}).solution || '';
  if (solution && String(solution).trim() && String(solution).trim() !== '-') {
    parts.push('## 解决方案');
    parts.push('');
    parts.push(String(solution).trim());
    parts.push('');
  }
  const content = parts.join('\n');
  const payload = {
    content_md: content,
    space_id: 'issues',
    title: `Redmine #${issueId} ${item.subject || ''}`.trim(),
    source: 'redmine',
    tags: ['Redmine问题沉淀'].concat(module ? [module] : []),
    links: [
      {target_type: 'redmine_issue', target_id: String(issueId), title: '#' + String(issueId)},
      ...(module ? [{target_type: 'test_case', target_id: module, title: module}] : [])
    ]
  };
  try {
    await api('/api/knowledge/docs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    notifyUser('已存为Wiki', '已存入「Redmine问题沉淀」并关联 Redmine #' + issueId, 'success');
  } catch (e) {
    notifyUser('存为Wiki失败', (e && e.message) || String(e), 'error');
  }
}

function _buildProblemDescription(item, attachments) {
  const parts = [];
  const desc = item.problem_description || item.description || '';
  if (desc && String(desc).trim() && String(desc).trim() !== '-') parts.push(String(desc).trim());
  const attachmentNotes = (attachments || []).map(function(a) {
    const analysis = a.analysis_json || {};
    const details = analysis.details || {};
    const excerpt = analysis.text_excerpt || details.ocr_text || '';
    const detected = details.detected_errors || [];
    if (!excerpt && !detected.length) return '';
    var lines = [`**附件**: ${a.filename || '-'}`];
    if (detected.length) lines.push(`检测到: ${detected.join(' / ')}`);
    if (excerpt) lines.push(excerpt.trim());
    return lines.join('\n');
  }).filter(Boolean);
  if (attachmentNotes.length) {
    parts.push('');
    parts.push('**附件分析**: ');
    parts.push(attachmentNotes.join('\n\n'));
  }
  return parts.join('\n') || '-';
}

function _extractErrorHtml(failures) {
  if (!failures || !failures.length) return '';
  return failures.slice(0, 3).map(f => `[${f.module || '-'}] ${f.name || '-'}: ${trunc(f.reason || '', 200)}`).join(_NL);
}

function renderAttachmentLinks(issueId, attachments) {
  const items = attachments || [];
  if (!items.length) {
    return `<div class="muted">本地暂无附件元数据。可点击“刷新附件元数据”从 Redmine 拉取附件名；或直接打开 <a href="${redmineIssueAttachmentsUrl(issueId)}" target="_blank">Redmine 附件区</a>。</div>`;
  }
  return `<div class="attachment-link-list">${items.map(a => {
    const name = String(a.filename || '');
    const lower = name.toLowerCase();
    const kind = lower.endsWith('.diff') || lower.endsWith('.patch') ? '补丁'
      : (/\.(png|jpg|jpeg|webp|bmp)$/i.test(lower) ? '截图' : '报告');
    const patchDir = kind === '补丁' ? '/vendor/rockchip/modules/power_ext' : '';
    // 优先使用同源代理下载，缺少附件 ID 时打开 Redmine 链接。
    const attId = a.attachment_id || a.id || '';
    const safeName = esc(name || '-');
    const linkHtml = attId
      ? `<a href="/api/redmine-agent/issues/${issueId}/attachments/${encodeURIComponent(attId)}/download" download="${esc(name)}" title="直接下载(不跳转)">${safeName}</a>`
      : `<a href="${esc(a.url || redmineIssueAttachmentsUrl(issueId))}" target="_blank">${safeName}</a>`;
    return `<div class="attachment-link-item">
      <span class="attachment-kind">${esc(kind)}</span>
      ${linkHtml}
      ${patchDir ? `<span class="patch-dir">应用目录：${esc(patchDir)}</span>` : ''}
    </div>`;
  }).join('')}</div>`;
}

async function loadIssueDocOnToggle(details, issueId) {
  if (!details || !details.open || details.dataset.loaded === '1') return;
  const box = details.querySelector('.formatted-doc');
  if (!box) return;
  box.innerHTML = '<div class="muted">正在加载完整文档…</div>';
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/document`);
    const text = data && data.doc_content ? data.doc_content : (data || '');
    box.classList.remove('muted');
    box.innerHTML = renderMarkdownDoc(String(text || '-'));
    details.dataset.loaded = '1';
  } catch (e) {
    box.innerHTML = `<div class="muted">完整文档加载失败: ${esc(e.message)}</div>`;
  }
}

async function refreshIssueMetadata(issueId) {
  try {
    notifyUser('正在刷新', `#${issueId} 附件和历史回复元数据`);
    await api(`/api/redmine-agent/issues/${issueId}/metadata`, {method:'POST'});
    await loadIssues(currentPage);
    notifyUser('已刷新', `#${issueId} 元数据已更新`);
  } catch (e) {
    notifyUser('刷新失败', e.message, 'error');
  }
}

function renderPagination(total, limit, offset) {
  const box = document.getElementById('issuesPagination');
  const pages = Math.ceil(total / limit);
  const current = Math.floor(offset / limit) + 1;
  if (pages <= 1) { box.innerHTML = `<div class="muted">共 ${total} 条</div>`; return; }

  // 页码窗口包含首页、末页和当前页前后两页。
  function pageWindow() {
    const span = 2;            // pages either side of current
    const win = new Set([1, pages, current]);
    for (let p = current - span; p <= current + span; p++) {
      if (p > 1 && p < pages) win.add(p);
    }
    const sorted = Array.from(win).filter(p => p >= 1 && p <= pages).sort((a, b) => a - b);
    const out = [];
    for (let i = 0; i < sorted.length; i++) {
      if (i > 0 && sorted[i] - sorted[i - 1] > 1) out.push('…');
      out.push(sorted[i]);
    }
    return out;
  }

  const numBtn = (p, label) => {
    const active = p === current;
    return `<button class="page-num${active ? ' active' : ''}"${active ? ' disabled' : ''} data-click="loadIssues" data-a0="${p}">${label}</button>`;
  };

  let html = `<button data-click="loadIssues" data-a0="1"${current === 1 ? ' disabled' : ''}>首页</button>`;
  html += `<button data-click="loadIssues" data-a0="${current-1}"${current === 1 ? ' disabled' : ''}>上一页</button>`;
  for (const p of pageWindow()) {
    if (p === '…') html += `<span class="muted" style="line-height:32px">…</span>`;
    else html += numBtn(p, p);
  }
  html += `<button data-click="loadIssues" data-a0="${current+1}"${current === pages ? ' disabled' : ''}>下一页</button>`;
  html += `<button data-click="loadIssues" data-a0="${pages}"${current === pages ? ' disabled' : ''}>末页</button>`;
  html += `<span class="muted" style="line-height:32px">第 ${current}/${pages} 页 (共${total}条)</span>`;
  box.innerHTML = html;
}

// ---- Runs ----
async function loadRuns() {
  try {
    const data = await api('/api/redmine-agent/runs?limit=30');
    const items = data.items || [];
    const box = document.getElementById('runsList');
    box.innerHTML = items.map(run => `
      <div class="run-item ${run.run_id === currentRunId ? 'active' : ''}" data-click="loadRun" data-a0="${esc(run.run_id)}">
        <div class="run-item-title">${esc(run.started_at || run.run_id)}</div>
        <div class="run-item-meta">${esc(run.status)} | mode=${esc(run.mode)} | issues ${run.issue_count || 0} | done ${run.processed_count || 0}</div>
      </div>`).join('') || '<div class="muted" style="padding:12px">暂无扫描记录</div>';
    if (!currentRunId && items.length) loadRun(items[0].run_id);
  } catch (e) {
    document.getElementById('runsList').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

async function loadRun(runId) {
  currentRunId = runId;
  try {
    const data = await api('/api/redmine-agent/runs/' + encodeURIComponent(runId));
    document.getElementById('runDetailTitle').textContent = '日报详情 ' + runId;
    const issues = data.issues || [];
    document.getElementById('runDetail').innerHTML = `
      <div class="muted">状态: ${esc(data.run.status)} | 报告: ${esc(data.run.report_path || '-')}</div>
      <div style="height:10px"></div>
      ${issues.map(renderIssueCard).join('') || '<div class="muted">没有扫描到问题。</div>'}`;
    loadRuns();
  } catch (e) {
    document.getElementById('runDetail').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}
