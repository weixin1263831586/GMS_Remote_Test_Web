// Daily Brief / 单号分析的诊断呈现辅助。
// 从 page.js 拆出（全局审查 P0：246KB 反弹顶穿 size ratchet，禁止抬
// ceiling，按 diagnosis 呈现域继续拆分回落）：诊断报告正文共用的
// 取证六态标签、证据门禁提示、系统权威元数据、证据质量提示。
// 依赖 page.js 的全局 esc（运行时调用，脚本加载顺序与既有辅助模块一致）。

// 单号分析与每日晨报行共用的实机取证六态标签。device_evidence_status 由
// Controller 端 _tool_status 从真实 trace 推导，非模型自报。
function dailyBriefEvidenceTag(evidenceStatus) {
  var status = String(evidenceStatus || '').trim();
  if (!status) return '';
  var text = ({
    succeeded: '实机取证成功',
    service_unavailable: '取证服务暂不可用',
    invalid_request: '取证请求无效',
    device_unavailable: '设备取证未完成',
    unavailable: '实机取证失败',
    collecting: '实机取证中',
    not_collected: '未执行实机取证'
  })[status] || '未执行实机取证';
  var evidenceClass = status === 'succeeded' ? 'evidence-ok'
    : (['service_unavailable', 'invalid_request', 'device_unavailable', 'unavailable'].includes(status)
      ? 'evidence-failed' : 'evidence-pending');
  return '<span class="' + evidenceClass + '">' + esc(text) + '</span>';
}

function dailyBriefEvidenceGateNotice(result) {
  var value = result && typeof result === 'object' ? result : {};
  if (!value.needs_human_review) return '';
  var gate = value.evidence_gate && typeof value.evidence_gate === 'object'
    ? value.evidence_gate : {};
  var findings = [];
  if (gate.attachments_checked === false) findings.push('附件尚未全部读取或校验');
  if (gate.history_search_required !== false && gate.history_checked !== true) {
    findings.push('相似历史工单检索未达到要求');
  }
  if (gate.source_evidence_required === true && gate.source_evidence_checked !== true) {
    findings.push('测试失败缺少可追溯的源码级证据');
  }
  if (!findings.length) findings.push('运行时证据校验尚未闭环');
  // 单行紧凑条:findings 内联,标题徽章已含「取证未闭环」,不再 <ul> 大块铺陈。
  return '<div class="daily-brief-warning daily-brief-gate-notice"><b>⚠️ 取证未闭环 · 仅供人工复核</b>'
    + '<span class="daily-brief-gate-findings">' + esc(findings.join('；')) + '</span></div>';
}

function dailyBriefSystemMetadata(result, extraChips) {
  var meta = result && result.report_metadata;
  var chips = String(extraChips || '');
  if (!meta || typeof meta !== 'object') {
    // 无元数据也要让质量徽章有处安放(与正文同一视觉行)。
    return chips ? '<div class="daily-brief-section"><div class="daily-brief-section-body daily-brief-meta-row">' + chips + '</div></div>' : '';
  }
  var source = meta.source === 'redmine_live' ? 'Redmine 实时校验' : '分析快照';
  var fields = [
    ['报告人', meta.author_name], ['当前负责人', meta.assigned_to_name],
    ['状态', meta.status_name], ['Redmine 更新时间', meta.updated_on],
    ['系统校验时间', meta.verified_at], ['数据来源', source]
  ].filter(function (item) { return String(item[1] || '').trim(); });
  return '<div class="daily-brief-section"><div class="daily-brief-section-body daily-brief-meta-row">'
    + fields.map(function (item) { return '<span class="daily-brief-meta-item"><b>'
      + esc(item[0]) + '</b> ' + esc(item[1]) + '</span>'; }).join('')
    + chips + '</div></div>';
}

function dailyBriefQualityChip(result) {
  var quality = String((result || {}).evidence_quality || '');
  if (!quality) return '';
  var label = ({ verified: '证据已验证', partial: '证据部分闭环', insufficient: '证据不足' })[quality] || quality;
  var cls = ({ verified: 'quality-verified', partial: 'quality-partial', insufficient: 'quality-insufficient' })[quality] || 'quality-partial';
  return '<span class="daily-brief-quality-chip ' + cls + '">' + esc(label) + '</span>';
}

function singleIssueAnalysisMeta(run, issue) {
  var timestamp = String(run.finished_at || run.started_at || '').replace('T', ' ').slice(0, 16);
  var tags = timestamp ? ['<span>分析时间 ' + esc(timestamp) + '</span>'] : [];
  var serial = String(run.device_serial || '').trim();
  if (serial) {
    tags.push('<span>设备 ' + esc(serial) + '</span>');
    tags.push(dailyBriefEvidenceTag(((issue.ai_execution || {}).device_evidence_status) || 'not_collected'));
  }
  return tags.join('');
}
