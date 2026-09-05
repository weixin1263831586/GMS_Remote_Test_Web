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
            if (content) content.innerHTML = `<div style="color: var(--danger-color); padding: 12px;">生成失败：${result.error || result.message || '未知错误'}</div>`;
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
        if (content) content.innerHTML = `<div style="color: var(--danger-color); padding: 12px;">生成失败：${err.message}</div>`;
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

function ut_getCategoryInfo(category) {
    return ut_categoryMeta[category] || UT_DEFAULT_CATEGORIES[category] || { icon: '📁', color: '#8e8e93' };
}

function ut_loadToolsList() {
    try {
        ut_categoryMeta = JSON.parse(localStorage.getItem(UT_CATEGORY_META_KEY) || '{}') || {};
    } catch (e) {
        console.error('加载常用工具分类信息失败:', e);
        ut_categoryMeta = {};
    }

    const stored = localStorage.getItem(UT_STORAGE_KEY);
    if (stored) {
        try {
            ut_categorizedTools = JSON.parse(stored);
            ut_ensureBuiltInTools();
            ut_renderToolsGrid();
            return;
        } catch (e) { console.error('加载常用工具数据失败:', e); }
    }
    // 默认数据
    ut_categorizedTools = {
        '常用工具': [
            {
                icon: '📦',
                title: '共享固件',
                description: '登记编译服务器上的固件路径，其他客户端可直接从远端主机流式下载',
                action: 'shareFirmware',
                builtin_id: 'builtin-share-firmware'
            },
            {
                icon: '📦',
                title: 'Gerrit Patch导出与导入',
                description: '从Gerrit导出patch并应用到本地Android源码，支持批量导出Change的patch文件到指定目录，也支持将patch应用到本地代码',
                file_path: 'gerrit_patch_export_and_apply_tool.sh'
            }
        ],
        '测试工具': []
    };
    ut_ensureBuiltInTools();
    ut_saveCategories();
    ut_renderToolsGrid();
}

const UT_HIDDEN_BUILTINS_KEY = 'gms_utility_tools_hidden_builtins';

function ut_getHiddenBuiltins() {
    try {
        return new Set(JSON.parse(localStorage.getItem(UT_HIDDEN_BUILTINS_KEY) || '[]'));
    } catch (e) {
        return new Set();
    }
}

function ut_hideBuiltin(builtinId) {
    const hidden = ut_getHiddenBuiltins();
    hidden.add(builtinId);
    localStorage.setItem(UT_HIDDEN_BUILTINS_KEY, JSON.stringify([...hidden]));
}

function ut_ensureBuiltInTools() {
    let changed = false;
    const hidden = ut_getHiddenBuiltins();
    Object.entries(UT_BUILTIN_TOOLS).forEach(([category, tools]) => {
        if (!ut_categorizedTools[category]) {
            ut_categorizedTools[category] = [];
            changed = true;
        }
        tools.forEach((builtinTool) => {
            // Skip builtins the user has explicitly hidden/deleted.
            if (hidden.has(builtinTool.builtin_id)) return;
            const existingIndex = ut_categorizedTools[category].findIndex(tool => tool.builtin_id === builtinTool.builtin_id);
            if (existingIndex >= 0) {
                ut_categorizedTools[category][existingIndex] = {
                    ...ut_categorizedTools[category][existingIndex],
                    ...builtinTool
                };
            } else {
                ut_categorizedTools[category].unshift(builtinTool);
                changed = true;
            }
        });
    });
    if (changed) ut_saveCategories();
}

function ut_saveCategories() {
    localStorage.setItem(UT_STORAGE_KEY, JSON.stringify(ut_categorizedTools));
    localStorage.setItem(UT_CATEGORY_META_KEY, JSON.stringify(ut_categoryMeta));
}

function ut_renderToolsGrid() {
    const grid = document.getElementById('ut-grid');
    if (!grid) return;
    ut_renderAllCategories(grid);
    ut_restoreSyncButtonState();
}

