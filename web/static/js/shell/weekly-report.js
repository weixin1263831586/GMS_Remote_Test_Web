// Shell 模块：周报总结（从 shell.html 内联脚本尾部提取）。
// ==================== 周报总结 ====================
// 周报正文 = 精炼版 (数字 + Top 主题清单)；详细流水单作为可折叠附录保留。
// 支持「我自己」(个人端点) 与「部门成员」(department 端点，单选成员) 两种归属。
let weeklyReportMarkdown = '';
let weeklyReportMembers = [];

function _isoDate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
}

function _defaultLastWeekRange() {
    // 上周一 → 上周日 (完整自然周)
    const today = new Date();
    const thisMonday = new Date(today);
    thisMonday.setDate(today.getDate() - ((today.getDay() + 6) % 7));
    const lastMonday = new Date(thisMonday);
    lastMonday.setDate(thisMonday.getDate() - 7);
    const lastSunday = new Date(lastMonday);
    lastSunday.setDate(lastMonday.getDate() + 6);
    return [_isoDate(lastMonday), _isoDate(lastSunday)];
}

function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function openWeeklyReport() {
    const [s, e] = _defaultLastWeekRange();
    const startInput = document.getElementById('weekly-report-start');
    const endInput = document.getElementById('weekly-report-end');
    if (startInput && !startInput.value) startInput.value = s;
    if (endInput && !endInput.value) endInput.value = e;
    ModalManager.open('weekly-report-modal');
    loadWeeklyReportMembers().then(() => generateWeeklyReport());
}

function closeWeeklyReportModal() {
    ModalManager.close('weekly-report-modal');
}

async function loadWeeklyReportMembers() {
    const sel = document.getElementById('weekly-report-member');
    if (!sel) return;
    const keep = sel.value;
    try {
        const resp = await fetch('/api/reports/weekly-report/department');
        const result = await resp.json().catch(() => ({ success: false }));
        if (!result.success) return;
        weeklyReportMembers = (result.data || {}).members || [];
        sel.innerHTML = weeklyReportMembers.map(m => `<option value="${_esc(m.owner)}">${_esc(m.name)}</option>`).join('');
        // 去掉「我自己」后，默认选第一个成员（若无成员则保留原样）
        if (!keep && weeklyReportMembers.length) sel.value = weeklyReportMembers[0].owner;
        else if (keep) sel.value = keep;
    } catch (e) { /* 静默：成员名单非必需，失败则只保留「我自己」 */ }
}

function onWeeklyReportMemberChange() {
    generateWeeklyReport();
}

// 把「我自己」与「部门成员」两种后端响应归一成统一形态：
// { range, name, redmine, gerrit, themes:{redmine,gerrit} }
function _normalizeReportData(result) {
    const d = result.data || {};
    if (d.member) {
        const m = d.member;
        return {
            range: d.range || {},
            name: m.name || m.owner || '成员',
            redmine: m.redmine || {},
            gerrit: m.gerrit || {},
            android17: m.android17 || {},
            gms_test: m.gms_test || {},
            themes: m.themes || { redmine: [], gerrit: [] },
            generated_at: d.generated_at,
        };
    }
    const rm = d.redmine || {};
    const ownerNames = (rm.owner_names || []);
    return {
        range: d.range || {},
        name: ownerNames.length ? ownerNames.join(' / ') : '当前用户',
        redmine: rm,
        gerrit: d.gerrit || {},
        android17: d.android17 || {},
        gms_test: d.gms_test || {},
        themes: d.themes || { redmine: [], gerrit: [] },
        generated_at: d.generated_at,
    };
}

async function generateWeeklyReport() {
    const start = (document.getElementById('weekly-report-start') || {}).value || '';
    const end = (document.getElementById('weekly-report-end') || {}).value || '';
    const owner = (document.getElementById('weekly-report-member') || {}).value || '';
    const content = document.getElementById('weekly-report-content');
    const status = document.getElementById('weekly-report-status');
    if (!start || !end) {
        if (status) status.textContent = '请选择起止日期';
        return;
    }
    if (content) content.innerHTML = '<div style="color: var(--text-secondary); padding: 12px;">正在生成周报...</div>';
    if (status) status.textContent = '';
    try {
        const base = `/api/reports/weekly-report`;
        const scopes = [
            `include_redmine=${document.getElementById('weekly-scope-redmine')?.checked ? 1 : 0}`,
            `include_gerrit=${document.getElementById('weekly-scope-gerrit')?.checked ? 1 : 0}`,
            `include_android17=${document.getElementById('weekly-scope-android17')?.checked ? 1 : 0}`,
            `include_gms_test=${document.getElementById('weekly-scope-gms-test')?.checked ? 1 : 0}`,
        ].join('&');
        const url = owner
            ? `${base}/department?owner=${encodeURIComponent(owner)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&${scopes}`
            : `${base}?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&${scopes}`;
        const resp = await fetch(url);
        const result = await resp.json().catch(() => ({ success: false }));
        if (!result.success) {
            // Server-provided error strings must be escaped before
            // innerHTML insertion (consistency with _esc elsewhere).
            const msg = _esc(result.error || result.message || '未知错误');
            if (content) content.innerHTML = `<div style="color: var(--danger-color); padding: 12px;">生成失败：${msg}</div>`;
            return;
        }
        const data = _normalizeReportData(result);
        window.__weeklyReportData = data;
        weeklyReportMarkdown = buildWeeklyReportMarkdown(data);
        if (content) content.innerHTML = renderWeeklyReportHtml(data);
        if (status) status.textContent = `已生成 · ${data.range.label || ''} (${data.range.start} ~ ${data.range.end})`;
        // 默认自动生成 AI 总结（静默：失败不弹 toast，显示错误条供重试）
        generateWeeklyReportAi(true);
    } catch (err) {
        const msg = _esc(err && err.message || err || '未知错误');
        if (content) content.innerHTML = `<div style="color: var(--danger-color); padding: 12px;">生成失败：${msg}</div>`;
    }
}

