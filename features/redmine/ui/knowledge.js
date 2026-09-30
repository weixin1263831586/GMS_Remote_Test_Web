// Redmine knowledge; loaded by page.html in dependency order.
// 知识库：成熟案例、批量导入、案例分析和回复草稿。
let currentCaseOffset = 0;
const casePageSize = 30;
let pendingReferenceIssueId = 0;
let pendingInternalSource = null; // {type:'issue'|'case', id}
const REDMINE_CASE_VIEW_STORAGE_KEY = 'redmineCaseView';
const REDMINE_CASE_VIEWS = new Set(['facts', 'cases']);
let caseView = new URLSearchParams(window.location.search).get('case_view') || '';
if (!REDMINE_CASE_VIEWS.has(caseView)) {
  try { caseView = window.sessionStorage.getItem(REDMINE_CASE_VIEW_STORAGE_KEY) || ''; } catch(_) { caseView = ''; }
}
if (!REDMINE_CASE_VIEWS.has(caseView)) caseView = 'facts';

function switchCaseView(view, {persist = true, load = true} = {}) {
  caseView = REDMINE_CASE_VIEWS.has(view) ? view : 'facts';
  document.getElementById('viewBtnFacts').classList.toggle('active', caseView === 'facts');
  document.getElementById('viewBtnCases').classList.toggle('active', caseView === 'cases');
  if (persist) {
    try { window.sessionStorage.setItem(REDMINE_CASE_VIEW_STORAGE_KEY, caseView); } catch(_) {}
    var url = new URL(window.location.href);
    if (caseView === 'facts') url.searchParams.delete('case_view');
    else url.searchParams.set('case_view', caseView);
    window.history.replaceState({}, '', url.toString());
  }
  currentCaseOffset = 0;
  if (load) loadCases();
}