async function ut_restoreSyncButtonState() {
    try {
        const checks = [
            {
                action: 'mainline-sync',
                url: '/api/mainline-known-issues/sync/status',
                parse: data => data.status || {},
                poll: (btn) => { ut_mainlineSyncButton = btn; ut_pollMainlineKnownIssuesSync(btn, '触发扫描'); }
            },
            {
                action: 'gms-update-sync',
                url: '/api/gms-update-monitor/sync/status',
                parse: data => (data.data && data.data.status) || {},
                poll: (btn, tool) => {
                    ut_gmsUpdateSyncButton = btn;
                    ut_pollGmsUpdateMonitorSync(
                        btn,
                        '触发扫描',
                        (tool && tool.sync_title) || 'GMS/CTS更新',
                        (tool && tool.sync_sources) || []
                    );
                }
            }
        ];
        for (const check of checks) {
            const response = await fetch(check.url);
            const result = await response.json();
            const status = check.parse(result);
            if (!status.running) continue;
            const runningSources = Array.isArray(status.source) ? status.source : [];
            const syncTool = ut_categorizedTools && Object.values(ut_categorizedTools)
                .flat().find(t => {
                    if (t.action !== check.action) return false;
                    if (check.action !== 'gms-update-sync' || runningSources.length === 0) return true;
                    const toolSources = Array.isArray(t.sync_sources) ? t.sync_sources : [];
                    return toolSources.length === runningSources.length &&
                        runningSources.every(source => toolSources.includes(source));
                });
            if (!syncTool) continue;
            const cards = document.querySelectorAll('#ut-grid .tool-card, #ut-grid [style*="cursor: pointer"]');
            for (const card of cards) {
                const titleEl = card.querySelector('div[style*="font-weight: 600"]');
                if (titleEl && titleEl.textContent === syncTool.title) {
                    const btn = card.querySelector('button');
                    if (btn) {
                        btn.disabled = true;
                        btn.textContent = '扫描中';
                        check.poll(btn, syncTool);
                    }
                    break;
                }
            }
        }
    } catch (e) {
        // 静默失败，不影响页面加载
    }
}

function ut_renderAllCategories(grid) {
    grid.innerHTML = '';
    const categories = Object.keys(ut_categorizedTools);
    if (categories.length === 0) {
        grid.innerHTML = `
            <div style="text-align: center; padding: 60px 20px; color: var(--text-muted);">
                <div style="font-size: 64px; margin-bottom: 20px;">🧰</div>
                <div style="font-size: 16px; margin-bottom: 8px;">还没有工具</div>
                <div style="font-size: 13px;">点击下方按钮添加分类和工具</div>
            </div>`;
        return;
    }

    categories.forEach((category, catIndex) => {
        const tools = ut_categorizedTools[category];
        const catInfo = ut_getCategoryInfo(category);
        const section = document.createElement('div');
        section.className = 'category-section';
        section.dataset.category = category;
        section.dataset.categoryIndex = catIndex;
        section.draggable = true;
        section.style.marginBottom = '4px';
        section.addEventListener('dragstart', handleCategoryDragStart);
        section.addEventListener('dragend', handleCategoryDragEnd);
        section.addEventListener('dragover', handleCategoryDragOver);
        section.addEventListener('drop', ut_handleCategoryDrop);

        const header = document.createElement('div');
        header.style.cssText = 'display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border-color); cursor: move;';

        const titleWrap = document.createElement('div');
        titleWrap.style.cssText = 'display: flex; align-items: center;';

        const icon = document.createElement('span');
        icon.style.cssText = 'font-size: 16px; margin-right: 6px; cursor: grab;';
        icon.textContent = catInfo.icon || '📁';
        titleWrap.appendChild(icon);

        const title = document.createElement('span');
        title.style.cssText = 'font-size: 16px; font-weight: 600; color: var(--text-primary); cursor: pointer;';
        title.textContent = category;
        title.addEventListener('click', () => ut_editCategory(category));
        titleWrap.appendChild(title);

        const count = document.createElement('span');
        count.style.cssText = 'margin-left: 10px; padding: 2px 8px; background: var(--light-bg); border-radius: 10px; font-size: 11px; color: var(--text-muted);';
        count.textContent = String(tools.length);
        titleWrap.appendChild(count);

        const actions = document.createElement('div');
        actions.style.cssText = 'display: flex; gap: 6px;';

        const addBtn = document.createElement('button');
        addBtn.textContent = '添加工具';
        addBtn.style.cssText = `padding: 6px 16px; background: ${catInfo.color}; color: white; border: none; border-radius: 5px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.2s;`;
        addBtn.addEventListener('mouseenter', () => { addBtn.style.opacity = '0.85'; });
        addBtn.addEventListener('mouseleave', () => { addBtn.style.opacity = '1'; });
        addBtn.addEventListener('click', () => ut_addNewToolToCategory(category));
        actions.appendChild(addBtn);

        const deleteBtn = document.createElement('button');
        deleteBtn.textContent = '🗑️';
        deleteBtn.title = '删除分类';
        deleteBtn.style.cssText = 'padding: 6px 12px; background: var(--danger-color); color: white; border: none; border-radius: 5px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.2s;';
        deleteBtn.addEventListener('mouseenter', () => { deleteBtn.style.opacity = '0.85'; });
        deleteBtn.addEventListener('mouseleave', () => { deleteBtn.style.opacity = '1'; });
        deleteBtn.addEventListener('click', () => ut_deleteCategory(category));
        actions.appendChild(deleteBtn);

        header.appendChild(titleWrap);
        header.appendChild(actions);
        section.appendChild(header);

        const cards = document.createElement('div');
        cards.style.cssText = 'display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 16px;';
        tools.forEach((tool, index) => {
            cards.appendChild(ut_createToolCard(tool, category, index, catInfo.color));
        });
        section.appendChild(cards);
        grid.appendChild(section);
    });
}