async function generateWeeklyReportAi(silent) {
    const d = window.__weeklyReportData;
    if (!d) { if (!silent) showToast('请先生成周报', 'warning'); return; }
    const btn = document.getElementById('weekly-report-ai-btn');
    const status = document.getElementById('weekly-report-status');
    if (btn) { btn.disabled = true; btn.textContent = '✨ 总结中...'; }
    if (status) status.textContent = 'AI 正在阅读工单详情并总结...';
    try {
        const owner = (document.getElementById('weekly-report-member') || {}).value || '';
        const resp = await fetch('/api/reports/weekly-report/ai-summary', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                start: (d.range || {}).start, end: (d.range || {}).end,
                owner, name: d.name || '',
                redmine: d.redmine, gerrit: d.gerrit,
                android17: d.android17, gms_test: d.gms_test,
            }),
        });
        const result = await resp.json().catch(() => ({ success: false }));
        if (!result.success) {
            if (!silent) showToast('AI 总结失败：' + (result.error || '未知错误'), 'error');
            if (status) status.textContent = 'AI 总结失败：' + (result.error || '未配置 AI') + '（可点「重新总结」重试）';
            renderWeeklyReportAiError(result.error || '未配置 AI');
            return;
        }
        d.aiSummary = (result.data || {}).summary || '';
        d.aiProvider = (result.data || {}).provider || '';
        weeklyReportMarkdown = buildWeeklyReportMarkdown(d);
        renderWeeklyReportAi(d);
        if (status) status.textContent = `AI 总结已生成（${d.aiProvider}，读取 ${result.data.issue_count} 个工单）`;
    } catch (err) {
        if (!silent) showToast('AI 总结失败：' + err.message, 'error');
        if (status) status.textContent = 'AI 总结失败（可点「重新总结」重试）';
        renderWeeklyReportAiError(err.message);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '✨ 重新总结'; }
    }
}

function renderWeeklyReportAiError(msg) {
    const content = document.getElementById('weekly-report-content');
    if (!content) return;
    let box = document.getElementById('weekly-report-ai');
    if (!box) {
        box = document.createElement('div');
        box.id = 'weekly-report-ai';
        content.insertBefore(box, content.firstChild);
    }
    box.innerHTML = `<div style="background: var(--card-bg); border:1px dashed var(--border-color); border-radius:8px; padding:10px 14px; margin-bottom:12px; color: var(--text-secondary); font-size:13px;">✨ AI 总结未生成：${_esc(msg || '未知原因')}。可点击「✨ 重新总结」重试。</div>`;
}

// 把 AI 总结渲染/插入到报告顶部（#weekly-report-ai 区块）
function renderWeeklyReportAi(d) {
    const content = document.getElementById('weekly-report-content');
    if (!content || !d.aiSummary) return;
    let box = document.getElementById('weekly-report-ai');
    if (!box) {
        box = document.createElement('div');
        box.id = 'weekly-report-ai';
        content.insertBefore(box, content.firstChild);
    }
    const html = _renderMarkdownLite(d.aiSummary);
    box.innerHTML = `
        <div style="background: linear-gradient(135deg, var(--primary-color, #2563eb), #7c3aed); color: white; border-radius:10px; padding:12px 16px; margin-bottom:14px;">
            <div style="font-weight:700; margin-bottom:8px; display:flex; align-items:center; gap:6px; font-size:14px;">✨ AI 周报总结 ${d.aiProvider ? `<span style="font-size:11px; opacity:0.8; font-weight:400;">· ${_esc(d.aiProvider)}</span>` : ''}</div>
            <div style="background: rgba(255,255,255,0.14); border-radius:8px; padding:12px 14px; line-height:1.75; font-size:13px;">${html}</div>
        </div>`;
}