async function loadCases() {
  const search = (document.getElementById('caseSearchInput') || {}).value || '';
  try {
    if (caseView === 'facts') {
      const data = await api(`/api/redmine-agent/cases?limit=${casePageSize}&offset=${currentCaseOffset}&search=${encodeURIComponent(search)}`);
      renderFactsList(data.items || [], data.total || 0);
    } else {
      const data = await api(`/api/redmine-agent/mature-cases?limit=${casePageSize}&offset=${currentCaseOffset}&search=${encodeURIComponent(search)}`);
      renderCasesList(data.items || [], data.total || 0);
    }
  } catch (e) {
    const box = document.getElementById('casesList');
    if (box) box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

function renderFactsList(items, total) {
  const box = document.getElementById('casesList');
  if (!items.length) {
    box.innerHTML = '<div class="muted" style="padding:14px">暂无已导入的工单。点击顶部「📚 导入」粘贴工单号,或导入最近20个我的指派。</div>';
    return;
  }
  box.innerHTML = items.map(f => {
    const sig = f.error_signature ? `<span class="case-sig">${esc(f.error_signature)}</span>` : '';
    const scope = [f.chip_platform, f.android_version, f.certification_type, f.module].filter(Boolean).map(esc).join(' / ');
    const conf = f.confidence ? `<span class="muted" style="float:right">置信度 ${f.confidence}</span>` : '';
    return `<div class="case-card" data-click="showCaseFact" data-a0="${f.issue_id}">
      <div class="case-head"><span class="case-status draft">${renderRedmineIssueLink(f.issue_id)}</span>${sig}${conf}</div>
      <div class="case-title">${esc(f.subject || '-')}</div>
      <div class="case-scope muted">${scope || '-'}</div>
      <div class="case-sources muted">${esc(f.problem_summary || '').slice(0,80)}</div>
    </div>`;
  }).join('') + `<div class="muted" style="padding:8px">共 ${total} 条工单事实</div>`;
}

function renderCasesList(items, total) {
  const box = document.getElementById('casesList');
  if (!items.length) {
    box.innerHTML = '<div class="muted" style="padding:14px">暂无成熟案例。请先点击顶部「📚 导入」结构化历史工单,再选中若干案例构建成熟案例。</div>';
    return;
  }
  box.innerHTML = items.map(c => {
    const status = c.status || 'draft';
    const badge = status === 'approved' ? '✅已审核' : status === 'draft' ? '📝草稿' : esc(status);
    const sig = c.canonical_error_signature ? `<span class="case-sig">${esc(c.canonical_error_signature)}</span>` : '';
    const scope = [c.chip_platform, c.android_version, c.certification_type, c.module].filter(Boolean).map(esc).join(' / ');
    return `<div class="case-card" data-click="showCaseDetail" data-a0="${c.case_id}">
      <div class="case-head"><span class="case-status ${status}">${badge}</span>${sig}</div>
      <div class="case-title">${esc(c.title || '-')}</div>
      <div class="case-scope muted">${scope || '-'}</div>
      <div class="case-sources muted">来源: ${renderRedmineIssueLinks(c.source_issue_ids_json || [])}</div>
    </div>`;
  }).join('') + `<div class="muted" style="padding:8px">共 ${total} 个案例</div>`;
}

async function showCaseDetail(caseId) {
  document.getElementById('caseDetailTitle').textContent = '案例 #' + caseId;
  const box = document.getElementById('caseDetail');
  box.innerHTML = '<div class="muted">加载中…</div>';
  try {
    const c = await api(`/api/redmine-agent/mature-cases/${caseId}`);
    const sol = c.solution_json || {};
    const rules = c.rules_json || [];
    const sources = c.source_issue_ids_json || [];
    box.innerHTML = `
      <div class="field"><div class="field-label">标题</div><div class="field-content">${esc(c.title||'-')}</div></div>
      <div class="field"><div class="field-label">适用范围</div><div class="field-content">${esc([c.chip_platform,c.android_version,c.certification_type,c.module].filter(Boolean).join(' / ')||'-')}</div></div>
      <div class="field"><div class="field-label">问题摘要</div><div class="field-content">${esc(c.problem_summary||'-')}</div></div>
      <div class="field"><div class="field-label">根因</div><div class="field-content">${esc(c.root_cause||'-')}</div></div>
      <div class="field"><div class="field-label">解决方案</div><div class="field-content">${renderFormattedContent(sol.overview||'-','field-content')}</div></div>
      ${rules.length?`<div class="field"><div class="field-label">经验规则</div><div class="field-content">${rules.map(r=>esc((r.title||'')+(r.content?': '+r.content:''))).join('<br>')}</div></div>`:''}
      <div class="field"><div class="field-label">来源工单</div><div class="field-content">${renderRedmineIssueLinks(sources)}</div></div>
      <div class="case-actions">
        ${c.status!=='approved'?`<button data-click="approveCase" data-a0="${caseId}">✅ 审核通过</button>`:''}
        <button class="secondary" data-click="draftReply" data-a0="${sources[0]||0}" data-a1="${caseId}">✉️ 生成回复</button>
        <button class="secondary" data-click="startCreateInternalCase" data-a0="${caseId}">📝 创建内部单</button>
      </div>`;
  } catch (e) { box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`; }
}

async function approveCase(caseId) {
  try { await api(`/api/redmine-agent/mature-cases/${caseId}/approve`, {method:'POST'}); notifyUser('已审核', '案例 #'+caseId+' 已标记为 approved'); loadCases(); showCaseDetail(caseId); }
  catch(e){ notifyUser('审核失败', e.message, 'error'); }
}

// ---- Batch import ----
function showBatchImportModal() {
  document.getElementById('batchImportIds').value = '';
  document.getElementById('batchImportResult').innerHTML = '';
  showModal('batchImportModal');
}

function _renderImportResult(data, resultBox) {
  const items = data.items || [];
  const done = items.filter(i => i.status === 'done' || i.status === 'exists').length;
  resultBox.innerHTML = `✅ 完成 ${done}/${items.length}${data.failed ? ` (失败 ${data.failed})` : ''}<br><span class="muted">${items.slice(0, 12).map(i => `${renderRedmineIssueLink(i.issue_id)}:${esc(i.status || '')}`).join('  ')}</span>`;
  notifyUser('批量导入完成', `成功 ${done}/${items.length}，已自动跳转「工单事实」查看`);
  hideModal('batchImportModal');
  switchCaseView('facts');
}

async function submitBatchImport() {
  const raw = document.getElementById('batchImportIds').value.trim();
  const reanalyze = document.getElementById('batchReanalyze').checked;
  const resultBox = document.getElementById('batchImportResult');
  if (!raw) { resultBox.innerHTML = '❌ 请先粘贴工单号,或点「导入最近20个」'; return; }
  resultBox.innerHTML = '⏳ 导入中...';
  try {
    const data = await api('/api/redmine-agent/issues/batch-import', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({issue_ids: raw, reanalyze})});
    _renderImportResult(data, resultBox);
  } catch(e){ resultBox.innerHTML = `❌ ${esc(e.message)}`; }
}

async function submitImportRecent(n) {
  const reanalyze = document.getElementById('batchReanalyze').checked;
  const resultBox = document.getElementById('batchImportResult');
  resultBox.innerHTML = `⏳ 导入最近 ${n} 个我的指派工单...`;
  try {
    const data = await api(`/api/redmine-agent/issues/import-recent?limit=${n}`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({assigned_like: '', reanalyze})});
    _renderImportResult(data, resultBox);
  } catch(e){ resultBox.innerHTML = `❌ ${esc(e.message)}`; }
}

// ---- Per-issue knowledge actions ----
async function analyzeIssueCase(issueId) {
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/analyze-case`, {method:'POST'});
    if (data.status==='done'||data.status==='exists') { notifyUser('已入库', `#${issueId} → 模块 ${data.module||'-'}`); showCaseFact(issueId); }
    else notifyUser('分析失败', data.error||data.status, 'error');
  } catch(e){ notifyUser('分析失败', e.message, 'error'); }
}

async function findSimilarCase(issueId) {
  openKnowledgeModal(`#${issueId} 相似工单`, '<div class="muted">检索中…</div>');
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/similar?limit=10`);
    const items = data.similar || [];
    if (!items.length) { setKnowledgeBody('<div class="muted">知识库暂无相似工单。请先批量导入历史单。</div>'); return; }
    setKnowledgeBody(items.map(s=>`<div class="ref-item">
      ${renderRedmineIssueLink(s.issue_id, {stopPropagation: false})}
      <span class="ref-badge ${s.similarity_level}">${s.similarity_level==='high'?'高':s.similarity_level==='medium'?'中':'低'} ${s.score}</span>
      <span class="ref-title">${esc(s.subject||'')}</span>
      <span class="muted">[${esc(s.module||'-')}${s.error_signature?'/'+esc(s.error_signature):''}]</span>
    </div>`).join(''));
  } catch(e){ setKnowledgeBody(`<div class="muted">失败: ${esc(e.message)}</div>`); }
}

async function toggleIssueWorkbench(issueId) {
  const panel = document.getElementById('issue-workbench-' + issueId);
  if (!panel) return;
  if (panel.style.display !== 'none') {
    panel.style.display = 'none';
    return;
  }
  panel.style.display = 'block';
  panel.innerHTML = '<div class="muted" style="padding:10px">正在汇总结构化事实、历史回复、附件证据和相似工单…</div>';
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/workbench?similar_limit=8`);
    panel.innerHTML = renderIssueWorkbench(data);
  } catch (e) {
    panel.innerHTML = `<div class="muted" style="padding:10px">知识面板加载失败: ${esc(e.message)}</div>`;
  }
}