function ut_createToolCard(tool, category, index, cardColor) {
    const card = document.createElement('div');
    card.className = 'tool-card';
    card.title = tool.description || '';
    card.dataset.toolCategory = category;
    card.dataset.toolIndex = index;
    card.draggable = true;
    card.style.cssText = `background: var(--card-bg); border-radius: 6px; padding: 10px; display: flex; flex-direction: column; align-items: center; gap: 6px; border: 1px solid var(--border-color); border-top: 2px solid ${cardColor}; transition: all 0.2s; cursor: grab; min-width: 100px; max-width: 130px; min-height: 118px;`;
    if (tool.action === 'openWeeklyReport') {
        card.addEventListener('click', () => openWeeklyReport());
    } else if (tool.action === 'shareFirmware') {
        card.addEventListener('click', () => shareFirmware());
    } else if (tool.url) {
        card.addEventListener('click', () => openToolLink(tool.url));
    } else if (tool.file_path && tool.action !== 'mainline-sync') {
        card.addEventListener('click', () => ut_downloadTool(tool.file_path, tool.title));
    }
    card.addEventListener('mouseenter', () => {
        card.style.transform = 'translateY(-2px)';
        card.style.boxShadow = 'var(--shadow-md)';
        card.style.borderColor = cardColor;
    });
    card.addEventListener('mouseleave', () => {
        card.style.transform = 'translateY(0)';
        card.style.boxShadow = 'none';
        card.style.borderColor = 'var(--border-color)';
    });
    card.addEventListener('dragstart', handleToolDragStart);
    card.addEventListener('dragend', handleToolDragEnd);
    card.addEventListener('dragover', handleToolDragOver);
    card.addEventListener('drop', ut_handleToolDrop);

    const icon = document.createElement('div');
    icon.style.cssText = 'width: 40px; height: 40px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; background: var(--light-bg); border-radius: 6px; font-size: 24px; pointer-events: none;';
    icon.textContent = tool.icon || '🔧';
    card.appendChild(icon);

    const textWrap = document.createElement('div');
    textWrap.style.cssText = 'text-align: center; width: 100%; pointer-events: none;';
    const title = document.createElement('div');
    title.style.cssText = 'font-size: 12px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%;';
    title.textContent = tool.title || '';
    textWrap.appendChild(title);
    card.appendChild(textWrap);

    const actions = document.createElement('div');
    actions.style.cssText = 'display: flex; gap: 3px; width: 100%; margin-top: auto;';

    const isSyncTool = tool.action === 'mainline-sync' || tool.action === 'gms-update-sync';
    const isShareFirmwareTool = tool.action === 'shareFirmware';
    // sync 工具显示触发按钮
    if (isSyncTool) {
        const downloadBtn = document.createElement('button');
        downloadBtn.textContent = '触发扫描';
        downloadBtn.title = tool.action === 'gms-update-sync' ? '扫描 GMS/CTS 更新' : '扫描 Mainline release notes';
        downloadBtn.style.cssText = 'flex: 2; padding: 2px 3px; background: var(--success-color); color: white; border: none; border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;';
        downloadBtn.addEventListener('mouseenter', () => { downloadBtn.style.opacity = '0.8'; });
        downloadBtn.addEventListener('mouseleave', () => { downloadBtn.style.opacity = '1'; });
        downloadBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            if (tool.action === 'gms-update-sync') {
                ut_startGmsUpdateMonitorSync(downloadBtn, tool);
            } else {
                ut_startMainlineKnownIssuesSync(downloadBtn);
            }
        });
        actions.appendChild(downloadBtn);
    }
    // 有 file_path 且无 url 的下载型工具不显示下载按钮（点击卡片即下载）
    // 只有有 url 的非内置工具才显示下载按钮
    const isDownloadOnlyTool = !isSyncTool && tool.file_path && !tool.url;
    if (!tool.builtin_id && !isSyncTool && !isShareFirmwareTool && !isDownloadOnlyTool) {
        const downloadBtn = document.createElement('button');
        downloadBtn.textContent = '下载';
        downloadBtn.title = '下载文件';
        downloadBtn.style.cssText = 'flex: 2; padding: 2px 3px; background: var(--success-color); color: white; border: none; border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;';
        downloadBtn.addEventListener('mouseenter', () => { downloadBtn.style.opacity = '0.8'; });
        downloadBtn.addEventListener('mouseleave', () => { downloadBtn.style.opacity = '1'; });
        downloadBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            ut_downloadTool(tool.file_path, tool.title);
        });
        actions.appendChild(downloadBtn);
    }

    if (!tool.builtin_id) {
        const editBtn = document.createElement('button');
        editBtn.textContent = '✏️';
        editBtn.style.cssText = 'flex: 1; padding: 2px 3px; background: transparent; color: var(--text-secondary); border: 1px solid var(--border-color); border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;';
        editBtn.addEventListener('mouseenter', () => {
            editBtn.style.background = 'var(--primary-color)';
            editBtn.style.color = 'white';
            editBtn.style.borderColor = 'var(--primary-color)';
        });
        editBtn.addEventListener('mouseleave', () => {
            editBtn.style.background = 'transparent';
            editBtn.style.color = 'var(--text-secondary)';
            editBtn.style.borderColor = 'var(--border-color)';
        });
        editBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            ut_editTool(category, index);
        });
        actions.appendChild(editBtn);

        const deleteBtn = document.createElement('button');
        deleteBtn.textContent = '🗑️';
        deleteBtn.style.cssText = 'flex: 1; padding: 2px 3px; background: transparent; color: var(--text-muted); border: 1px solid var(--border-color); border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;';
        deleteBtn.addEventListener('mouseenter', () => {
            deleteBtn.style.background = 'var(--danger-color)';
            deleteBtn.style.color = 'white';
            deleteBtn.style.borderColor = 'var(--danger-color)';
        });
        deleteBtn.addEventListener('mouseleave', () => {
            deleteBtn.style.background = 'transparent';
            deleteBtn.style.color = 'var(--text-muted)';
            deleteBtn.style.borderColor = 'var(--border-color)';
        });
        deleteBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            ut_deleteTool(category, index);
        });
        actions.appendChild(deleteBtn);
    }

    // Built-in tools get a hide button so users can remove them.
    if (tool.builtin_id) {
        const hideBtn = document.createElement('button');
        hideBtn.textContent = '🗑️';
        hideBtn.title = '隐藏此内置工具';
        hideBtn.style.cssText = 'flex: 1; padding: 2px 3px; background: transparent; color: var(--text-muted); border: 1px solid var(--border-color); border-radius: 3px; font-size: 12px; cursor: pointer; transition: all 0.2s; line-height: 1;';
        hideBtn.addEventListener('mouseenter', () => {
            hideBtn.style.background = 'var(--danger-color)';
            hideBtn.style.color = 'white';
            hideBtn.style.borderColor = 'var(--danger-color)';
        });
        hideBtn.addEventListener('mouseleave', () => {
            hideBtn.style.background = 'transparent';
            hideBtn.style.color = 'var(--text-muted)';
            hideBtn.style.borderColor = 'var(--border-color)';
        });
        hideBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            ut_hideBuiltin(tool.builtin_id);
            ut_categorizedTools[category].splice(index, 1);
            ut_saveCategories();
            ut_renderToolsGrid();
            showToast(`已隐藏「${tool.title}」`, 'info');
        });
        actions.appendChild(hideBtn);
    }

    card.appendChild(actions);
    return card;
}