// 轻量渲染标题、列表、粗体、代码和段落。
function _renderMarkdownLite(md) {
    const lines = String(md || '').split('\n');
    const out = [];
    let i = 0;
    const inline = (s) => _esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code style="background:rgba(0,0,0,0.15);padding:0 3px;border-radius:3px;">$1</code>');
    // 每两个前导空格表示一级；圆点和数字标记后的空格可选。
    const ITEM_RE = /^(\s*)(?:(?:-|\*)\s+|•\s*|\d+\.\s*)(.*)$/;
    const matchItem = (line) => {
        const m = ITEM_RE.exec(line);
        if (!m) return null;
        const indent = Math.floor((m[1] || '').length / 2); // 2 空格一级
        return { indent, text: m[2] };
    };
    while (i < lines.length) {
        const line = lines[i];
        const trimmed = line.trim();
        if (!trimmed) { i++; continue; }
        if (/^## /.test(trimmed)) {
            out.push(`<h4 style="margin:10px 0 4px; font-size:13.5px;">${inline(trimmed.slice(3))}</h4>`);
            i++; continue;
        }
        if (/^### /.test(trimmed)) {
            out.push(`<div style="margin:8px 0 2px; font-weight:700; font-size:13px;">${inline(trimmed.slice(4))}</div>`);
            i++; continue;
        }
        // 列表块：连续若干行，每行都符合（含缩进子项）。用缩进层级嵌套渲染。
        if (matchItem(line)) {
            const root = [];
            const buildLevel = (items) => {
                const ul = [];
                let idx = 0;
                while (idx < items.length) {
                    const cur = items[idx];
                    const children = [];
                    let j = idx + 1;
                    while (j < items.length && items[j].indent > cur.indent) {
                        children.push(items[j]);
                        j++;
                    }
                    const childHtml = children.length ? buildLevel(children) : '';
                    const pad = cur.indent * 16;
                    ul.push(`<li style="margin:3px 0; padding-left:${pad + 14}px; position:relative;"><span style="position:absolute;left:${pad}px;top:0;">•</span>${inline(cur.text)}${childHtml}</li>`);
                    idx = j;
                }
                return `<ul style="margin:3px 0 6px 4px; padding:0; list-style:none;">${ul.join('')}</ul>`;
            };
            const collected = [];
            while (i < lines.length) {
                const it = matchItem(lines[i]);
                if (it) { collected.push(it); i++; continue; }
                // 列表内允许空行（跳过），但遇到非列表非空行则结束。
                if (lines[i].trim() === '') { i++; continue; }
                break;
            }
            if (collected.length) out.push(buildLevel(collected));
            continue;
        }
        // 普通段落
        out.push(`<p style="margin:4px 0;">${inline(trimmed)}</p>`);
        i++;
    }
    return out.join('');
}

function _themesText(themes, max) {
    const arr = (themes || []).slice(0, max || 8);
    return arr.map(t => `${t.tag}×${t.count}`).join('、');
}

function _themesChips(themes, max) {
    const arr = (themes || []).slice(0, max || 8);
    if (!arr.length) return '<span style="color: var(--text-muted);">—</span>';
    return arr.map(t => `<span style="display:inline-block; background: var(--light-bg); border:1px solid var(--border-color); border-radius:10px; padding:1px 8px; margin:2px 4px 2px 0; font-size:12px;">${_esc(t.tag)} <b style="color: var(--primary-color);">${t.count}</b></span>`).join('');
}