function renderIssueWorkbench(data) {
  const fact = data.fact || {};
  const sections = data.gms_like_sections || {};
  const scope = sections.scope || {};
  const evidence = data.evidence || {};
  const mature = data.mature_case || null;
  const similar = data.similar || [];
  const attachments = evidence.attachment_summary || [];
  const replies = evidence.reply_summary || [];
  const failures = evidence.failure_summary || [];
  const symptoms = sections.symptoms || [];
  const rules = sections.rules || [];
  const sourceIds = sections.source_issue_ids || [];
  return `
    <div class="workbench-grid">
      <section class="workbench-block">
        <div class="workbench-title">GMS风格结构化结论</div>
        <div class="kv-line"><b>范围</b><span>${esc([scope.chip_platform, scope.android_version, scope.test_version, scope.module].filter(Boolean).join(' / ') || '-')}</span></div>
        <div class="kv-line"><b>错误签名</b><span>${esc(fact.error_signature || (mature ? mature.canonical_error_signature : '') || '-')}</span></div>
        <div class="kv-line"><b>问题现象</b><span>${renderListText(symptoms, '暂无结构化现象')}</span></div>
        <div class="kv-line"><b>根因</b><span>${renderFormattedContent(sections.root_cause || '-', 'field-content')}</span></div>
        <div class="kv-line"><b>解决方案</b><span>${renderFormattedContent(sections.solution || '-', 'field-content')}</span></div>
        <div class="kv-line"><b>验证方式</b><span>${esc(sections.verification || '-')}</span></div>
        ${rules.length ? `<div class="kv-line"><b>经验规则</b><span>${rules.map(r => esc((r.title || '') + (r.content ? ': ' + r.content : ''))).join('<br>')}</span></div>` : ''}
      </section>
      <section class="workbench-block">
        <div class="workbench-title">历史依据</div>
        ${mature ? `<div class="mature-hit">命中成熟案例 #${mature.case_id}: ${esc(mature.title || '')}</div>` : '<div class="muted">暂无成熟案例命中，可先从相似工单构建。</div>'}
        <div class="similar-list">${similar.length ? similar.map(s => `
          <div class="ref-item">
            ${renderRedmineIssueLink(s.issue_id, {stopPropagation: false})}
            <span class="ref-badge ${s.similarity_level || 'low'}">${esc(s.similarity_level || 'low')} ${s.score || 0}</span>
            <span class="ref-title">${esc(s.subject || '')}</span>
          </div>`).join('') : '<div class="muted">暂无相似历史工单。</div>'}</div>
        <div class="source-links">${sourceIds.length ? '来源: ' + renderRedmineIssueLinks(sourceIds, {stopPropagation: false}) : ''}</div>
      </section>
    </div>
    <div class="workbench-grid">
      <section class="workbench-block">
        <div class="workbench-title">附件 / 截图 / 日志证据</div>
        ${failures.length ? `<div class="mini-evidence-title">失败项</div>${failures.map(f => `<div class="evidence-line"><b>${esc(f.module || '-')}</b> ${esc(f.name || '')}<br><span>${esc(f.reason || '')}</span></div>`).join('')}` : ''}
        ${attachments.length ? attachments.map(a => renderAttachmentEvidence(a)).join('') : `<div class="muted">暂无本地解析结果；附件文件不存入内部知识库，请通过上方“Redmine附件 / 补丁”或 <a href="${redmineIssueAttachmentsUrl(data.issue_id)}" target="_blank">Redmine 附件区</a> 查看源文件。</div>`}
      </section>
      <section class="workbench-block">
        <div class="workbench-title">历史回复摘要</div>
        ${replies.length ? replies.map(r => `
          <div class="reply-line">
            <div><b>${esc(r.user || '-')}</b> <span class="muted">${esc(String(r.created_on || '').slice(0, 19))}</span></div>
            ${r.notes ? `<div>${esc(r.notes)}</div>` : ''}
            ${r.details && r.details.length ? `<div class="muted">${r.details.map(d => esc([d.name, d.old_value, d.new_value].filter(Boolean).join(' -> '))).join('<br>')}</div>` : ''}
          </div>`).join('') : '<div class="muted">暂无可汇总的历史回复。</div>'}
      </section>
    </div>`;
}

function renderListText(items, emptyText) {
  if (!items || !items.length) return esc(emptyText || '-');
  return '<ul class="compact-list">' + items.slice(0, 8).map(item => `<li>${esc(item)}</li>`).join('') + '</ul>';
}

function renderAttachmentEvidence(a) {
  const detected = a.detected_errors || [];
  const failures = a.failures || [];
  const type = a.type || a.content_type || '';
  return `<div class="attachment-evidence">
    <div><b>${esc(a.filename || '-')}</b> <span class="muted">${esc(type || a.status || '')}</span></div>
    ${detected.length ? `<div class="detected-errors">${detected.map(esc).join(' / ')} ${a.certification_type ? '(' + esc(a.certification_type) + ')' : ''}</div>` : ''}
    ${failures.length ? failures.map(f => `<div class="evidence-line"><b>${esc(f.module || '-')}</b> ${esc(f.name || '')}<br><span>${esc(f.reason || '')}</span></div>`).join('') : ''}
    ${a.text_excerpt ? `<details><summary>OCR/文本摘录</summary><pre>${esc(a.text_excerpt)}</pre></details>` : ''}
  </div>`;
}

async function draftReply(issueId, matureCaseId) {
  openKnowledgeModal(`✉️ 回复草稿 #${issueId}`, '<div class="muted">生成中…</div>');
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/draft-reply`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(matureCaseId ? {mature_case_id: matureCaseId} : {})});
    const body = `<div class="muted" style="margin-bottom:6px">Redmine ${renderRedmineIssueLink(issueId, {stopPropagation: false})} · 来源: ${data.source==='mature_case'?'成熟案例 #'+(data.mature_case_id||''):'相似工单'} · 模块 ${esc(data.module||'-')} ${data.error_signature?'/ '+esc(data.error_signature):''}</div>
      <textarea id="replyDraftArea" rows="14" style="width:100%;font-family:monospace">${esc(data.reply_draft||'')}</textarea>
      <div style="margin-top:8px"><button data-click="copyReplyDraft" data-r0="el">📋 复制</button></div>`;
    setKnowledgeBody(body);
  } catch(e){ setKnowledgeBody(`<div class="muted">失败: ${esc(e.message)}</div>`); }
}

async function draftAgentReply(issueId, matureCaseId) {
  openKnowledgeModal(`🤖 AI+知识库回复 #${issueId}`, '<div class="muted">正在拉取/分析工单并生成回复，可能需要几十秒…</div>');
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/agent-reply`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(matureCaseId ? {mature_case_id: matureCaseId} : {})
    });
    const body = `<div class="muted" style="margin-bottom:6px">Redmine ${renderRedmineIssueLink(issueId, {stopPropagation: false})} · 来源: ${esc(data.source || '-')} · 模块 ${esc(data.module||'-')} ${data.error_signature?'/ '+esc(data.error_signature):''}</div>
      ${data.patch_direction ? `<div class="field"><div class="field-label">补丁方向</div>${renderFormattedContent(data.patch_direction, 'field-content')}</div>` : ''}
      <textarea id="replyDraftArea" rows="16" style="width:100%;font-family:monospace">${esc(data.reply_draft||'')}</textarea>
      <div style="margin-top:8px"><button data-click="copyReplyDraft" data-r0="el">📋 复制</button></div>`;
    setKnowledgeBody(body);
  } catch(e){ setKnowledgeBody(`<div class="muted">失败: ${esc(e.message)}</div>`); }
}

function copyReplyDraft(btn) {
  const area = document.getElementById('replyDraftArea');
  if (!area) return;
  navigator.clipboard.writeText(area.value).then(function(){ notifyUser('已复制', '回复草稿已复制到剪贴板'); });
  if (btn) { var old = btn.textContent; btn.textContent = '✓'; setTimeout(function(){ btn.textContent = old; }, 1500); }
}

async function sendReplyToRedmine(issueId, btn) {
  const area = document.getElementById('replyDraftArea');
  if (!area) { notifyUser('无回复内容', '请先生成回复草稿', 'error'); return; }
  const text = (area.value || '').trim();
  if (!text) { notifyUser('回复为空', '请先生成或编辑回复草稿', 'error'); return; }
  if (!await confirmUserAction(
    '发送 Redmine 回复',
    '确认将此回复发送到 Redmine #' + issueId + '？'
  )) return;
  if (btn) { btn.disabled = true; var old = btn.textContent; btn.textContent = '⏳ 发送中…'; }
  try {
    const data = await api('/api/redmine/reply', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({issue_id: String(issueId), reply_text: text}),
    });
    notifyUser('已发送', data && data.message ? data.message : '回复已发送到 Redmine #' + issueId);
    if (btn) { btn.textContent = '✓ 已发送'; }
  } catch (e) {
    notifyUser('发送失败', e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; setTimeout(function(){ btn.textContent = old; }, 2000); }
  }
}

async function agentReplyDraft(issueId, btn) {
  if (!issueId) return;
  if (btn) { btn.disabled = true; var oldBtn = btn.textContent; btn.textContent = '⏳ 生成中…'; }
  notifyUser('正在生成回复', '联网拉取工单详情、附件与历史回复，约 10-30s');
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/agent-reply`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({force: false}),
    });
    const srcMap = {mature_case: '成熟案例 #' + (data.mature_case_id || ''), ai_analysis: 'AI 分析（联网）', similar_issues: '相似历史工单'};
    const srcLabel = srcMap[data.source] || data.source;
    const sigLine = data.error_signature ? ' / ' + data.error_signature : '';
    const similarText = (data.similar_issues || []).length
      ? ' · 相似历史 ' + (data.similar_issues || []).map(function(s) { return renderRedmineIssueLink(s.issue_id, {stopPropagation: false}); }).join(' ')
      : '';
    const summaryHtml = '来源: ' + esc(srcLabel || '-') + ' · 模块 ' + esc(data.module || '-') + esc(sigLine) + similarText;
    openRedmineReplyModal(issueId, data.reply_draft || '', {summaryHtml: summaryHtml});
  } catch (e) {
    notifyUser('生成失败', e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = oldBtn; }
  }
}