// 常用工具拖拽排序。

function ut_handleToolDrop(e) {
    e.preventDefault();
    const card = e.target.closest('.tool-card');
    if (!card || !draggedTool) return;
    const targetCategory = card.dataset.toolCategory;
    const targetIndex = parseInt(card.dataset.toolIndex);
    if (targetCategory === draggedTool.category &&
        targetIndex === draggedTool.index) return;
    moveTool(ut_categorizedTools, draggedTool.category, draggedTool.index, targetCategory, targetIndex, ut_saveCategories);
    ut_renderToolsGrid();
}

function ut_handleCategoryDrop(e) {
    e.preventDefault();
    const section = e.target.closest('.category-section');
    if (!section || draggedCategoryIndex === null) return;
    const targetIndex = parseInt(section.dataset.categoryIndex);
    if (targetIndex === draggedCategoryIndex) return;
    moveCategory(ut_categorizedTools, draggedCategoryIndex, targetIndex, (newData) => { ut_categorizedTools = newData; ut_saveCategories(); });
    ut_renderToolsGrid();
}

let ut_mainlineSyncButton = null;
let ut_gmsUpdateSyncButton = null;

async function ut_startMainlineKnownIssuesSync(button) {
    ut_mainlineSyncButton = button;
    // 检查 db 是否存在，不存在则直接全量扫描
    try {
        const resp = await fetch('/api/mainline-known-issues/sync/status');
        const data = await resp.json();
        if (data.status && data.status.running) {
            showToast('扫描正在进行中，请稍候', 'warning');
            return;
        }
        if (!data.status || !data.status.db_exists) {
            // db 不存在，弹框提示后直接全量扫描
            showConfirmDialog(
                '首次扫描提示',
                '首次使用需要全量扫描，请确保本机浏览器已打开 https://docs.partner.android.com/mainline/release/release-notes 并可正常访问。'
            ).then(confirmed => {
                if (confirmed) {
                    ut_confirmMainlineKnownIssuesSync('full');
                }
            });
            return;
        }
    } catch (e) {
        // 查询失败，继续弹框选择
    }
    // db 存在，弹框选择扫描方式
    const modal = document.getElementById('mainline-sync-modal');
    if (modal) {
        modal.style.display = 'flex';
    }
}