function buildWeeklyReportMarkdown(data) {
    const r = data.range || {};
    const rm = data.redmine || {};
    const gr = data.gerrit || {};
    const a17 = data.android17 || {};
    const gt = data.gms_test || {};
    const themes = data.themes || {};
    const rmOk = rm.available !== false;
    const grOk = gr.available !== false;
    const a17Ok = a17.available === true;
    const gtOk = gt.available === true;
    const lines = [];
    lines.push(`# 周报 · ${data.name || ''}`);
    lines.push('');
    lines.push(`**周期**：${r.start} ~ ${r.end}（${r.label || '自定义'}）`);
    lines.push(`**生成时间**：${data.generated_at || ''}`);
    lines.push('');

    // AI 总结（若有，置于最前）
    if (data.aiSummary) {
        lines.push(`## ✨ 本周工作总结（AI）`);
        lines.push('');
        lines.push(data.aiSummary);
        lines.push('');
    }

    // 一句话总结（顺序：Redmine → Gerrit → GMS → Android17）
    const summaryParts = [];
    if (rmOk) summaryParts.push(`关闭 ${rm.resolved_this_period || 0} 个 Redmine 工单`);
    if (grOk) summaryParts.push(`合并 ${gr.merged_this_period || 0} 个 Gerrit 提交、新增 ${gr.new_this_period || 0} 个`);
    if (gtOk) summaryParts.push(`推进 ${gt.platform_count || 0} 个芯片平台 GMS 认证测试（${gt.total_fail || 0} 项失败）`);
    if (a17Ok) summaryParts.push(`完成 ${a17.count || 0} 项 Android17 移植任务`);
    lines.push(`> 本周${summaryParts.length ? summaryParts.join('，') : '暂无可用数据'}。`);
    lines.push('');

    // 主题清单
    if ((themes.redmine && themes.redmine.length) || (themes.gerrit && themes.gerrit.length)) {
        lines.push(`**本周关键词**：`);
        if (themes.redmine && themes.redmine.length) lines.push(`- 工单：${_themesText(themes.redmine)}`);
        if (themes.gerrit && themes.gerrit.length) lines.push(`- 提交：${_themesText(themes.gerrit)}`);
        lines.push('');
    }

    // —— 本周工作内容（周报核心：实际做了什么）——
    // 顺序：Redmine → Gerrit → GMS 认证测试 → Android17 移植
    lines.push(`## 本周工作内容`);
    const mergedItems = grOk ? ((gr.lists || {}).merged || []) : [];
    const newItems = grOk ? ((gr.lists || {}).new || []) : [];
    const waitingItems = rmOk ? ((rm.lists || {}).waiting_my_reply || []) : [];
    const staleItems = rmOk ? ((rm.lists || {}).no_reply_3_days || []) : [];
    const _isMechMd = (subj) => /^(bump|version bump|bump version|cherry pick|revert)/i.test(subj || '');
    const mergedMajorMd = mergedItems.filter(it => !_isMechMd(it.subject));
    const mergedMinorMd = mergedItems.filter(it => _isMechMd(it.subject));
    const pushList = (title, items, fmt) => {
        const arr = Array.isArray(items) ? items : [];
        if (!arr.length) return;
        lines.push(`### ${title}（${arr.length}）`);
        arr.slice(0, 30).forEach(it => lines.push(`- ${fmt(it)}`));
        lines.push('');
    };
    // Redmine 工单：标题 + (若有)最新进展展开说明解决了什么
    const pushIssueList = (title, items) => {
        const arr = Array.isArray(items) ? items : [];
        if (!arr.length) return;
        lines.push(`### ${title}（${arr.length}）`);
        arr.slice(0, 10).forEach(it => {
            const head = `#${it.issue_id || it.id || '?'} ${it.subject || ''}`.trim();
            const reply = (it.last_external_reply || '').trim();
            if (reply) {
                lines.push(`- ${head}`);
                lines.push(`  - 最新进展${it.last_external_reply_by ? '（' + it.last_external_reply_by + '）' : ''}：${reply.slice(0, 300)}`);
            } else {
                lines.push(`- ${head}`);
            }
        });
        lines.push('');
    };

    // 1) Redmine 工单
    if (rmOk && rm.resolved_this_period) {
        lines.push(`### Redmine 本周关闭（${rm.resolved_this_period}）`);
        lines.push(`> 本周共关闭/解决 ${rm.resolved_this_period} 个工单（明细见看板）。`);
        lines.push('');
    }
    pushIssueList('跟进中的工单', waitingItems);
    pushIssueList('超 3 天未回复（需关注）', staleItems);

    // 2) Gerrit 提交
    pushList('Gerrit 合并 · 主要（代码已合入）', mergedMajorMd, it => `${it.number || it.id || '?'} ${it.subject || ''}`.trim());
    if (mergedMinorMd.length) {
        lines.push(`> 另有 ${mergedMinorMd.length} 个版本/例行合并（Bump version 等），详见完整明细。`);
        lines.push('');
    }
    pushList('Gerrit 新增/进行中', newItems.filter(it => !_isMechMd(it.subject)), it => `${it.number || it.id || '?'} ${it.subject || ''}`.trim());

    // 按芯片平台和测试模块聚合最新认证进展。
    if (gtOk && (gt.platforms || []).length) {
        lines.push(`### GMS 认证测试进展（${gt.platform_count || 0} 个芯片平台 · ${gt.count || 0} 个模块，总失败 ${gt.total_fail || 0}）`);
        (gt.platforms || []).forEach(p => {
            lines.push(`#### ${p.platform}（${p.module_count} 模块，总用例 ${p.total_cases || 0}，失败 ${p.total_fail || 0}，通过率 ${p.pass_rate || '-'}）`);
            (p.modules || []).forEach(m => {
                const failTag = (m.fail || 0) > 0 ? `，剩余 fail **${m.fail}**` : '';
                lines.push(`- **${m.module}**：总 ${m.total || 0}，通过 ${m.pass || 0}，失败 ${m.fail || 0}，通过率 ${m.pass_rate || '-'}${failTag}`);
                lines.push(`  - 最新：${m.latest_ts || '-'}${m.device ? `；设备：${m.device}` : ''}`);
            });
            lines.push('');
        });
    }

    // 4) Android17 移植工作
    if (a17Ok && (a17.tasks || []).length) {
        lines.push(`### ${a17.title || 'Android17_SDK移植适配工作'}（${a17.count || 0}）`);
        a17.tasks.slice(0, 20).forEach(t => {
            const head = `[${t.category || ''}] ${t.task || ''}`.trim();
            lines.push(`- ${head}`);
            if (t.progress) lines.push(`  - 进展：${t.progress.slice(0, 200)}`);
        });
        lines.push('');
    }

    // —— 数据汇总（折叠）——
    // 顺序：Redmine → Gerrit → GMS 认证测试 → Android17 移植
    lines.push(`<details><summary>数据汇总</summary>`);
    lines.push('');
    lines.push(`### Redmine 工单`);
    if (!rmOk) {
        lines.push(`> 暂不可用：${rm.error || '未配置 Redmine 凭证'}`);
    } else {
        lines.push(`- 本周关闭/解决：**${rm.resolved_this_period || 0}**`);
        lines.push(`- 当前名下工单：${rm.total_owned || 0}（其中开放 ${rm.open_count || 0}）`);
        lines.push(`- 待我回复：${rm.waiting_my_reply || 0}；超 3 天未回复：${rm.no_reply_3_days || 0}`);
    }
    lines.push('');
    lines.push(`### Gerrit 提交`);
    if (!grOk) {
        lines.push(`> 暂不可用：${gr.error || '未配置 Gerrit'}`);
    } else {
        lines.push(`- 本周合并：**${gr.merged_this_period || 0}**；本周新增/进行中：${gr.new_this_period || 0}`);
        lines.push(`- 待我评审：${gr.review_queue_count == null ? '-' : gr.review_queue_count}；当前开放变更：${gr.open_count || 0}`);
    }
    lines.push('');
    lines.push(`### GMS 认证测试`);
    if (gt.available === false) {
        lines.push(`> 暂不可用：${gt.error || '未找到测试结果'}`);
    } else if (gtOk) {
        lines.push(`- ${gt.platform_count || 0} 个平台 / ${gt.count || 0} 个模块，总用例 ${gt.total_cases || 0}，剩余失败 ${gt.total_fail || 0}`);
    }
    lines.push('');
    lines.push(`### Android17 移植`);
    if (a17.available === false) {
        lines.push(`> 暂不可用：${a17.error || '未勾选或解析失败'}`);
    } else if (a17Ok) {
        lines.push(`- 移植任务：完成 **${a17.count || 0}** 项`);
    }
    lines.push('');
    // 全量明细
    if (rmOk) {
        pushList('当前开放工单', (rm.lists || {}).open_issues, it => `#${it.issue_id || it.id || '?'} ${it.subject || ''}`.trim());
    }
    lines.push(`</details>`);
    return lines.join('\n');
}