function copyPatchDirection(btn) {
  const code = document.querySelector('#patchDirectionBlock code');
  if (!code) return;
  navigator.clipboard.writeText(code.textContent).then(function(){ notifyUser('已复制', '补丁方向已复制到剪贴板'); });
  if (btn) { var old = btn.textContent; btn.textContent = '✓'; setTimeout(function(){ btn.textContent = old; }, 1500); }
}

async function showCaseFact(issueId) {
  openKnowledgeModal(`📚 案例结构化 #${issueId}`, '<div class="muted">加载中…</div>');
  try {
    const f = await api(`/api/redmine-agent/cases/${issueId}`);
    setKnowledgeBody(`
      <div class="field"><div class="field-label">Redmine工单</div><div class="field-content">${renderRedmineIssueLink(issueId, {stopPropagation: false})}</div></div>
      <div class="field"><div class="field-label">平台/版本</div><div class="field-content">${esc(f.chip_platform||'-')} / ${esc(f.android_version||'-')} / ${esc(f.certification_type||'-')}</div></div>
      <div class="field"><div class="field-label">模块/错误签名</div><div class="field-content">${esc(f.module||'-')} / ${esc(f.error_signature||'-')} <span class="muted">(置信度 ${f.confidence||0})</span></div></div>
      <div class="field"><div class="field-label">问题摘要</div><div class="field-content">${esc(f.problem_summary||'-')}</div></div>
      <div class="field"><div class="field-label">根因</div><div class="field-content">${esc(f.root_cause||'-')}</div></div>
      <div class="field"><div class="field-label">解决方案</div><div class="field-content">${renderFormattedContent(f.solution||'-','field-content')}</div></div>
      <div class="case-actions">
        <button data-click="buildMatureFromIssue" data-a0="${issueId}">🏗️ 构建成熟案例</button>
        <button class="secondary" data-click="draftReply" data-a0="${issueId}">✉️ 生成回复</button>
      </div>`);
  } catch(e){ setKnowledgeBody(`<div class="muted">失败: ${esc(e.message)}</div>`); }
}