function ut_closeMainlineSyncModal() {
    const modal = document.getElementById('mainline-sync-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

async function ut_confirmMainlineKnownIssuesSync(mode) {
    const button = ut_mainlineSyncButton;
    ut_closeMainlineSyncModal();
    if (!button) {
        showToast('未找到扫描按钮，请刷新页面后重试', 'error');
        return;
    }
    const modeText = mode === 'full' ? '全量扫描' : '增量扫描';
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = '扫描中';
    try {
        const result = await apiCall(
            '/api/mainline-known-issues/sync?mode=' + encodeURIComponent(mode),
            'POST'
        );
        if (!result.success) {
            throw new Error(result.error || '启动扫描失败');
        }
        showToast('Mainline包豁免项' + modeText + '已启动', 'info');
        ut_pollMainlineKnownIssuesSync(button, originalText);
    } catch (error) {
        button.disabled = false;
        button.textContent = originalText;
        showToast('启动扫描失败: ' + error.message, 'error');
    }
}

async function ut_pollMainlineKnownIssuesSync(button, originalText) {
    try {
        const response = await fetch('/api/mainline-known-issues/sync/status');
        const result = await response.json();
        const status = result.status || {};
        if (status.running) {
            setTimeout(() => ut_pollMainlineKnownIssuesSync(button, originalText), 3000);
            return;
        }
        button.disabled = false;
        button.textContent = originalText;
        if (status.error) {
            showToast('扫描失败: ' + status.error, 'error');
            if (typeof notifyOperationResult === 'function') {
                notifyOperationResult('Mainline包豁免项扫描失败', status.error, 'error', 'system');
            }
            return;
        }
        // 显示扫描完成详情
        let duration = '';
        if (status.started_at && status.finished_at) {
            const sec = Math.round((new Date(status.finished_at) - new Date(status.started_at)) / 1000);
            duration = sec >= 60 ? `耗时 ${Math.floor(sec / 60)}分${sec % 60}秒` : `耗时 ${sec}秒`;
        }
        const msg = `Mainline包豁免项扫描完成${duration ? '（' + duration + '）' : ''}`;
        showToast(msg, 'success');
        // 发送 Windows 系统通知
        if (typeof notifyOperationResult === 'function') {
            notifyOperationResult('Mainline包豁免项扫描完成', duration || '扫描已完成', 'success', 'system');
        }
    } catch (error) {
        button.disabled = false;
        button.textContent = originalText;
        showToast('扫描状态查询失败: ' + error.message, 'error');
    }
}

async function ut_startGmsUpdateMonitorSync(button, tool) {
    ut_gmsUpdateSyncButton = button;
    const syncTitle = (tool && tool.sync_title) || 'GMS/CTS更新';
    const sources = (tool && Array.isArray(tool.sync_sources)) ? tool.sync_sources : [];
    try {
        const resp = await fetch('/api/gms-update-monitor/sync/status');
        const data = await resp.json();
        const status = (data.data && data.data.status) || {};
        if (status.running) {
            showToast('更新扫描正在进行中，请稍候', 'warning');
            return;
        }
        if (!status.db_exists) {
            const confirmed = await showConfirmDialog(
                '首次扫描提示',
                '首次使用将执行全量扫描，请确保本机浏览器已可正常访问 docs.partner.android.com。'
            );
            if (confirmed) {
                ut_confirmGmsUpdateMonitorSync('full', sources, syncTitle);
            }
            return;
        }
    } catch (e) {
        // 查询失败时继续确认全量扫描
    }
    const confirmed = await showConfirmDialog(
        syncTitle + '扫描',
        '确定执行全量扫描吗？'
    );
    if (confirmed) {
        ut_confirmGmsUpdateMonitorSync('full', sources, syncTitle);
    }
}

async function ut_confirmGmsUpdateMonitorSync(mode, sources, syncTitle) {
    const button = ut_gmsUpdateSyncButton;
    if (!button) {
        showToast('未找到扫描按钮，请刷新页面后重试', 'error');
        return;
    }
    sources = Array.isArray(sources) ? sources : [];
    syncTitle = syncTitle || 'GMS/CTS更新';
    const modeText = mode === 'full' ? '全量扫描' : '增量扫描';
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = '扫描中';
    try {
        const params = new URLSearchParams({ mode });
        sources.forEach(source => params.append('source', source));
        const result = await apiCall(
            '/api/gms-update-monitor/sync?' + params.toString(),
            'POST'
        );
        if (!result.success) {
            throw new Error(result.error || '启动扫描失败');
        }
        showToast(syncTitle + modeText + '已启动', 'info');
        ut_pollGmsUpdateMonitorSync(button, originalText, syncTitle, sources);
    } catch (error) {
        button.disabled = false;
        button.textContent = originalText;
        showToast('启动扫描失败: ' + error.message, 'error');
    }
}

async function ut_pollGmsUpdateMonitorSync(button, originalText, syncTitle, sources) {
    syncTitle = syncTitle || 'GMS/CTS更新';
    sources = Array.isArray(sources) ? sources : [];
    try {
        const response = await fetch('/api/gms-update-monitor/sync/status');
        const result = await response.json();
        const status = (result.data && result.data.status) || {};
        if (status.running) {
            setTimeout(() => ut_pollGmsUpdateMonitorSync(button, originalText, syncTitle, sources), 3000);
            return;
        }
        button.disabled = false;
        button.textContent = originalText;
        if (status.error) {
            showToast(syncTitle + '扫描失败: ' + status.error, 'error');
            if (typeof notifyOperationResult === 'function') {
                notifyOperationResult(syncTitle + '扫描失败', status.error, 'error', 'system');
            }
            return;
        }
        let duration = '';
        if (status.started_at && status.finished_at) {
            const sec = Math.round((new Date(status.finished_at) - new Date(status.started_at)) / 1000);
            duration = sec >= 60 ? `耗时 ${Math.floor(sec / 60)}分${sec % 60}秒` : `耗时 ${sec}秒`;
        }
        showToast(syncTitle + '扫描完成' + (duration ? '，' + duration : ''), 'success');
        if (typeof notifyOperationResult === 'function') {
            notifyOperationResult(syncTitle + '扫描完成', (status.stdout || '').trim() || '扫描完成', 'success', 'system');
        }
        if (syncTitle === '测试套件更新') {
            ut_promptNewSuiteDownloads(sources);
        }
    } catch (error) {
        setTimeout(() => ut_pollGmsUpdateMonitorSync(button, originalText, syncTitle, sources), 5000);
    }
}

async function ut_promptNewSuiteDownloads(sources) {
    try {
        const params = new URLSearchParams({ limit: '20' });
        (Array.isArray(sources) ? sources : []).forEach(source => params.append('source_key', source));
        const response = await fetch('/api/gms-update-monitor/artifacts/new?' + params.toString());
        const result = await response.json();
        const items = result && result.data && Array.isArray(result.data.items) ? result.data.items : [];
        if (!items.length) return;
        const first = items[0];
        const confirmed = await showConfirmDialog(
            '发现新测试套件',
            `本次扫描发现 ${items.length} 个新测试套件。是否跳转到“测试套件”页面并填入下载地址？`
        );
        if (!confirmed) return;
        switchPage('test-suites');
        setTimeout(() => {
            const input = document.getElementById('suite-download-url');
            if (input) {
                input.value = first.download_url || '';
                input.focus();
                input.select();
            }
            showToast('已填入最新套件下载地址，可点击“⬇️ 下载套件”开始下载', 'info');
        }, 120);
    } catch (error) {
        console.warn('检查新增测试套件失败:', error);
    }
}

async function downloadTestSuite() {
    const input = document.getElementById('suite-download-url');
    const url = input ? input.value.trim() : '';
    if (!url) {
        showToast('请输入测试套件下载地址', 'warning');
        return;
    }
    const button = document.getElementById('btn-download-suite');
    const progressWrap = document.getElementById('suite-download-progress');
    const statusEl = document.getElementById('suite-progress-status');
    const barEl = document.getElementById('suite-progress-bar');
    const percentEl = document.getElementById('suite-progress-percent');
    if (button) button.disabled = true;
    if (progressWrap) progressWrap.style.display = 'block';
    if (statusEl) statusEl.textContent = '准备下载...';
    if (barEl) barEl.style.width = '0%';
    if (percentEl) percentEl.textContent = '0%';
    try {
        const response = await fetch('/api/test/suites/download-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.error || result.message || '启动下载失败');
        }
        showToast(result.message || '下载任务已启动', 'success');
        if (result.task_id) {
            pollTestSuiteDownload(result.task_id, button);
        } else {
            if (statusEl) statusEl.textContent = result.message || '下载完成';
            if (barEl) barEl.style.width = '100%';
            if (percentEl) percentEl.textContent = '100%';
            if (button) button.disabled = false;
            if (typeof refreshTestSuiteBrowser === 'function') refreshTestSuiteBrowser();
        }
    } catch (error) {
        if (button) button.disabled = false;
        if (statusEl) statusEl.textContent = '下载失败';
        showToast('下载套件失败: ' + error.message, 'error');
    }
}

async function pollTestSuiteDownload(taskId, button) {
    const statusEl = document.getElementById('suite-progress-status');
    const barEl = document.getElementById('suite-progress-bar');
    const percentEl = document.getElementById('suite-progress-percent');
    try {
        const response = await fetch('/api/test/suites/download-status/' + encodeURIComponent(taskId));
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.error || '下载状态查询失败');
        }
        const task = result.task || {};
        const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
        if (statusEl) statusEl.textContent = task.message || task.status || '下载中...';
        if (barEl) barEl.style.width = progress.toFixed(0) + '%';
        if (percentEl) percentEl.textContent = progress.toFixed(0) + '%';
        if (task.status === 'completed') {
            if (button) button.disabled = false;
            showToast('测试套件下载完成', 'success');
            if (typeof refreshTestSuiteBrowser === 'function') refreshTestSuiteBrowser();
            return;
        }
        if (task.status === 'error') {
            if (button) button.disabled = false;
            showToast('测试套件下载失败: ' + (task.error || '未知错误'), 'error');
            return;
        }
        setTimeout(() => pollTestSuiteDownload(taskId, button), 1500);
    } catch (error) {
        if (button) button.disabled = false;
        showToast('下载状态查询失败: ' + error.message, 'error');
    }
}