function renderWeeklyReportHtml(data) {
    const r = data.range || {};
    const rm = data.redmine || {};
    const gr = data.gerrit || {};
    const a17 = data.android17 || {};
    const gt = data.gms_test || {};
    const themes = data.themes || {};
    const rmOk = rm.available !== false;
    const grOk = gr.available !== false;
    const a17Ok = a17.available === true;
    const gtOk = gt.available === true;

    const statCard = (label, value, color) => `
        <div style="background: var(--card-bg); border:1px solid var(--border-color); border-radius:8px; padding:10px 12px; flex:1 1 110px; min-width:110px;">
            <div style="font-size:12px; color: var(--text-secondary);">${_esc(label)}</div>
            <div style="font-size:22px; font-weight:700; color: ${color || 'var(--text-primary)'}; margin-top:2px;">${_esc(value)}</div>
        </div>`;

    // 分组卡片与完整明细共用 workHtml。
    const sectionCard = (title, color, inner) => `
        <div style="margin-top:10px; background: var(--light-bg); border:1px solid var(--border-color); border-left:3px solid ${color}; border-radius:6px; overflow:hidden;">
            <div style="padding:6px 12px; font-weight:700; color:${color}; font-size:13px; border-bottom:1px solid var(--border-color); background: var(--card-bg);">${title}</div>
            <div style="padding:8px 12px;">${inner || ''}</div>
        </div>`;

    // 顺序：Redmine → Gerrit → GMS → Android17
    const summaryParts = [];
    if (rmOk) summaryParts.push(`关闭 <b>${rm.resolved_this_period || 0}</b> 个 Redmine 工单`);
    if (grOk) summaryParts.push(`合并 <b>${gr.merged_this_period || 0}</b> 个 Gerrit 提交、新增 <b>${gr.new_this_period || 0}</b> 个`);
    if (gtOk) summaryParts.push(`推进 <b>${gt.platform_count || 0}</b> 个芯片平台 GMS 认证测试（剩余失败 <b>${gt.total_fail || 0}</b>）`);
    if (a17Ok) summaryParts.push(`完成 <b>${a17.count || 0}</b> 项 Android17 移植任务`);
    const summary = summaryParts.length ? `本周${summaryParts.join('，')}。` : '本周暂无可用数据。';

    const hasThemes = (themes.redmine && themes.redmine.length) || (themes.gerrit && themes.gerrit.length);

    let redmineStats, gerritStats;
    if (!rmOk) {
        redmineStats = `<div style="color: var(--text-secondary); padding:6px 0;">暂不可用：${_esc(rm.error || '未配置 Redmine 凭证')}</div>`;
    } else {
        redmineStats = `<div style="display:flex; flex-wrap:wrap; gap:8px; margin:6px 0;">
            ${statCard('本周关闭', rm.resolved_this_period || 0, 'var(--success-color)')}
            ${statCard('名下工单', rm.total_owned || 0)}
            ${statCard('开放', rm.open_count || 0)}
            ${statCard('待我回复', rm.waiting_my_reply || 0, 'var(--warning-color, #f59e0b)')}
            ${statCard('超3天未回复', rm.no_reply_3_days || 0, 'var(--danger-color)')}
        </div>`;
    }
    if (!grOk) {
        gerritStats = `<div style="color: var(--text-secondary); padding:6px 0;">暂不可用：${_esc(gr.error || '未配置 Gerrit')}</div>`;
    } else {
        gerritStats = `<div style="display:flex; flex-wrap:wrap; gap:8px; margin:6px 0;">
            ${statCard('本周合并', gr.merged_this_period || 0, 'var(--success-color)')}
            ${statCard('本周新增/进行中', gr.new_this_period || 0)}
            ${statCard('待我评审', (gr.review_queue_count == null ? '-' : gr.review_queue_count), 'var(--warning-color, #f59e0b)')}
            ${statCard('当前开放变更', gr.open_count || 0)}
        </div>`;
    }

    // 本周工作默认展示前几项，其余收入折叠明细。
    const changeFmt = it => `<b>${_esc(it.number || it.id || '?')}</b> ${_esc(it.subject || '')}`;
    const issueFmt = it => `<b>#${_esc(it.issue_id || it.id || '?')}</b> ${_esc(it.subject || '')}`;
    const INLINE_LIMIT = 6;
    const inlineList = (items, fmt, limit) => {
        const arr = (Array.isArray(items) ? items : []).slice(0, limit || INLINE_LIMIT);
        if (!arr.length) return '';
        return `<ul style="margin:4px 0 0 18px; padding:0; color: var(--text-secondary);">${arr.map(it => `<li style="margin:3px 0;">${fmt(it)}</li>`).join('')}</ul>`;
    };

    const mergedItems = grOk ? ((gr.lists || {}).merged || []) : [];
    const newItems = grOk ? ((gr.lists || {}).new || []) : [];
    const waitingItems = rmOk ? ((rm.lists || {}).waiting_my_reply || []) : [];
    const staleItems = rmOk ? ((rm.lists || {}).no_reply_3_days || []) : [];

    // 「代表性」判定：Gerrit 机械提交 (Bump version / version bump) 归到「其他」简列，
    // Redmine 工单有 last_external_reply 时展开最新进展。
    const _isMechanical = (subj) => /^(bump|version bump|bump version|cherry pick|revert)/i.test(subj || '');
    const mergedMajor = mergedItems.filter(it => !_isMechanical(it.subject));
    const mergedMinor = mergedItems.filter(it => _isMechanical(it.subject));
    const newMajor = newItems.filter(it => !_isMechanical(it.subject));

    // Redmine 工单项：标题 + (若有)最新进展展开。limit 限制展示条数。
    const issueBlock = (items, limit) => {
        const arr = (Array.isArray(items) ? items : []).slice(0, limit || 6);
        if (!arr.length) return '';
        return arr.map(it => {
            const head = issueFmt(it);
            const reply = (it.last_external_reply || '').trim();
            const replyBy = it.last_external_reply_by ? `（${_esc(it.last_external_reply_by)}）` : '';
            const tail = reply ? `<div style="margin:2px 0 4px 0; padding:2px 6px; background: var(--card-bg); border-left:2px solid var(--border-color); font-size:12px; color: var(--text-secondary);">最新进展${replyBy}：${_esc(reply.slice(0, 300))}</div>` : '';
            return `<li style="margin:5px 0; list-style:none;">${head}${tail}</li>`;
        }).join('');
    };
    const changeBlock = (items, limit) => {
        const arr = (Array.isArray(items) ? items : []).slice(0, limit || 8);
        if (!arr.length) return '';
        return arr.map(it => `<li style="margin:3px 0;">${changeFmt(it)}</li>`).join('');
    };

    const workHtml = (() => {
        if (!grOk && !rmOk && !a17Ok && !gtOk) {
            return '<div style="color: var(--text-muted); padding:6px 0;">本周暂无可用数据。</div>';
        }
        const parts = [];
        // 顺序：Redmine → Gerrit → GMS 认证测试 → Android17 移植
        // —— Redmine 工单 ——
        const rParts = [];
        if (rmOk && rm.resolved_this_period) {
            rParts.push(`<div style="margin-top:6px; color: var(--text-secondary);">本周关闭/解决 <b style="color: var(--success-color);">${rm.resolved_this_period}</b> 个工单。</div>`);
        }
        if (waitingItems.length) {
            rParts.push(`<div style="margin-top:6px;"><b style="color: var(--warning-color, #f59e0b);">跟进中的工单（${waitingItems.length}）</b><ul style="margin:4px 0 0 0; padding:0;">${issueBlock(waitingItems, 6)}</ul></div>`);
        }
        if (staleItems.length) {
            rParts.push(`<div style="margin-top:6px;"><b style="color: var(--danger-color);">超3天未回复 · 需关注（${staleItems.length}）</b><ul style="margin:4px 0 0 0; padding:0;">${issueBlock(staleItems, 6)}</ul></div>`);
        }
        if (rParts.length) parts.push(sectionCard('🔵 Redmine 工单', 'var(--primary-color)', rParts.join('')));
        // —— Gerrit 代码提交 ——
        const gParts = [];
        if (mergedMajor.length) {
            gParts.push(`<div style="margin-top:6px;"><b style="color: var(--success-color);">本周合并 · 主要（${mergedMajor.length}）</b><ul style="margin:4px 0 0 0; padding:0; color: var(--text-secondary);">${changeBlock(mergedMajor, 8)}</ul></div>`);
        }
        if (mergedMinor.length) {
            gParts.push(`<div style="margin-top:6px;"><b style="color: var(--text-muted);">本周合并 · 版本/例行（${mergedMinor.length}）</b><ul style="margin:4px 0 0 18px; padding:0; color: var(--text-muted); font-size:12px;">${mergedMinor.map(it => `<li>${changeFmt(it)}</li>`).join('')}</ul></div>`);
        }
        if (newMajor.length) {
            gParts.push(`<div style="margin-top:6px;"><b>本周新增/进行中（${newItems.length}）</b><ul style="margin:4px 0 0 18px; padding:0; color: var(--text-secondary);">${changeBlock(newMajor, 6)}</ul></div>`);
        }
        if (gParts.length) parts.push(sectionCard('🟢 Gerrit 代码提交', 'var(--success-color)', gParts.join('')));
        // —— GMS 认证测试：平台 × 模块矩阵（每组合取区间内最新一次）——
        if (gtOk && (gt.platforms || []).length) {
            const failColor = v => (v > 0 ? 'var(--danger-color)' : 'var(--success-color)');
            const platCards = (gt.platforms || []).map(p => {
                const cells = (p.modules || []).map(m => {
                    const rate = m.pass_rate || '-';
                    return `<div style="display:flex; justify-content:space-between; gap:8px; padding:4px 6px; border-bottom:1px solid var(--border-color);">
                        <div><b>${_esc(m.module)}</b><div style="font-size:11px; color: var(--text-secondary);">总 ${m.total || 0} · ${_esc(m.latest_ts || '').slice(0,18)}</div></div>
                        <div style="text-align:right; white-space:nowrap;">通过率 <b>${_esc(rate)}</b><div style="font-size:11px; color:${failColor(m.fail || 0)};">剩余 fail <b>${m.fail || 0}</b></div></div>
                    </div>`;
                }).join('');
                return `<div style="margin-top:6px; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                    <div style="padding:5px 8px; background: var(--card-bg); font-weight:700;">${_esc(p.platform)} <span style="font-weight:400; font-size:11px; color: var(--text-secondary);">· ${p.module_count} 模块 · 总 ${p.total_cases || 0} · 失败 <b style="color:${failColor(p.total_fail || 0)};">${p.total_fail || 0}</b> · 通过率 ${_esc(p.pass_rate || '-')}</span></div>
                    ${cells}
                </div>`;
            }).join('');
            parts.push(sectionCard('🟠 GMS 认证测试进展（平台 × 模块）', 'var(--orange-color, #f97316)', platCards));
        }
        // —— Android17 移植 ——
        if (a17Ok && (a17.tasks || []).length) {
            const rows = a17.tasks.slice(0, 12).map(t => {
                const head = `[${_esc(t.category || '')}] ${_esc(t.task || '')}`;
                return `<li style="margin:4px 0;"><b>${head.trim() || '任务'}</b>${t.progress ? `<div style="margin:2px 0 4px 0; padding:2px 6px; background: var(--card-bg); border-left:2px solid var(--border-color); font-size:12px; color: var(--text-secondary);">${_esc(t.progress.slice(0, 200))}</div>` : ''}</li>`;
            }).join('');
            parts.push(sectionCard('🟣 Android17 SDK 移植适配', 'var(--purple-color, #7c3aed)', `<ul style="margin:4px 0 0 0; padding:0; color: var(--text-secondary);">${rows}</ul>`));
        }
        if (!parts.length) return '<div style="color: var(--text-muted); padding:6px 0;">本周暂无合并/新增提交或待处理工单。</div>';
        return parts.join('');
    })();

    // 详细明细 (可折叠) —— 全量列表
    const listBlock = (title, items, fmt) => {
        const arr = Array.isArray(items) ? items : [];
        if (!arr.length) return '';
        const rows = arr.slice(0, 30).map(it => `<li style="margin:2px 0;">${fmt(it)}</li>`).join('');
        return `<div style="margin-top:8px;"><div style="font-weight:600; color: var(--text-primary);">${_esc(title)}（${arr.length}）</div><ul style="margin:4px 0 0 18px; padding:0; color: var(--text-secondary);">${rows}</ul></div>`;
    };
    // 顺序：Redmine → Gerrit → GMS 认证测试 → Android17 移植
    let detailInner = '';
    if (rmOk) {
        let rmDetail = '';
        rmDetail += listBlock('待我回复', waitingItems, issueFmt);
        rmDetail += listBlock('超 3 天未回复', staleItems, issueFmt);
        rmDetail += listBlock('当前开放工单', (rm.lists || {}).open_issues, issueFmt);
        if (rmDetail) detailInner += sectionCard('🔵 Redmine 工单', 'var(--primary-color)', rmDetail);
    }
    if (grOk) {
        let grDetail = '';
        grDetail += listBlock('本周合并', mergedItems, changeFmt);
        grDetail += listBlock('本周新增/进行中', newItems, changeFmt);
        if (grDetail) detailInner += sectionCard('🟢 Gerrit 代码提交', 'var(--success-color)', grDetail);
    }
    // GMS 认证测试明细
    if (gtOk && (gt.platforms || []).length) {
        let gtDetail = '';
        (gt.platforms || []).forEach(p => {
            gtDetail += listBlock(`${p.platform}（总 ${p.total_cases || 0}，失败 ${p.total_fail || 0}）`, p.modules || [],
                m => `${_esc(m.module)} — 总 ${m.total || 0}，通过 ${m.pass || 0}，失败 ${m.fail || 0}，通过率 ${_esc(m.pass_rate || '-')}（${_esc(m.latest_ts || '')}）`);
        });
        if (gtDetail) detailInner += sectionCard('🟠 GMS 认证测试进展（平台 × 模块）', 'var(--orange-color, #f97316)', gtDetail);
    }
    // Android17 移植明细
    if (a17Ok && (a17.tasks || []).length) {
        const a17Detail = listBlock('已完成任务', a17.tasks, t => `[${_esc(t.category || '')}] ${_esc(t.task || '')}`);
        if (a17Detail) detailInner += sectionCard('🟣 Android17 SDK 移植适配', 'var(--purple-color, #7c3aed)', a17Detail);
    }
    const detailHtml = detailInner
        ? `<details style="margin-top:12px; border-top:1px dashed var(--border-color); padding-top:8px;"><summary style="cursor:pointer; color: var(--text-secondary); font-weight:600;">完整明细（点击展开）</summary><div style="margin-top:6px;">${detailInner}</div></details>`
        : '';

    const themesHtml = hasThemes ? `
        <div style="margin-top:8px;">
            <div style="font-weight:600; color: var(--text-primary); margin-bottom:2px;">本周关键词</div>
            ${themes.redmine && themes.redmine.length ? `<div style="margin:2px 0;"><span style="color: var(--text-secondary); font-size:12px;">工单：</span>${_themesChips(themes.redmine)}</div>` : ''}
            ${themes.gerrit && themes.gerrit.length ? `<div style="margin:2px 0;"><span style="color: var(--text-secondary); font-size:12px;">提交：</span>${_themesChips(themes.gerrit)}</div>` : ''}
        </div>` : '';

    return `
        <div style="background: var(--light-bg); border:1px solid var(--border-color); border-left:3px solid var(--primary-color); border-radius:6px; padding:8px 12px; margin-bottom:10px;">
            <div style="font-size:12px; color: var(--text-secondary);">${_esc(r.start)} ~ ${_esc(r.end)}（${_esc(r.label || '自定义')}） · ${_esc(data.name || '')}</div>
            <div style="margin-top:4px;">${summary}</div>
            ${themesHtml}
        </div>
        <h3 style="margin:10px 0 2px; color: var(--text-primary);">本周工作</h3>
        ${workHtml}
        <details style="margin-top:10px;"><summary style="cursor:pointer; color: var(--text-secondary); font-size:12px;">数据汇总（关闭/名下/开放/待评审…）</summary>
            <h4 style="margin:8px 0 2px; color: var(--text-primary);">Redmine 工单</h4>
            ${redmineStats}
            <h4 style="margin:8px 0 2px; color: var(--text-primary);">Gerrit 提交</h4>
            ${gerritStats}
        </details>
        ${detailHtml}`;
}

