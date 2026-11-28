function renderGmsPolicyEvidence(result) {
    const lines = ['来源：GMS Requirements（本地授权扫描）',
        '索引时间：' + (result.indexed_at || '未知'),
        '状态：' + ({matched: '已找到章节', not_indexed: '尚未建立条款索引', scan_failed: '最近一次规范扫描失败',
            anchor_not_indexed: '未索引指定锚点', anchor_required: '需要指定章节锚点',
            no_match: '未找到匹配章节', revision_missing: '索引缺少修订信息'}
            [result.status] || result.status || '未知'),
        '未核验上游完整性、实时版本及产品适用性。'];
    if (result.latest_scan) {
        const scan = result.latest_scan;
        lines.push('扫描批次：' + scan.run_id + ' · 获取文档：' + scan.fetched + ' · 解析记录：' + scan.parsed);
    }
    if (result.latest_scan_confirmed === false) lines.push('最近扫描未确认当前索引，请在 GMS 更新中心核查扫描结果。');
    if (result.truncated) lines.push('返回范围受限，请缩小章节范围。');
    if (result.index_consistent === false) lines.push('索引记录数量或修订不一致；仅供原文核查，请重新同步政策。');
    if (result.revision_text_scope !== 'active_markup_only') lines.push('当前索引未确认已清除删除线修订文本，请重新同步政策。');
    const block = document.createElement('div');
    const addText = (parent, tag, value) => {
        const node = document.createElement(tag);
        node.textContent = String(value ?? '');
        parent.append(node);
        return node;
    };
    const addPolicyLink = (parent, sourceUrl) => {
        if (!sourceUrl) return;
        try {
            const url = new URL(sourceUrl);
            if (url.origin !== 'https://docs.partner.android.com'
                || url.pathname !== '/gms/policies/domains/reqs') return;
            const link = addText(parent, 'a', '查看政策原文');
            link.href = url.href;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
        } catch (_) { /* Invalid retained URL. */ }
    };
    if (!result.sections?.length) lines.push('没有可返回章节，请核查采集和索引状态。');
    for (const section of result.sections || []) {
        addText(block, 'strong', section.title || '政策章节');
        for (const clause of section.clauses || []) {
            addText(block, 'p', [clause.requirement_id, clause.change_version,
                clause.change_kind].filter(Boolean).join(' · '));
            if (clause.original_text) addText(block, 'p', clause.original_text);
        }
        const requested = result.requested_android_version;
        const matchedRows = section.version_rows || [];
        if (requested && result.lookup_scope === 'low_ram_declaration') {
            addText(block, 'strong', 'Android ' + requested + '：政策表格对应要求');
            if (result.index_consistent === false) {
                addText(block, 'p', '索引完整性检查未通过，不推断认证容量结论。');
            } else if (!matchedRows.length) {
                addText(block, 'p', '未找到明确适用的版本行，不推断 RAM 准入数值。');
            } else {
                for (const row of matchedRows) {
                    const headers = row.headers.map(value => String(value).trim().toLowerCase().replace(/\s+/g, ' '));
                    for (const [header, label] of [
                        ['must be low ram', '必须声明 Low RAM'],
                        ['optionally low ram', '可选声明 Low RAM'],
                        ['not qualified for gms', '不符合 GMS 资格']
                    ]) {
                        const index = headers.indexOf(header);
                        if (index >= 0) addText(block, 'p', label + '：' + (row.values[index] || '未列明'));
                    }
                }
                addText(block, 'strong', 'Go / 非 Go 设备 RAM 要求解读（证据有限）');
                addText(block, 'p', 'Low RAM 设备：必须声明或可选声明的容量见上表；Go 产品认证要求不能仅凭此表确认。');
                const optionalValues = new Set(matchedRows.map(row => {
                    const index = row.headers.findIndex(value =>
                        String(value).trim().toLowerCase().replace(/\s+/g, ' ') === 'optionally low ram');
                    return String(index >= 0 ? (row.values[index] ?? '') : '').trim();
                }));
                const optional = optionalValues.size === 1 ? [...optionalValues][0] : '';
                if (optional && !/^(?:n\s*\/?\s*a|none|not applicable|[-—–]+)$/i.test(optional)) {
                    addText(block, 'p', '非 Low RAM 设备参考容量（非 Go 认证仍需核实）：' + optional
                        + ' 在表中属于可选声明 Low RAM 的容量，可以选择不声明 Low RAM。');
                    addText(block, 'p', '这是根据当前版本表格得出的参考结论，不是独立的非 Go 认证最低 RAM 条款；未列出的容量及产品适用条件仍需核验。');
                } else {
                    addText(block, 'p', '非 Go 设备 RAM：表格未提供唯一明确的可选 Low RAM 容量，不能据此推断 Standard 设备最低 RAM。');
                }
                addText(block, 'p', 'Low RAM 声明不等同于 Android Go 产品认证；仍需核验产品适用条件。');
            }
        }
        if (section.context_limited) {
            addText(block, 'p', '索引存在相邻政策混入，已排除无关条款和表格；章节附注待重新同步核验。');
        }
        const grouped = new Map();
        const displayRows = requested && result.lookup_scope === 'low_ram_declaration'
            ? matchedRows : (section.table_rows || []);
        for (const row of displayRows) {
            if (!Array.isArray(row.values) || !row.values.some(value => String(value ?? '').trim())) continue;
            const key = row.table_index;
            if (!grouped.has(key)) grouped.set(key, []);
            grouped.get(key).push(row);
        }
        for (const rows of grouped.values()) {
            const table = document.createElement('table');
            table.className = 'list-table';
            const header = table.createTHead().insertRow();
            for (const title of rows[0].headers || []) {
                const th = addText(header, 'th', title);
                th.scope = 'col';
            }
            const body = table.createTBody();
            for (const row of rows) {
                const tr = body.insertRow();
                for (const value of row.values || []) tr.insertCell().textContent = value;
            }
            block.append(table);
        }
        addPolicyLink(block, section.source_url);
        const details = document.createElement('details');
        addText(details, 'summary', '展开章节原文、Note、脚注及版本信息');
        addText(details, 'p', '文档修订：' + (section.source_revision || '未知'));
        if (requested && result.lookup_scope === 'low_ram_declaration') {
            for (const row of section.table_rows || []) {
                addText(details, 'p', (row.headers || []).map((header, i) =>
                    header + '：' + (row.values[i] || '')).join(' | '));
            }
        }
        const structured = section.structured_evidence;
        if (structured?.notes?.length) {
            addText(details, 'strong', '关联附注与脚注（待核验适用性）');
            for (const note of structured.notes) {
                const association = note.association === 'explicit_anchor' ? '明确锚点关联' : '相邻候选';
                addText(details, 'p', association + '：' + note.text);
            }
        }
        if (section.text) addText(details, 'p', section.text);
        else if (section.context_limited) addText(details, 'p', '原始上下文边界不明确，请查看官方章节。');
        if (section.media?.length) addText(details, 'p', '包含图片引用，图片内容尚未核验。');
        block.append(details);
    }
    if (result.related_ram) {
        const related = result.related_ram;
        const relatedDetails = document.createElement('details');
        addText(relatedDetails, 'summary', '展开其他 RAM 相关政策（功能及适用条件）');
        const definitions = related.go_product_definitions || [];
        if (definitions.length) {
            addText(block, 'strong', 'Go 产品定义（索引原文）');
            addText(block, 'p', definitions[0].text);
            addText(block, 'p', '定义不能替代 Go 专项认证条件；仍需核验。');
        } else {
            addText(block, 'p', '未找到明确的 Go 产品定义；仍需读取 Go 专项政策。');
        }
        block.append(relatedDetails);
        addText(relatedDetails, 'p', '已检索本地规范其他章节：' + related.scanned_sections + ' 个，命中 '
            + related.section_matches + ' 个章节、' + related.clause_matches + ' 个候选条款。');
        addText(relatedDetails, 'p', '以下是跨版本候选原文，需核验目标 Android 版本、首发/升级、设备及芯片条件；不能将功能触发门槛当作统一 GMS 准入门槛。');
        if (related.excluded_subsystem_sections) addText(relatedDetails, 'p', '已排除子系统内存相关章节：' + related.excluded_subsystem_sections + ' 个。');
        if (related.other_version_clauses) addText(relatedDetails, 'p', '明确涉及其他 Android 版本的候选条款：' + related.other_version_clauses + ' 条（仅供历史参考）。');
        if (related.truncated) addText(relatedDetails, 'p', '返回范围受限：部分候选或上下文未展示，请按章节或条款编号进一步读取。');
        for (const section of related.sections || []) {
            const details = document.createElement('details');
            addText(details, 'summary', section.title + '（' + section.clause_matches + ' 条候选）');
            const historical = [];
            for (const clause of section.clauses || []) {
                if (clause.version_applicability === 'other_version') {
                    historical.push(clause);
                    continue;
                }
                addText(details, 'strong', [clause.requirement_id, clause.change_version,
                    clause.change_kind].filter(Boolean).join(' · '));
                addText(details, 'p', clause.version_applicability === 'version_match'
                    ? '明确版本条件匹配；其他适用条件仍需核验。' : '版本适用性未确认。');
                if (clause.context_text) addText(details, 'p', '索引适用条件：' + clause.context_text);
                addText(details, 'p', clause.original_text);
                if (clause.scope_json && clause.scope_json !== '[]') {
                    addText(details, 'p', '索引范围：' + clause.scope_json);
                }
            }
            for (const row of section.table_rows || []) {
                addText(details, 'p', row.headers.map((header, i) =>
                    header + '：' + (row.values[i] || '')).join(' | '));
            }
            if (historical.length || section.fragments?.length) {
                const raw = document.createElement('details');
                addText(raw, 'summary', '历史版本及原始上下文（未核验）');
                for (const clause of historical) {
                    addText(raw, 'strong', clause.requirement_id);
                    if (clause.context_text) addText(raw, 'p', clause.context_text);
                    addText(raw, 'p', clause.original_text);
                }
                for (const fragment of section.fragments || []) {
                    addText(raw, 'p', '章节上下文' + (fragment.excerpt ? '（节选）' : '') + '：' + fragment.text);
                }
                details.append(raw);
            }
            addPolicyLink(details, section.source_url);
            addText(details, 'p', '文档修订：' + (section.source_revision || '未知'));
            relatedDetails.append(details);
        }
    }
    const meta = addText(block, 'p', lines.join('\n'));
    meta.className = 'agent-message-body';
    return block;
}