function ut_downloadTool(filePath, title) {
    if (!filePath) {
        showToast('该工具未配置下载文件', 'warning');
        return;
    }
    const encodedPath = String(filePath).split('/').map(encodeURIComponent).join('/');
    const a = document.createElement('a');
    a.href = `/api/tools/download/${encodedPath}`;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    showToast(`正在下载: ${title}`, 'info');
}

// --- Tool CRUD ---
function ut_addNewToolToCategory(category) {
    ut_editingTool = null;
    ut_updateCategorySelect();
    document.getElementById('ut-tool-modal-title').textContent = '添加工具到 ' + category;
    document.getElementById('ut-tool-category').value = category;
    document.getElementById('ut-tool-title').value = '';
    document.getElementById('ut-tool-icon').value = '';
    document.getElementById('ut-tool-desc').value = '';
    document.getElementById('ut-tool-filepath').value = '';
    ModalManager.open('ut-tool-modal');
}

function ut_editTool(category, index) {
    const tool = ut_categorizedTools[category][index];
    ut_editingTool = { category, index };
    ut_updateCategorySelect();
    document.getElementById('ut-tool-modal-title').textContent = '✏️ 编辑工具';
    document.getElementById('ut-tool-category').value = category;
    document.getElementById('ut-tool-title').value = tool.title || '';
    document.getElementById('ut-tool-icon').value = tool.icon || '';
    document.getElementById('ut-tool-desc').value = tool.description || '';
    document.getElementById('ut-tool-filepath').value = tool.file_path || '';
    ModalManager.open('ut-tool-modal');
}