function copyWeeklyReportMarkdown() {
    if (!weeklyReportMarkdown) { showToast('请先生成周报', 'warning'); return; }
    const done = () => showToast('Markdown 已复制', 'success');
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(weeklyReportMarkdown).then(done).catch(() => _fallbackCopy(weeklyReportMarkdown, done));
    } else {
        _fallbackCopy(weeklyReportMarkdown, done);
    }
}

function _fallbackCopy(text, cb) {
    try {
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); document.body.removeChild(ta); cb();
    } catch (e) { showToast('复制失败', 'error'); }
}

function downloadWeeklyReport() {
    if (!weeklyReportMarkdown) { showToast('请先生成周报', 'warning'); return; }
    const d = window.__weeklyReportData || {};
    const r = d.range || {};
    const safeName = (d.name || '周报').replace(/[\\/:*?"<>|/\s]+/g, '_');
    const name = `${safeName}_${r.start || ''}_${r.end || ''}.md`;
    const blob = new Blob([weeklyReportMarkdown], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name; document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
}

async function sendWeeklyReportEmail() {
    if (!weeklyReportMarkdown) { showToast('请先生成周报', 'warning'); return; }
    const d = window.__weeklyReportData || {};
    const r = d.range || {};
    const to = prompt('收件人邮箱（多个用逗号或分号分隔）：', '');
    if (!to || !to.trim()) return;
    const cc = (prompt('抄送（可留空，多个用逗号或分号分隔）：', '') || '').trim();
    const subject = `周报 - ${d.name || ''} ${r.start || ''}~${r.end || ''}`.trim();
    const status = document.getElementById('weekly-report-status');
    if (status) status.textContent = '正在发送邮件...';
    try {
        const resp = await fetch('/api/email/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                to: to.trim(),
                cc: cc || undefined,
                subject,
                body: weeklyReportMarkdown,
                is_html: false,
                sender_name: '周报总结',
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
    } finally {
        if (status) status.textContent = '';
    }
}
window.openWeeklyReport = openWeeklyReport;
window.closeWeeklyReportModal = closeWeeklyReportModal;
window.generateWeeklyReport = generateWeeklyReport;
window.onWeeklyReportMemberChange = onWeeklyReportMemberChange;
window.generateWeeklyReportAi = generateWeeklyReportAi;
window.copyWeeklyReportMarkdown = copyWeeklyReportMarkdown;
window.downloadWeeklyReport = downloadWeeklyReport;
window.sendWeeklyReportEmail = sendWeeklyReportEmail;