function bindAgentPolicyLookup(container, getSessionId) {
    container.querySelectorAll('[data-gms-policy-query]').forEach(button => {
        button.addEventListener('click', async () => {
            const output = button.parentElement.nextElementSibling;
            if (!output) return;
            const sessionId = getSessionId();
            const stillCurrent = () => output.isConnected && getSessionId() === sessionId;
            button.disabled = true;
            output.textContent = '正在读取授权索引…';
            try {
                const result = await apiCall(
                    '/api/gms-update-center/requirements/clauses?q=' + button.dataset.gmsPolicyQuery,
                    'GET', null, {silentToast: true, cache: 'no-store', shouldRetry: stillCurrent},
                );
                if (!stillCurrent()) return;
                if (!result.success) {
                    output.textContent = 'GMS 政策检索暂不可用。';
                    return;
                }
                output.replaceChildren(renderGmsPolicyEvidence(result.data || {}));
                container.scrollTop = container.scrollHeight;
            } catch (error) {
                if (!stillCurrent()) return;
                output.textContent = error.status === 401 ? '登录已失效，请重新登录。'
                    : error.elevationRequired ? '需要有效的管理员授权，请重新读取。'
                    : error.status === 403 ? '当前身份无权读取 GMS 政策。'
                    : 'GMS 政策检索失败，请重新读取。';
            } finally {
                button.disabled = false;
            }
        });
    });
}