function ut_saveTool() {
    const category = document.getElementById('ut-tool-category').value.trim();
    const title = document.getElementById('ut-tool-title').value.trim();
    const icon = document.getElementById('ut-tool-icon').value.trim() || '🔧';
    const description = document.getElementById('ut-tool-desc').value.trim();
    const filePath = document.getElementById('ut-tool-filepath').value.trim();

    if (!category) { showToast('请选择分类', 'warning'); return; }
    if (!title) { showToast('请输入工具名称', 'warning'); return; }

    if (!ut_categorizedTools[category]) {
        ut_categorizedTools[category] = [];
    }

    const toolData = { icon, title, description, file_path: filePath };

    if (ut_editingTool) {
        // 编辑模式
        const oldCat = ut_editingTool.category;
        const oldIdx = ut_editingTool.index;
        if (oldCat === category) {
            ut_categorizedTools[category][oldIdx] = toolData;
        } else {
            ut_categorizedTools[oldCat].splice(oldIdx, 1);
            if (ut_categorizedTools[oldCat].length === 0) delete ut_categorizedTools[oldCat];
            ut_categorizedTools[category].push(toolData);
        }
    } else {
        ut_categorizedTools[category].push(toolData);
    }

    ut_saveCategories();
    ut_renderToolsGrid();
    const isEdit = !!ut_editingTool;
    ut_closeToolModal();
    showToast(isEdit ? '工具已更新' : '工具已添加', 'success');
}

function ut_deleteTool(category, index) {
    _deleteTool(ut_categorizedTools, category, index, ut_saveCategories, ut_renderToolsGrid, { showName: true });
}

function ut_closeToolModal() {
    _closeModal('ut-tool-modal', () => { ut_editingTool = null; });
}

// --- Category CRUD ---
function ut_showAddCategoryModal() {
    _showAddCategoryModal('ut-category-modal', { name: 'ut-category-name', icon: 'ut-category-icon' }, () => { ut_editingCategory = null; });
}