async function buildMatureFromIssue(issueId) {
  try {
    const data = await api('/api/redmine-agent/mature-cases/build', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({issue_ids: String(issueId)})});
    notifyUser('已构建', '成熟案例 #'+data.case_id); hideModal('knowledgeModal'); loadCases();
  } catch(e){ notifyUser('构建失败', e.message, 'error'); }
}

// ---- Reference output + evaluation ----
function openReferenceModal(issueId) { pendingReferenceIssueId = issueId; showModal('referenceModal'); }

async function submitReference() {
  const issueId = pendingReferenceIssueId;
  const payload = {
    source: document.getElementById('referenceSource').value,
    title: document.getElementById('referenceTitle').value,
    markdown: document.getElementById('referenceMarkdown').value,
  };
  try {
    await api(`/api/redmine-agent/issues/${issueId}/reference-output`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    hideModal('referenceModal');
    compareCase(issueId);
  } catch(e){ notifyUser('导入失败', e.message, 'error'); }
}

async function compareCase(issueId) {
  openKnowledgeModal(`⚖️ 质量对比 #${issueId}`, '<div class="muted">评测中…</div>');
  try {
    const data = await api(`/api/redmine-agent/issues/${issueId}/evaluate-case`, {method:'POST'});
    setKnowledgeBody(`
      <div class="field"><div class="field-label">Redmine工单</div><div class="field-content">${renderRedmineIssueLink(issueId, {stopPropagation: false})}</div></div>
      <div class="field"><div class="field-label">评分</div><div class="field-content" style="font-size:20px;font-weight:600">${data.score||0}/100</div></div>
      ${(data.missing_fields||[]).length?`<div class="field"><div class="field-label">缺失字段</div><div class="field-content">${data.missing_fields.map(esc).join(', ')}</div></div>`:''}
      ${(data.mismatch_fields||[]).length?`<div class="field"><div class="field-label">不一致字段</div><div class="field-content">${data.mismatch_fields.map(m=>`${esc(m.field)}: 内部=${esc(m.internal)} / 参考=${esc(m.reference)}`).join('<br>')}</div></div>`:''}
      ${(data.suggestions||[]).length?`<div class="field"><div class="field-label">优化建议</div><div class="field-content">${data.suggestions.map(esc).join('<br>')}</div></div>`:''}
      <div class="muted" style="margin-top:8px">注:参考输出仅用于评测,不参与自动回复。</div>`);
  } catch(e){ setKnowledgeBody(`<div class="muted">失败: ${esc(e.message)}</div>`); }
}

// ---- Internal issue creation ----
function startCreateInternal(issueId) { pendingInternalSource = {type:'issue', id: issueId}; showModal('internalCreateModal'); }
function startCreateInternalCase(caseId) { pendingInternalSource = {type:'case', id: caseId}; showModal('internalCreateModal'); }

async function confirmInternalCreate() {
  if (!pendingInternalSource) return;
  const payload = {
    project_id: document.getElementById('internalProjectId').value,
    tracker_id: parseInt(document.getElementById('internalTrackerId').value)||1,
    priority_id: parseInt(document.getElementById('internalPriorityId').value)||2,
    assigned_to_id: document.getElementById('internalAssignedId').value || null,
    confirmed: true,
  };
  const src = pendingInternalSource; pendingInternalSource = null;
  try {
    const url = src.type==='case' ? `/api/redmine-agent/mature-cases/${src.id}/create-internal` : `/api/redmine-agent/issues/${src.id}/create-internal`;
    const data = await api(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    hideModal('internalCreateModal');
    if (data.success) notifyUser('已创建', '内部工单 #'+data.internal_issue_id);
    else notifyUser('创建未完成', data.error||'请检查 Redmine 凭据/配置', 'warning');
  } catch(e){ notifyUser('创建失败', e.message, 'error'); }
}

// ---- Knowledge modal helpers ----
function openKnowledgeModal(title, body) {
  document.getElementById('knowledgeTitle').textContent = title;
  document.getElementById('knowledgeFooter').style.display = '';
  setKnowledgeBody(body);
  showModal('knowledgeModal');
}
function setKnowledgeBody(html) { document.getElementById('knowledgeBody').innerHTML = html; }