function ut_closeCategoryModal() {
    _closeModal('ut-category-modal', () => { ut_editingCategory = null; });
}

function ut_saveCategory() {
    const name = document.getElementById('ut-category-name').value.trim();
    const icon = document.getElementById('ut-category-icon').value.trim() || '📁';

    if (!name) { showToast('请输入分类名称', 'warning'); return; }
    if (ut_categorizedTools[name] && !ut_editingCategory) {
        showToast('分类已存在', 'warning'); return;
    }

    if (ut_editingCategory && ut_editingCategory !== name) {
        // 重命名
        ut_categorizedTools[name] = ut_categorizedTools[ut_editingCategory];
        ut_categoryMeta[name] = ut_categoryMeta[ut_editingCategory] || UT_DEFAULT_CATEGORIES[ut_editingCategory] || {};
        delete ut_categorizedTools[ut_editingCategory];
        delete ut_categoryMeta[ut_editingCategory];
    }
    if (!ut_categorizedTools[name]) {
        ut_categorizedTools[name] = [];
    }
    ut_categoryMeta[name] = {
        ...(ut_categoryMeta[name] || UT_DEFAULT_CATEGORIES[name] || {}),
        icon,
    };

    ut_saveCategories();
    ut_renderToolsGrid();
    ut_closeCategoryModal();
    showToast(`分类"${name}"已保存`, 'success');
}

function ut_editCategory(oldName) {
    ut_editingCategory = oldName;
    document.getElementById('ut-category-name').value = oldName;
    const catInfo = ut_getCategoryInfo(oldName);
    document.getElementById('ut-category-icon').value = catInfo.icon;
    ModalManager.open('ut-category-modal');
}

function ut_deleteCategory(categoryName) {
    _deleteCategory(ut_categorizedTools, categoryName, [(cat) => delete ut_categoryMeta[cat]], ut_saveCategories, ut_renderToolsGrid);
}

function ut_updateCategorySelect() {
    const select = document.getElementById('ut-tool-category');
    if (!select) return;
    select.innerHTML = '';
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = '-- 选择分类 --';
    select.appendChild(empty);
    Object.keys(ut_categorizedTools).forEach(cat => {
        const option = document.createElement('option');
        option.value = cat;
        option.textContent = cat;
        select.appendChild(option);
    });
}

// --- Utility Tool File Browser (reuses #file-browser-modal) ---
async function ut_browseFiles() {
    state.fileBrowser.mode = 'utility-tool';
    state.fileBrowser.targetInputId = 'ut-tool-filepath';
    state.fileBrowser.selectedFile = null;
    state.fileBrowser.currentPath = '';
    document.getElementById('file-browser-title').textContent = '选择工具文件 (tools/)';
    ModalManager.open('file-browser-modal');
    await ut_loadToolDir('');
}

async function ut_loadToolDir(subpath) {
    try {
        const r = await fetch('/api/tools/browse', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: subpath })
        });
        const data = await r.json();
        if (!data.success) { showToast('加载失败: ' + (data.error || ''), 'error'); return; }
        state.fileBrowser.currentPath = data.path || '';
        // 复用 app.js 中的 renderFileList 来渲染文件列表
        const pathDisplay = document.getElementById('file-browser-current-path');
        pathDisplay.textContent = 'tools/' + (data.path || '');
        const listContainer = document.getElementById('file-browser-list');
        if (data.files.length === 0) {
            listContainer.innerHTML = '<div class="file-browser-item" style="cursor: default; color: var(--text-muted);">空目录</div>';
            return;
        }
        listContainer.innerHTML = '';
        data.files.forEach(file => {
            const item = document.createElement('div');
            item.className = 'file-browser-item';
            item.addEventListener('click', (event) => selectFileForSelection(file.name, file.type, event));
            item.addEventListener('dblclick', () => ut_openToolFileOrDir(file.name, file.type));

            const icon = document.createElement('span');
            icon.className = 'file-browser-icon';
            icon.textContent = file.type === 'directory' ? '📁' : '📄';
            item.appendChild(icon);

            const name = document.createElement('span');
            name.className = 'file-browser-name';
            name.textContent = file.name;
            item.appendChild(name);

            if (file.type === 'file') {
                const size = document.createElement('span');
                size.style.cssText = 'color: var(--text-muted); font-size: 11px;';
                size.textContent = formatBytes(file.size, true);
                item.appendChild(size);
            }
            listContainer.appendChild(item);
        });
    } catch (e) {
        showToast('加载失败: ' + e.message, 'error');
    }
}

function ut_openToolFileOrDir(name, type) {
    if (type === 'directory') {
        const current = state.fileBrowser.currentPath;
        const newPath = current ? current + '/' + name : name;
        ut_loadToolDir(newPath);
    } else {
        selectFileForSelection(name, type);
    }
}
