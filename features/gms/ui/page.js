    const TAB_STORAGE_KEY = 'gms_update_center_tab';
    const monitorTabs = ['changes', 'requirements', 'packages', 'mainline', 'artifacts'];
    const issueTabs = ['exemptions', 'patch', 'critical'];
    const allTabs = [...monitorTabs, ...issueTabs];
    let initialTab = new URLSearchParams(window.location.search).get('tab') || '';
    if (!allTabs.includes(initialTab)) {
      try { initialTab = window.sessionStorage.getItem(TAB_STORAGE_KEY) || ''; } catch (_) { initialTab = ''; }
    }
    let tab = allTabs.includes(initialTab) ? initialTab : 'changes';
    let globalSearchActive = false;
    let offset = 0;
    const limit = 100;
    let total = 0;
    let queryGeneration = 0;
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    // External bulletin / suite URLs originate in indexed upstream documents.
    // HTML escaping does not prevent javascript: or data: navigation.
    const safeExternalHref = value => {
      const raw = String(value ?? '').trim();
      if (!raw) return '';
      try {
        const url = new URL(raw, window.location.href);
        if (url.protocol === 'https:' || (url.protocol === 'http:' && url.origin === window.location.origin)) {
          return url.href;
        }
      } catch (_) {}
      return '';
    };
    const link = (rawUrl, label='打开') => {
      const href = safeExternalHref(rawUrl);
      return href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`
        : '<span class="muted">—</span>';
    };
    const suiteDownloadLink = url => {
      const href = safeExternalHref(url);
      return href ? `<a href="#" class="suite-download-link" data-url="${esc(href)}">下载</a>` : '';
    };
    const releaseMonthOf = url => {
      const m = String(url || '').match(/release-notes\/\d{4}\/([a-z-]+?)(?:\/|$)/);
      return m ? m[1].charAt(0).toUpperCase() + m[1].slice(1) : '';
    };
    const releaseCell = item => `${esc(item.release_year)} ${item.release_month ? '<span class="muted">' + esc(item.release_month) + '</span> ' : ''}<span class="muted">${esc(item.release_label)}</span>`;
    // table-layout:fixed 下未指定宽度的列独占剩余空间（放长文本列）。
    const EXEMPTION_HEADERS = ['类型', '文档小节', 'Android版本', '类别', '发布版本', '豁免ID', '测试模块', '测试用例', '来源'];
    const EXEMPTION_WIDTHS = ['64px', '100px', '180px', '104px', '224px', '112px', '240px', '', '64px'];
    const PATCH_HEADERS = ['CVE 编号', 'Mainline 模块', '版本说明', '发布版本', '来源'];
    const PATCH_WIDTHS = ['160px', '150px', '', '224px', '64px'];
    const CRITICAL_HEADERS = ['年份', '月份', '完成期限', '补丁ID', '类型', 'Android版本', '说明', '关联豁免', 'Gerrit合入', '来源'];
    const CRITICAL_BASE_WIDTHS = ['54px', '90px', '112px', '112px', '88px', '140px', '', '204px'];
    const CRITICAL_KIND_LABELS = {mandatory: '强制', strongly_recommended: '强烈推荐', recommended: '推荐', functional_unspecified: '等级未注明'};
    const CRITICAL_MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    let criticalItems = null;
    let criticalCoverage = false;
    let criticalAvailable = true;
    let criticalGerritMessage = '';
    let criticalGerritReady = false;
    let showLegacyGerritVersions = false;
    const isLegacyGerritVersion = version => {
      const major = String(version).match(/^\s*(?:Android\s+)?(\d+)(?=\s|\.|$)/i);
      return Boolean(major && Number(major[1]) < 14);
    };
    let criticalGerritRequest = 0;
    const criticalFilterIds = ['critical_year', 'critical_month', 'critical_version', 'critical_kind'];
    const GERRIT_LABELS = {merged: '已合入', partial: '部分合入', open: '评审中', missing: '未合入', unknown: '未能核对'};
    const criticalFilterState = new Map();
    let activeCriticalFilter = null;
    let criticalFilterPanel;
    let gerritByPatch = null;
    // 全局搜索（跨三类）统一聚合列规范，与 GMS Update Monitor 相同。
    const GLOBAL_HEADERS = ['类别', '标识', '摘要', '时间/发布', '操作'];
    const GLOBAL_WIDTHS = ['130px', '260px', '', '210px', '80px'];
    async function fetchJson(url) {
      const r = await fetch(url);
      const data = await r.json();
      if (!r.ok || !data.success) {
        const error = new Error(data.error || 'HTTP ' + r.status);
        error.status = r.status;
        throw error;
      }
      return data;
    }
    function sectionBadgeText(text) {
      const badge = document.getElementById('section-badge');
      badge.textContent = text;
      badge.hidden = !text;
    }
    function tableHtml(headers, rows, emptyText, widths, options={}) {
      const cols = (widths || []).map(w => `<col${w ? ' style="width:' + esc(w) + '"' : ''}>`).join('');
      const colgroup = cols ? `<colgroup>${cols}</colgroup>` : '';
      const head = headers.map((label,index) => `<th scope="col"${options.headerClasses?.[index] ? ' class="'+esc(options.headerClasses[index])+'"' : ''}>${esc(label)}</th>`).join('');
      const bodyRows = rows.length ? rows.join('') : `<tr><td colspan="${headers.length}" class="muted">${esc(emptyText)}</td></tr>`;
      return `<div class="table-split${options.wrapperClass ? ' '+esc(options.wrapperClass) : ''}"><div class="table-scroll"><table class="table-head-table table-body-table${options.tableClass ? ' '+esc(options.tableClass) : ''}"${options.minWidth ? ' style="min-width:'+Number(options.minWidth)+'px"' : ''}>${colgroup}<thead><tr>${head}</tr></thead><tbody>${bodyRows}</tbody></table></div></div>`;
    }
    const SOURCE_LABELS = {cts_downloads:'CTS',vts_downloads:'VTS / GSI',gts_downloads:'GTS',
      gms_downloads:'GMS包更新',gms_requirements:'GMS要求',mainline_preload:'Mainline包更新',gms_bulletins:'Critical_Patch'};
    const CHANGE_LABELS = {added:'新增',changed:'修改',removed:'移除',specific:'版本专属'};
    const FIELD_LABELS = {description:'说明',android_version:'Android版本',android_versions:'Android版本',patch_kind:'补丁类型',
      deadline:'完成期限',file_name:'文件名',release_name:'套件版本',partner_gerrit_tag:'Gerrit Tag',required_from:'生效日期',
      preload_version:'PRELOAD版本',partner_zip_build_id:'Partner Zip',section_title:'章节',text_excerpt:'要求摘要',
      row_text:'表格内容',detail_text:'公告正文',patch_rows_json:'补丁证据',detail_status:'详情状态',bulletin_url:'公告链接'};
    const detailRecords = [];
    let detailCopyText = '';
    const displayValue = value => value == null || value === '' ? '—' :
      typeof value === 'object' ? JSON.stringify(value,null,2) : String(value);
    function compactTime(value) {
      const date = new Date(value);
      if (!value || Number.isNaN(date.getTime())) return value || '—';
      return new Intl.DateTimeFormat('sv-SE',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date);
    }
    function changeFields(item) {
      const before=item.before || {}, after=item.after || {};
      const metadata=new Set(['content_hash','detail_fetched_at','last_seen_at','first_seen_at','updated_at','created_at','table_index','row_index']);
      return [...new Set([...Object.keys(before),...Object.keys(after)])].filter(k=>!metadata.has(k)&&JSON.stringify(before[k])!==JSON.stringify(after[k]));
    }
    function changeIdentity(item) {
      const row=item.after || item.before || {};
      return row.patch_id || row.cve_id || row.exemption_id || row.requirement_ids || row.preload_version || row.file_name || row.release_name || row.section_title || row.title || row.android_version || item.entity_key || '—';
    }
    function changeSummary(item) {
      const row=item.after || item.before || {};
      const subject=row.description || row.issue_text || row.version_note || row.release_name || row.file_name || row.preload_version || row.section_title || row.title || row.row_text || row.text_excerpt || item.entity_type || '';
      const fields=item.change_type==='changed' ? changeFields(item) : [];
      return [subject,item.change_type==='changed' ? (fields.length?'变更字段：'+fields.map(k=>FIELD_LABELS[k]||k).join('、'):'仅索引元数据变化或无可见字段差异'):''].filter(Boolean).join(' · ');
    }
    function detailButton(title,fields,label) {
      const index=detailRecords.push({title,fields})-1;
      return '<button type="button" class="table-detail-link" data-record-detail="'+index+'" title="'+esc(label)+'">'+esc(label)+'</button>';
    }
    function textDetail(title,value) {
      return value ? detailButton(title,[[title,value]],String(value)) : '<span class="muted">—</span>';
    }
    function changeDetails(item) {
      const fields=[['来源',SOURCE_LABELS[item.source_key]||item.source_key],['对象',item.entity_type],['类型',CHANGE_LABELS[item.change_type]||item.change_type],
        ['关联标识',changeIdentity(item)],['本地检测时间',compactTime(item.detected_at)],['变更来源',({baseline:'首次建立基线',reindex:'重新索引',partial:'部分覆盖补录',source_changed:'源文档变化',unclassified:'未分类'})[item.origin]||'未分类'],['说明','本地索引检测时间不代表 Google 实际发布时间。源文档变化不等于该条记录必然由官方新发布。']];
      if(item.change_type==='changed') for(const key of changeFields(item)) fields.push([FIELD_LABELS[key]||key,
        '变更前：\n'+displayValue(item.before?.[key])+'\n\n变更后：\n'+displayValue(item.after?.[key])]);
      fields.push(['原始变更前数据',item.before],['原始变更后数据',item.after]);
      return detailButton('变更详情',fields,'查看');
    }
    function changeRows(items) {
      return items.map(x=>'<tr><td title="'+esc(x.source_key)+'">'+esc(SOURCE_LABELS[x.source_key]||x.source_key)+'</td><td>'+esc(CHANGE_LABELS[x.change_type]||x.change_type)+'</td><td>'+textDetail('关联标识',changeIdentity(x))+'</td><td>'+textDetail('变更摘要',changeSummary(x))+'</td><td title="'+esc(x.detected_at)+'">'+esc(compactTime(x.detected_at))+'</td><td>'+changeDetails(x)+'</td></tr>');
    }
    const TABLE_META = {
      changes: {
        label: '变更',
        headers: ['来源','类型','关联标识','变更摘要','本地检测时间','详情'],
        widths: ['132px','72px','240px','','184px','64px'],
        rows: changeRows,
      },
      artifacts: {
        label: '测试套件更新',
        headers: ['套件', 'Android', 'API level', '套件版本', '套件文件名', '架构', '页面', '下载'],
        widths: ['120px', '160px', '80px', '30%', '', '72px', '64px', '64px'],
        rows: items => items.map(x => {
          const title = x.release_name || x.file_name || '';
          const androidText = x.suite_type === 'GTS' && x.target_platform ? x.target_platform : x.android_version;
          return `<tr><td>${esc(x.suite_type)}</td><td class="wrap">${esc(androidText)}</td><td>${esc(x.api_level)}</td><td class="wrap">${esc(title)}</td><td class="wrap">${esc(x.file_name)}</td><td>${esc(x.arch)}</td><td>${link(x.section_url, '页面')}</td><td>${suiteDownloadLink(x.download_url)}</td></tr>`;
        }),
      },
      packages: {
        label: 'GMS包更新',
        headers: ['章节', 'Android', '文件', '生效日期', 'Gerrit Tag', '链接'],
        widths: ['220px', '112px', '', '120px', '240px', '64px'],
        rows: items => items.map(x => `<tr><td>${esc(x.section)}</td><td>${esc(x.android_version)}</td><td class="wrap">${esc(x.file_name || x.description)}</td><td>${esc(formatDeadline(x.required_from))}</td><td>${esc(x.partner_gerrit_tag)}</td><td>${link(x.download_url)}</td></tr>`),
      },
      mainline: {
        label: 'Mainline包更新',
        headers: ['年份', '月份', 'PRELOAD版本', 'Partner Zip', 'CI构建', 'Notes'],
        widths: ['64px', '104px', '', '240px', '88px', '80px'],
        rows: items => items.map(x => {
          const zipText = x.partner_zip_build_id || x.partner_zip_label || '';
          return `<tr><td>${esc(x.year)}</td><td>${esc(x.month_label || x.month)}</td><td class="wrap">${link(x.notes_url, x.preload_version)}</td><td>${esc(zipText)}</td><td>${link(x.ci_build_url, zipText ? '构建' : '')}</td><td>${link(x.notes_url, '查看')}</td></tr>`;
        }),
      },
      requirements: {
        label: 'GMS要求',
        headers: ['Android版本', '类型', '章节', 'Requirement ID', '要求摘要','来源'],
        widths: ['112px', '88px', '24%', '144px', '', '64px'],
        rows: items => items.map(x => `<tr><td>${esc(x.android_version)}</td><td title="${esc(x.change_kind)}">${esc(CHANGE_LABELS[x.change_kind]||x.change_kind)}</td><td>${textDetail("章节",x.section_title)}</td><td>${textDetail("Requirement ID",x.requirement_ids)}</td><td>${textDetail("要求摘要（索引摘录）",x.text_excerpt)}</td><td>${link(x.source_url || (x.source_key === "gms_requirements" ? "https://docs.partner.android.com/gms/policies/domains/reqs" : ""),"文档")}</td></tr>`),
      },
    };
    function exemptionRows(items) {
      return items.map(item => `
        <tr>
          <td>${esc(item.issue_type)}</td>
          <td>${esc(item.product_section)}</td>
          <td>${textDetail('Android版本',item.android_versions)}</td>
          <td>${esc(item.category)}</td>
          <td>${releaseCell({...item, release_month: item.release_month || releaseMonthOf(item.source_url)})}</td>
          <td><code>${esc(item.exemption_id)}</code></td>
          <td>${textDetail('测试模块',item.test_module)}</td>
          <td>${textDetail('测试用例',item.test_case)}</td>
          <td>${link(item.source_url)}</td>
        </tr>`);
    }
    function patchRows(items) {
      return items.map(item => `
        <tr>
          <td><code>${esc(item.cve_id)}</code></td>
          <td>${esc(item.mainline_module)}</td>
          <td>${textDetail('版本说明',item.version_note)}</td>
          <td>${releaseCell({...item, release_month: item.release_month || releaseMonthOf(item.source_url)})}</td>
          <td>${link(item.source_url)}</td>
        </tr>`);
    }
    function criticalDescription(item) {
      const excerpt = item.detail_excerpt
        ? `<details><summary>详情正文摘录</summary><div class="wrap">${esc(item.detail_excerpt)}</div></details>` : '';
      const pending = ['not_scanned', 'unavailable', 'stale'].includes(item.detail_status)
        ? '<div class="muted">详情未完整同步，请更新资料后核实。</div>' : '';
      return `${esc(item.description)}${excerpt}${pending}`;
    }
    function criticalAndroidVersions(value) {
      const versions = String(value ?? '').split(/[,，]/).map(version =>
        version.trim().replace(/^Android\s+(?=\d)/i, '').replace(/\s+/g, ' ')
      ).filter(Boolean);
      return [...new Set(versions)].join(', ');
    }
    function criticalKey(item) { return String(item.item_key || item.patch_id); }
    function versionList(value) {
      return criticalAndroidVersions(value).split(',').map(v => v.trim()).filter(Boolean);
    }
    function sortedVersions(values) {
      return [...new Set(values)].sort((a,b) => a.localeCompare(b, 'en', {numeric:true}));
    }
    function criticalVersions(item) {
      return sortedVersions([...versionList(item.android_versions),
        ...((gerritByPatch && gerritByPatch[criticalKey(item)] || {}).versions || []).map(v => v.version).filter(Boolean)]);
    }
    function formatDeadline(value) {
      const raw = String(value || '').trim();
      const m = raw.match(/^([A-Za-z]+) (\d{1,2}), (\d{4})$/);
      const month = m && CRITICAL_MONTHS.indexOf(m[1]) + 1;
      return month ? m[3] + '-' + String(month).padStart(2,'0') + '-' + m[2].padStart(2,'0') : raw || '—';
    }
    function exemptionSummary(item) {
      const groups = [...new Set((item.exemptions || []).map(e => [e.issue_type,e.test_module].filter(Boolean).join(' ')))];
      if (!groups.length) return '<span class="muted">—</span>';
      return `<button type="button" class="critical-text-button" data-critical-detail="${esc(criticalKey(item))}" title="${esc(groups.join('；'))}">${esc(groups[0])}${groups.length > 1 ? ' +' + (groups.length-1) : ''}</button>`;
    }
    function versionGerritStatus(item, version) {
      const row = gerritByPatch && gerritByPatch[criticalKey(item)];
      const evidence = row && (row.versions || []).find(v => v.version === version);
      return evidence && GERRIT_LABELS[evidence.status] ? evidence.status : 'unknown';
    }
    function matrixCell(item, version) {
      const result = gerritByPatch && gerritByPatch[criticalKey(item)];
      const found = result && (result.versions || []).find(v => v.version === version);
      if (!criticalVersions(item).includes(version)) return '<span class="muted" title="不适用">—</span>';
      if (!criticalGerritReady) return '<span class="muted">待核对</span>';
      const status = versionGerritStatus(item, version);
      const count = found && found.upstream_total ? `${found.upstream_merged}/${found.upstream_total}` : '';
      return `<span class="gerrit-badge gerrit-${esc(status)}">${esc(GERRIT_LABELS[status])}</span>${count ? '<div class="muted">' + esc(count) + '</div>' : ''}`;
    }
    function matrixOverall(item) {
      const row = gerritByPatch && gerritByPatch[criticalKey(item)];
      if (!criticalGerritReady) return '<span class="muted">待核对</span>';
      const status = row && GERRIT_LABELS[row.status] ? row.status : 'unknown';
      const count = row && row.upstream_total ? `${row.upstream_merged}/${row.upstream_total}` : '';
      return `<button type="button" class="critical-text-button gerrit-badge gerrit-${esc(status)}" data-critical-detail="${esc(criticalKey(item))}" title="查看完整核对证据">${esc(GERRIT_LABELS[status])}${row && row.truncated ? ' ※' : ''}</button>${count ? '<div class="muted">' + esc(count) + '</div>' : ''}`;
    }
    function criticalRows(items, versions) {
      return items.map(item => {
        const key = criticalKey(item);
        const kind = CRITICAL_KIND_LABELS[item.patch_kind] || item.patch_kind || '未注明';
        const version = criticalAndroidVersions(item.android_versions);
        return `<tr>
          <td>${esc(item.year)}</td>
          <td>${esc(item.release_month || item.bulletin_month || '')}</td>
          <td title="${esc(item.deadline)}">${esc(formatDeadline(item.deadline))}</td>
          <td><code>${esc(item.patch_id || '未注明')}</code></td>
          <td class="critical-kind" title="${esc(item.patch_kind)}">${esc(kind)}</td>
          <td class="critical-version" title="${esc(version)}">${esc(version)}</td>
          <td class="critical-description" title="${esc(item.description)}"><button type="button" class="critical-text-button" data-critical-detail="${esc(key)}">${esc(item.description)} <span class="muted">›</span></button>${['not_scanned','unavailable','stale'].includes(item.detail_status) ? '<div class="muted">详情未完整同步</div>' : ''}</td>
          <td class="critical-exemptions">${exemptionSummary(item)}</td>
          <td class="gerrit-cell critical-matrix-status" data-patch="${esc(key)}"><div class="critical-status-stack">${matrixOverall(item)}</div></td>
          ${versions.map(v => `<td class="critical-matrix-status" data-version="${esc(v)}"><div class="critical-status-stack">${matrixCell(item,v)}</div></td>`).join('')}
          <td class="critical-source">${link(item.bulletin_url, '打开')}</td>
        </tr>`;
      });
    }
    function criticalTable(items, versions) {
      const widths = [...CRITICAL_BASE_WIDTHS, '92px', ...versions.map(() => '84px'), '56px'];
      const headers = [...CRITICAL_HEADERS.slice(0,9), ...versions, CRITICAL_HEADERS[9]];
      const headerClasses = [...Array(8).fill(''), 'critical-matrix-group',
        ...versions.map(() => 'critical-matrix-group critical-version-label'), ''];
      return tableHtml(headers, criticalRows(items, versions), '无匹配记录', widths, {
        wrapperClass:'critical-matrix', tableClass:'critical-matrix-table',
        minWidth:1120+84*versions.length, headerClasses,
      });
    }
    function showRecordDetails(index) {
      const record=detailRecords[index];
      if(!record) return;
      detailCopyText=record.fields.map(([label,value])=>label+'：\n'+displayValue(value)).join('\n\n');
      document.getElementById('critical-detail-title').textContent=record.title;
      document.getElementById('critical-detail-body').innerHTML=record.fields.map(([label,value])=>'<h3>'+esc(label)+'</h3><div class="critical-evidence-record">'+esc(displayValue(value))+'</div>').join('');
      document.getElementById('critical-detail-copy').hidden=false;
      document.getElementById('critical-detail-dialog').showModal();
    }
    function showCriticalDetails(key) {
      const item = (criticalItems || []).find(x => criticalKey(x) === key);
      if (!item) return;
      const result = gerritByPatch && gerritByPatch[key];
      const exemptions = (item.exemptions || []).map(e => `<div class="critical-evidence-record"><strong>${esc(e.issue_type)} ${esc(e.test_module)}</strong><div>${esc(e.test_case || '未注明用例')}</div><div class="muted">Bug ID：${esc(e.exemption_id || '未注明')} · Android版本：${esc(e.android_versions || '未提供')}${e.source_url ? ' · ' + link(e.source_url, '原始公告') : ''}</div></div>`).join('');
      document.getElementById('critical-detail-copy').hidden = true;
      document.getElementById('critical-detail-title').textContent = 'Critical_Patch · ' + (item.patch_id || '历史公告小节');
      document.getElementById('critical-detail-body').innerHTML = `<h3>公告内容</h3><p>${esc(item.description)}</p><p class="muted">${esc(item.year)} ${esc(item.bulletin_month)} · ${esc(criticalAndroidVersions(item.android_versions))} · ${link(item.bulletin_url,'原始公告')}</p><div class="critical-evidence-record">${esc(item.detail_text || item.detail_excerpt || '暂无正文证据')}</div><h3>关联豁免</h3>${exemptions || '<p class="muted">无关联记录</p>'}${exemptions ? '<p class="muted">Bug ID 关联不代表豁免仍适用，需核对实际版本及官方公告。</p>' : ''}<h3>Gerrit 核对证据</h3>${gerritCell(result)}`;
      document.getElementById('critical-detail-dialog').showModal();
    }
    function closeCriticalDetails() {
      document.getElementById('critical-detail-dialog').close();
    }
    document.getElementById('tables-area').addEventListener('click', event => {
      const control = event.target.closest('[data-critical-detail]');
      if (control) showCriticalDetails(control.dataset.criticalDetail);
      const record=event.target.closest('[data-record-detail]');
      if(record) showRecordDetails(Number(record.dataset.recordDetail));
    });
    async function issueSummary() {
      const data = await fetchJson('/api/gms-update-center/exemptions/summary');
      const parts = [`总数 ${data.total}`];
      for (const row of (data.by_type || [])) parts.push(`${row.issue_type}/${row.product_section}: ${row.count}`);
      if (data.patch_total) parts.push(`安全补丁(CVE): ${data.patch_total}`);
      if (data.year_range) {
        const yr = data.year_range;
        parts.push(`数据覆盖: ${yr.min_year === yr.max_year ? yr.min_year : yr.min_year + '-' + yr.max_year}`);
      }
      if (data.last_sync) {
        const ls = data.last_sync;
        const syncTime = ls.finished_at ? new Date(ls.finished_at).toLocaleString('zh-CN') : '未知';
        const found = ` ${ls.issues_found || 0}条${ls.patches_found ? ', CVE补丁 ' + ls.patches_found : ''}`;
        parts.push(`最近同步: ${syncTime}, ${ls.mode === 'full' ? '全量' : '增量'}${ls.pages_scanned ? ', ' + ls.pages_scanned + '页' : ''},${found}`);
      }
      return parts.join(' | ');
    }
    async function monitorSummary() {
      const data = await fetchJson('/api/gms-update-center/summary');
      const d = data.data;
      const parts = [];
      if (d.last_run) parts.push(`最近扫描: ${new Date(d.last_run.finished_at).toLocaleString('zh-CN')}，变更 ${d.last_run.changes_total}`);
      const suiteTotal = (d.sources || []).reduce((sum, s) => sum + (s.source_key === 'gms_requirements' ? 0 : (s.artifacts || 0) + (s.packages || 0)), 0);
      const reqTotal = (d.sources || []).reduce((sum, s) => sum + (s.source_key === 'gms_requirements' ? (s.requirement_sections || 0) : 0), 0);
      if (suiteTotal) parts.push(`测试/GMS包: ${suiteTotal}`);
      if (reqTotal) parts.push(`认证章节: ${reqTotal}`);
      if (Array.isArray(d.requirement_version_summary) && d.requirement_version_summary.length) {
        const versionTotals = {};
        d.requirement_version_summary.forEach(x => { versionTotals[x.android_version] = (versionTotals[x.android_version] || 0) + x.count; });
        parts.push('新版本要求: ' + Object.entries(versionTotals).map(([k, v]) => `${k} ${v}`).join(', '));
      }
      return parts.join(' | ') || '暂无扫描数据';
    }
    let summaryGeneration = 0;
    async function loadSummary() {
      const generation = ++summaryGeneration;
      const el = document.getElementById('summary');
      try {
        const value = await (issueTabs.includes(tab) && tab !== 'critical' ? issueSummary() : monitorSummary());
        if (generation === summaryGeneration) { el.textContent = value; el.title = value; }
      } catch (err) {
        if (generation === summaryGeneration) el.textContent = '扫描摘要暂不可用';
      }
    }
    function rememberTab() {
      try {
        window.sessionStorage.setItem(TAB_STORAGE_KEY, tab);
      } catch (_) {}
    }
    function setTab(next) {
      if (!allTabs.includes(next)) return;
      if (tab !== next) {
        document.getElementById('tables-area').innerHTML = '<span class="muted">加载中…</span>';
        sectionBadgeText('');
      }
      tab = next;
      offset = 0;
      total = 0;
      document.getElementById('source_key').value = '';
      document.getElementById('type_filter').value = '';
      for (const id of ['monitor_version','requirement_kind','change_kind','cve_year','cve_module']) document.getElementById(id).value = '';
      globalSearchActive = false;
      document.getElementById('q').value = '';
      rememberTab();
      const url = new URL(window.location.href);
      url.searchParams.set('tab', tab);
      window.history.replaceState({}, '', url.toString());
      document.querySelectorAll('.tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
      syncFilterVisibility();
      loadAll(true);
    }
    function syncFilterVisibility() {
      closeCriticalFilter();
      const groups = [
        [['issue_type', 'product_section', 'exemption_id'], tab === 'exemptions'],
        [['gerrit_status', 'gerrit-refresh', 'critical-legacy-toggle', ...criticalFilterIds], tab === 'critical'],
        [['source_key'], ['changes','artifacts'].includes(tab)],
        [['type_filter'], ['changes','artifacts','mainline'].includes(tab)],
        [['monitor_version'], ['requirements','packages'].includes(tab)],
        [['requirement_kind'], tab === 'requirements'],
        [['change_kind'], tab === 'changes'],
        [['cve_year','cve_module'], tab === 'patch'],
      ];
      groups.forEach(([ids,visible])=>ids.forEach(id=>document.getElementById(id).style.display=visible?'':'none'));
      const type = document.getElementById('type_filter');
      type.placeholder = {changes:'对象类型',artifacts:'套件类型/Android版本',packages:'Android版本',mainline:'年份',requirements:'版本/变更类型'}[tab] || '类型/版本';
      document.querySelectorAll('header [data-click="startSync"]').forEach(btn=>{
        btn.title = (tab === 'changes' ? '扫描全部七个数据源' : '扫描当前分类对应数据源') + '（Mainline 发布文档统一生成包、豁免与CVE）';
      });
    }
    function gerritChangeLabel(change) {
      const status = change.status === 'MERGED' ? '已合入' : ((change.status === 'NEW' || change.status === 'OPEN') ? '评审中' : (change.status || ''));
      return [change.number ? '#' + change.number : '', change.branch || '', status].filter(Boolean).join(' ');
    }
    function gerritChangeLinks(changes) {
      return (Array.isArray(changes) ? changes : []).map(change => {
        const label = gerritChangeLabel(change || {});
        const href = safeExternalHref(change && change.url);
        return href ? `<div><a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a></div>` : `<div>${esc(label)}</div>`;
      }).join('');
    }
    function gerritCell(row) {
      if (!row || !GERRIT_LABELS[row.status]) return '<span class="muted">尚无核对证据</span>';
      const parts = [`<span class="gerrit-badge gerrit-${esc(row.status)}">${esc(GERRIT_LABELS[row.status])}</span>`];
      const versions = Array.isArray(row.versions) ? row.versions : [];
      if (versions.length) {
        versions.forEach(version => {
          const state = GERRIT_LABELS[version.status] ? version.status : 'unknown';
          const count = version.upstream_total ? `${version.upstream_merged}/${version.upstream_total}` : '—';
          parts.push(`<div class="gerrit-version-result"><span>${esc(version.version)}</span> <span class="gerrit-badge gerrit-${esc(state)}">${esc(GERRIT_LABELS[state])}</span> <span class="muted">${esc(count)}</span></div>`);
        });
      } else if (row.upstream_total) {
        parts.push(`<div class="muted">${esc(row.upstream_merged)}/${esc(row.upstream_total)} 个公告补丁</div>`);
      }
      if (row.branch_coverage_complete === false) parts.push('<div class="muted">尚无同一分支包含全部补丁的证据</div>');
      if (row.truncated) parts.push('<div class="muted">结果可能不完整</div>');
      const upstreams = Array.isArray(row.upstreams) ? row.upstreams : [];
      if (upstreams.length) {
        const detail = [];
        upstreams.forEach(item => {
          const label = GERRIT_LABELS[item.status] || '未能核对';
          detail.push(`<div class="gerrit-badge gerrit-${esc(item.status || 'unknown')}">${esc(label)}</div> <span class="muted">${esc([item.version, item.project].filter(Boolean).join(' · '))}</span>`);
          if (item.subject) detail.push(`<div class="muted">${esc(item.subject)}</div>`);
          detail.push(gerritChangeLinks(item.changes));
        });
        parts.push(`<div class="critical-evidence-record">${detail.join('')}</div>`);
        return parts.join('');
      }
      parts.push(gerritChangeLinks(row.changes));
      return parts.join('');
    }
    function paintGerritBadge(message) {
      if (tab !== 'critical' || document.getElementById('q').value.trim()) return;
      sectionBadgeText([
        !criticalAvailable ? 'Critical_Patch 数据源未扫描' : !criticalCoverage ? 'Critical_Patch 详情未完整同步' : '',
        message || '',
      ].filter(Boolean).join(' · '));
    }
    function selectedCriticalValues(id) {
      return [...(criticalFilterState.get(id)?.selected || [])];
    }
    function updateCriticalFilterButton(id) {
      const state = criticalFilterState.get(id);
      const button = document.getElementById(id);
      const labels = state.options.filter(option => state.selected.has(option.value)).map(option => option.label);
      button.textContent = labels.length === 0 ? state.title : labels.length === 1 ? labels[0] : '已选 ' + labels.length + ' 项';
      button.setAttribute('aria-label', state.name + '：' + (labels.join('、') || state.title));
    }
    function filterOptions(id, values, title, label = x => x) {
      const previous = criticalFilterState.get(id);
      const options = values.map(value => ({value, label: label(value)}));
      const selected = new Set(values.filter(value => previous?.selected.has(value)));
      if (activeCriticalFilter === id && JSON.stringify(previous?.options) !== JSON.stringify(options)) closeCriticalFilter();
      criticalFilterState.set(id, {
        name: previous?.name || document.getElementById(id).getAttribute('aria-label'),
        title, options, selected,
      });
      updateCriticalFilterButton(id);
    }
    function closeCriticalFilter(restoreFocus=false) {
      if (!activeCriticalFilter) return;
      const button = document.getElementById(activeCriticalFilter);
      activeCriticalFilter = null;
      criticalFilterPanel.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      if (restoreFocus && !button.disabled) button.focus();
    }
    function openCriticalFilter(id) {
      const button = document.getElementById(id);
      if (button.disabled) return;
      if (activeCriticalFilter === id) { closeCriticalFilter(); return; }
      closeCriticalFilter();
      const state = criticalFilterState.get(id);
      if (!state) return;
      const panel = criticalFilterPanel;
      panel.replaceChildren();
      panel.setAttribute('aria-label', state.name);
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'critical-filter-clear';
      clear.textContent = state.title;
      const checkboxes = [];
      const sync = () => {
        const current = criticalFilterState.get(id);
        clear.setAttribute('aria-pressed', String(current.selected.size === 0));
        checkboxes.forEach(input => { input.checked = current.selected.has(input.value); });
        updateCriticalFilterButton(id);
      };
      clear.addEventListener('click', () => {
        criticalFilterState.get(id).selected.clear();
        sync();
        setGerritFilter();
      });
      panel.append(clear);
      state.options.forEach(option => {
        const row = document.createElement('label');
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.value = option.value;
        input.addEventListener('change', () => {
          const selected = criticalFilterState.get(id).selected;
          if (input.checked) selected.add(option.value);
          else selected.delete(option.value);
          sync();
          setGerritFilter();
        });
        checkboxes.push(input);
        const label = document.createElement('span');
        label.textContent = option.label;
        row.append(input, label);
        panel.append(row);
      });
      sync();
      activeCriticalFilter = id;
      button.setAttribute('aria-expanded', 'true');
      const rect = button.getBoundingClientRect();
      panel.style.width = rect.width + 'px';
      panel.style.left = rect.left + 'px';
      panel.hidden = false;
      const height = panel.offsetHeight;
      panel.style.top = Math.max(8, Math.min(rect.bottom + 4 + height <= innerHeight - 8 ? rect.bottom + 4 : rect.top - height - 4, innerHeight - height - 8)) + 'px';
      clear.focus();
    }
    function initCriticalFilters() {
      criticalFilterPanel = document.createElement('div');
      criticalFilterPanel.id = 'critical-filter-panel';
      criticalFilterPanel.className = 'critical-filter-panel';
      criticalFilterPanel.hidden = true;
      criticalFilterPanel.setAttribute('role', 'group');
      document.body.append(criticalFilterPanel);
      for (const id of criticalFilterIds) {
        const button = document.getElementById(id);
        filterOptions(id, [], button.textContent);
      }
      filterOptions('gerrit_status', ['missing','partial','open','merged','unknown'], '全部状态', value => GERRIT_LABELS[value]);
      for (const id of [...criticalFilterIds, 'gerrit_status']) {
        document.getElementById(id).addEventListener('click', () => openCriticalFilter(id));
      }
      const outside = event => {
        if (activeCriticalFilter && !criticalFilterPanel.contains(event.target) &&
            !document.getElementById(activeCriticalFilter).contains(event.target)) closeCriticalFilter();
      };
      document.addEventListener('pointerdown', outside);
      document.addEventListener('focusin', outside);
      document.addEventListener('keydown', event => {
        if (activeCriticalFilter && event.key === 'Escape') { event.preventDefault(); closeCriticalFilter(true); }
      });
      window.addEventListener('resize', () => closeCriticalFilter());
    }
    function populateCriticalFilters() {
      const rows = criticalItems || [];
      filterOptions('critical_year', [...new Set(rows.map(r=>String(r.year)).filter(Boolean))].sort().reverse(), '全部年份');
      filterOptions('critical_month', CRITICAL_MONTHS.filter(m=>rows.some(r=>r.bulletin_month===m)), '全部月份');
      const versions = rows.flatMap(criticalVersions);
      // Keep explicitly selected versions until the new Gerrit result has arrived.
      if (!criticalGerritReady) versions.push(...selectedCriticalValues('critical_version'));
      filterOptions('critical_version', sortedVersions(versions), '全部版本');
      filterOptions('critical_kind', [...new Set(rows.map(r=>r.patch_kind).filter(Boolean))], '全部类型', v=>CRITICAL_KIND_LABELS[v]||v);
    }
    function renderCritical(preserveVertical=false) {
      if (tab !== 'critical' || !criticalItems || document.getElementById('q').value.trim()) return;
      const [years, months, selectedVersions, kinds] = criticalFilterIds.map(selectedCriticalValues);
      const statusControl = document.getElementById('gerrit_status');
      const statuses = selectedCriticalValues('gerrit_status');
      statusControl.title = selectedVersions.length ? '按所选适用 Android 版本的合入状态筛选（任一匹配）' : '按总体合入状态筛选';
      statusControl.disabled = !criticalGerritReady;
      if (statusControl.disabled && activeCriticalFilter === 'gerrit_status') closeCriticalFilter();
      const matches = (values, value) => !values.length || values.includes(value);
      const filtered = criticalItems.filter(item => {
        if (!matches(years, String(item.year)) || !matches(months, item.bulletin_month) || !matches(kinds, item.patch_kind)) return false;
        const applicable = selectedVersions.length ? criticalVersions(item).filter(version => selectedVersions.includes(version)) : [];
        if (selectedVersions.length && !applicable.length) return false;
        if (!statuses.length || !criticalGerritReady) return true;
        if (selectedVersions.length) return applicable.some(version => statuses.includes(versionGerritStatus(item, version)));
        const match = gerritByPatch && gerritByPatch[criticalKey(item)];
        return statuses.includes(match && GERRIT_LABELS[match.status] ? match.status : 'unknown');
      });
      total = filtered.length;
      offset = Math.min(offset, Math.max(0, Math.floor((total-1)/limit)*limit));
      const pageItems = filtered.slice(offset,offset+limit);
      const allVersions = sortedVersions(criticalItems.flatMap(item => versionList(item.android_versions)));
      const hiddenVersions = allVersions.filter(isLegacyGerritVersion);
      const versions = selectedVersions.length ? sortedVersions(selectedVersions) : allVersions.filter(v => showLegacyGerritVersions || !isLegacyGerritVersion(v));
      const legacyToggle = document.getElementById('critical-legacy-toggle');
      legacyToggle.textContent = showLegacyGerritVersions ? '隐藏历史版本' : '显示历史版本';
      legacyToggle.setAttribute('aria-pressed', String(showLegacyGerritVersions));
      legacyToggle.title = selectedVersions.length ? '已按指定版本展示；清除版本筛选后可切换历史版本列' :
        (showLegacyGerritVersions ? '隐藏 Android 14 以下版本列，不改变合入统计' :
          '显示 Android 14 以下版本列（全部公告 ' + hiddenVersions.length + ' 列）');
      legacyToggle.disabled = Boolean(selectedVersions.length);
      const area = document.getElementById('tables-area');
      const previousScroll = area.querySelector('.critical-matrix .table-scroll');
      const horizontal = previousScroll ? previousScroll.scrollLeft : 0;
      const vertical = preserveVertical && previousScroll ? previousScroll.scrollTop : 0;
      area.innerHTML = criticalTable(pageItems,versions);
      const scroll = area.querySelector('.critical-matrix .table-scroll');
      scroll.scrollLeft = horizontal;
      scroll.scrollTop = vertical;
      updatePager();
      paintGerritBadge(criticalGerritMessage);
    }
    function applyGerrit(data) {
      if (tab !== 'critical' || !data || Array.isArray(data.items) || data.configured === undefined) return;
      gerritByPatch = data.items || {};
      criticalGerritReady = true;
      criticalGerritMessage = data.error || (data.configured ? '' : 'Gerrit 未配置');
      populateCriticalFilters();
      renderCritical(true);
    }
    function setGerritFilter() { offset=0; renderCritical(); }
    function setGerritBusy(busy) {
      const button = document.getElementById('gerrit-refresh');
      button.textContent = busy ? '核对中…' : '重新核对';
      button.disabled = busy;
      button.setAttribute('aria-busy', String(busy));
    }
    async function loadGerrit(generation, refresh=false) {
      const request = ++criticalGerritRequest;
      setGerritBusy(true);
      try {
        const params = new URLSearchParams({q: '', refresh:refresh?'1':'0'});
        const data = await fetchJson('/api/gms-update-center/critical-patches/gerrit?' + params.toString());
        if (generation !== queryGeneration || request !== criticalGerritRequest || tab !== 'critical') return;
        if (!data || Array.isArray(data.items) || data.configured === undefined) throw new Error('Gerrit 核对结果格式异常');
        applyGerrit(data);
      } catch (err) {
        if (generation !== queryGeneration || request !== criticalGerritRequest || tab !== 'critical') return;
        criticalGerritMessage = err && err.message ? err.message : 'Gerrit 核对失败';
        if (err.status === 401 || err.status === 403) {
          gerritByPatch = {};
          criticalGerritReady = true;
          populateCriticalFilters();
          renderCritical(true);
        } else if (criticalGerritReady && gerritByPatch) {
          criticalGerritMessage += '（保留上次核对结果）';
          paintGerritBadge(criticalGerritMessage);
        } else {
          criticalGerritReady = true;
          gerritByPatch = {};
          populateCriticalFilters();
          renderCritical(true);
        }
      } finally {
        if (generation === queryGeneration && request === criticalGerritRequest) setGerritBusy(false);
      }
    }
    async function refreshCriticalGerrit() {
      if (tab !== 'critical' || document.getElementById('q').value.trim() || !criticalItems ||
          document.getElementById('gerrit-refresh').disabled) return;
      await loadGerrit(queryGeneration,true);
    }
    async function loadCriticalIndex(generation) {
      const items = [];
      let sourceTotal = null;
      let data;
      do {
        data = await fetchJson('/api/gms-update-center/critical-patches?' + new URLSearchParams({limit,offset:items.length}));
        if (generation !== queryGeneration || tab !== 'critical' || document.getElementById('q').value.trim()) return;
        if (sourceTotal !== null && sourceTotal !== data.total) throw new Error('扫描数据已变化，请重新查询');
        sourceTotal = data.total;
        if (!data.items.length && items.length < sourceTotal) throw new Error('分页记录不完整，请重新查询');
        items.push(...data.items);
      } while (items.length < sourceTotal);
      if (items.length !== sourceTotal || new Set(items.map(criticalKey)).size !== items.length) throw new Error('分页记录不一致，请重新查询');
      const sameSnapshot = criticalItems && JSON.stringify(criticalItems) === JSON.stringify(items)
        && criticalCoverage === (data.coverage_complete !== false)
        && criticalAvailable === (data.available !== false);
      criticalItems = items;
      criticalCoverage = data.coverage_complete !== false;
      criticalAvailable = data.available !== false;
      if (!sameSnapshot) {
        gerritByPatch = null;
        criticalGerritReady = false;
        criticalGerritMessage = '';
      }
      populateCriticalFilters();
      renderCritical(Boolean(sameSnapshot));
      if (items.length) loadGerrit(generation);
      else { criticalGerritReady = true; setGerritBusy(false); renderCritical(); }
    }
    // 全局搜索：关键字非空时并行查询三类内容聚合为统一表。
    const MONITOR_SEARCH_SOURCES = [
      {key: 'changes', label: '变更', endpoint: '/api/gms-update-center/changes'},
      {key: 'artifacts', label: '测试套件更新', endpoint: '/api/gms-update-center/artifacts'},
      {key: 'packages', label: 'GMS包更新', endpoint: '/api/gms-update-center/packages'},
      {key: 'mainline', label: 'Mainline包更新', endpoint: '/api/gms-update-center/mainline'},
      {key: 'requirements', label: 'GMS要求', endpoint: '/api/gms-update-center/requirements'}
    ];
    function monitorGlobalRow(kind, x) {
      if (kind === 'changes') {
        return `<tr><td>变更</td><td>${esc(SOURCE_LABELS[x.source_key]||x.source_key)} · ${esc(CHANGE_LABELS[x.change_type]||x.change_type)}</td><td>${textDetail("变更摘要",changeSummary(x))}</td><td>${esc(compactTime(x.detected_at))}</td><td>${changeDetails(x)}</td></tr>`;
      }
      if (kind === 'artifacts') {
        return `<tr><td>测试套件更新</td><td>${esc(x.suite_type)} · ${esc(x.android_version)}</td><td class="wrap">${esc(x.release_name || x.file_name || '')}（API ${esc(x.api_level ?? '')}，${esc(x.file_name || '')}）</td><td></td><td>${link(x.section_url, '页面')} ${suiteDownloadLink(x.download_url)}</td></tr>`;
      }
      if (kind === 'packages') {
        return `<tr><td>GMS包更新</td><td>${esc(x.section)} · ${esc(x.android_version)}</td><td class="wrap">${esc(x.file_name || x.description || '')}（Gerrit: ${esc(x.partner_gerrit_tag || '')}）</td><td>${esc(x.required_from)}</td><td>${link(x.download_url)}</td></tr>`;
      }
      if (kind === 'mainline') {
        return `<tr><td>Mainline包更新</td><td>${esc(x.year)} · ${esc(x.month_label || x.month)}</td><td class="wrap">PRELOAD ${esc(x.preload_version || '')}（${esc(x.partner_zip_build_id || x.partner_zip_label || '')}）</td><td></td><td>${link(x.notes_url, '查看')}</td></tr>`;
      }
      return `<tr><td>GMS要求</td><td>${esc(x.android_version)} · ${esc(x.change_kind)}</td><td class="wrap">${esc(x.section_title || '')} ${esc(x.requirement_ids || '')}：${esc(x.text_excerpt || '').slice(0, 300)}</td><td></td><td></td></tr>`;
    }
    const GLOBAL_SEARCH_SOURCES = [
      ...MONITOR_SEARCH_SOURCES,
      {key: 'exemptions', label: '豁免条目', endpoint: '/api/gms-update-center/exemptions'},
      {key: 'patch', label: '安全补丁 (CVE)', endpoint: '/api/gms-update-center/security-patches'},
      {key: 'critical', label: 'Critical_Patch 补丁', endpoint: '/api/gms-update-center/critical-patches'},
    ];
    function globalRow(kind, x) {
      if (monitorTabs.includes(kind)) return monitorGlobalRow(kind, x);
      if (kind === 'exemptions') {
        return `<tr><td>豁免条目</td><td>${esc(x.issue_type)} · ${esc(x.product_section)} · <code>${esc(x.exemption_id)}</code></td><td class="wrap"><code>${esc(x.test_module)}</code> / <code>${esc(x.test_case)}</code> · ${esc(x.category)} · ${esc(x.android_versions)}</td><td>${releaseCell({...x, release_month: x.release_month || releaseMonthOf(x.source_url)})}</td><td>${link(x.source_url)}</td></tr>`;
      }
      if (kind === 'patch') {
        return `<tr><td>安全补丁 (CVE)</td><td><code>${esc(x.cve_id)}</code></td><td class="wrap">${esc(x.mainline_module)} · ${esc(x.version_note)}</td><td>${releaseCell({...x, release_month: x.release_month || releaseMonthOf(x.source_url)})}</td><td>${link(x.source_url)}</td></tr>`;
      }
      return `<tr><td>Critical_Patch</td><td><code>${esc(x.patch_id)}</code> · ${esc(x.patch_kind)} · ${esc(criticalAndroidVersions(x.android_versions))}</td><td class="wrap">${criticalDescription(x)}${x.deadline ? '（期限 ' + esc(x.deadline) + '）' : ''}</td><td>${esc(x.release_month || x.bulletin_month || '')}</td><td>${link(x.bulletin_url)}</td></tr>`;
    }
    async function searchAll(q) {
      const generation = ++queryGeneration;
      setGerritBusy(false);
      globalSearchActive = true;
      const results = await Promise.all(GLOBAL_SEARCH_SOURCES.map(async src => {
        const params = new URLSearchParams({q, limit, offset: 0});
        try {
          const data = await fetchJson(src.endpoint + '?' + params.toString());
          const isMonitor = monitorTabs.includes(src.key);
          const items = isMonitor ? data.data?.items : data.items;
          if (!Array.isArray(items)) throw new Error('invalid search response');
          return {src, items, total: Number(isMonitor ? (data.meta?.total ?? items.length) : (data.total ?? items.length)),
                  available: data.available !== false, complete: data.coverage_complete !== false};
        } catch (_) {
          return {src, items: [], total: 0, available: false, complete: false};
        }
      }));
      if (generation !== queryGeneration || document.getElementById('q').value.trim() !== q) return;
      globalSearchActive = true;
      detailRecords.length=0;
      const rows = [];
      const counts = [];
      const unavailable = [];
      let totalAll = 0;
      results.forEach(({src, items, total: srcTotal, available, complete}) => {
        if (!available) unavailable.push(src.label);
        counts.push(`${src.label} ${available ? srcTotal : '查询不可用'}${available && !complete ? '（详情未完整同步）' : ''}`);
        if (available) totalAll += srcTotal;
        if (!available || !items.length) return;
        rows.push(`<tr class="group-row"><td colspan="5">${esc(src.label)} · 命中 ${srcTotal} 条${items.length < srcTotal ? `（显示前 ${items.length} 条）` : ''}</td></tr>`);
        items.forEach(x => rows.push(globalRow(src.key, x)));
      });
      document.getElementById('tables-area').innerHTML = rows.length
        ? tableHtml(GLOBAL_HEADERS, rows, '无记录', GLOBAL_WIDTHS)
        : unavailable.length
          ? `<span class="muted">部分来源查询不可用（${esc(unavailable.join('、'))}），其余来源无匹配结果</span>`
          : `<span class="muted">无匹配「${esc(q)}」的结果</span>`;
      sectionBadgeText(`全局搜索「${q}」共 ${totalAll} 条${unavailable.length ? '（部分来源不可用：'+unavailable.join('、')+'）' : ''}${results.some(r=>r.available&&!r.complete) ? ' · 部分详情未完整同步' : ''}`);
      document.querySelector('.pager').style.display = 'none';
      document.getElementById('pageinfo').textContent = `全局搜索「${q}」共 ${totalAll} 条（${counts.join(' · ')}）`;
    }
    function runQuery() {
      return reload(true).catch(showQueryError);
    }
    let activeQueryRequest = 0;
    let displayedQueryScope = '';
    async function reload(reset=false) {
      const request = ++activeQueryRequest;
      if (reset) offset = 0;
      const button = document.getElementById('gms-query-btn');
      const area = document.getElementById('tables-area');
      const q = document.getElementById('q').value.trim();
      const scope = q ? JSON.stringify(['search', q]) : JSON.stringify([tab, tab === 'critical' ? 0 : offset,
        ...Array.from(document.querySelectorAll('.filters input,.filters select')).filter(el => el.getClientRects().length).map(el => [el.id, el.value])]);
      const sameScope = displayedQueryScope === scope;
      if (!sameScope) {
        area.innerHTML = '<span class="muted">查询中…</span>';
        sectionBadgeText('');
      }
      setActionButtonBusy(button, true, '查询中');
      area.setAttribute('aria-busy', 'true');
      try {
        await reloadData(reset);
        if (request === activeQueryRequest) displayedQueryScope = scope;
      } catch (err) {
        if (request === activeQueryRequest) {
          if (!sameScope || err.status === 401 || err.status === 403) {
            area.textContent = err.message || '查询失败';
            displayedQueryScope = '';
          }
          notifyUser('查询失败', err.message || '请重试', 'error');
        }
      } finally {
        if (request === activeQueryRequest) {
          setActionButtonBusy(button, false);
          area.setAttribute('aria-busy', 'false');
        }
      }
    }
    async function reloadData(reset=false) {
      if (reset) offset = 0;
      const q = document.getElementById('q').value.trim();
      if (q) { await searchAll(q); return; }
      const generation = ++queryGeneration;
      setGerritBusy(false);
      globalSearchActive = false;
      document.querySelector('.pager').style.display = '';
      const params = new URLSearchParams({limit, offset});
      if (q) params.set('q', q);
      const area = document.getElementById('tables-area');
      let sections = '';
      if (monitorTabs.includes(tab)) {
        const source = document.getElementById('source_key').value;
        const type = document.getElementById('type_filter').value.trim();
        const monitorVersion = document.getElementById('monitor_version').value;
        if (source && ['changes','artifacts'].includes(tab)) params.set('source_key', source);
        let endpoint = '/api/gms-update-center/changes';
        if (tab === 'artifacts') {
          endpoint = '/api/gms-update-center/artifacts';
          if (type) params.set(type.match(/^Android/i) ? 'android_version' : 'suite_type', type);
        } else if (tab === 'packages') {
          endpoint = '/api/gms-update-center/packages';
          params.delete('source_key');
          if (monitorVersion) params.set('android_version', monitorVersion);
        } else if (tab === 'mainline') {
          endpoint = '/api/gms-update-center/mainline';
          params.delete('source_key');
          if (type) params.set('year', type);
        } else if (tab === 'requirements') {
          endpoint = '/api/gms-update-center/requirements';
          params.delete('source_key');
          if (monitorVersion) params.set('android_version', monitorVersion);
          const kind = document.getElementById('requirement_kind').value;
          if (kind) params.set('change_kind', kind);
        } else if (type) {
          params.set('entity_type', type);
        }
        if (tab === 'changes' && document.getElementById('change_kind').value) params.set('change_type',document.getElementById('change_kind').value);
        const data = await fetchJson(endpoint + '?' + params.toString());
        if (generation !== queryGeneration || document.getElementById('q').value.trim()) return;
        if (!Array.isArray(data.data?.items)) throw new Error('invalid monitor response');
        total = Number(data.meta?.total ?? data.data.items.length);
        const meta = TABLE_META[tab];
        detailRecords.length=0;
        sections = tableHtml(meta.headers, meta.rows(data.data.items), '无记录', meta.widths);
      } else if (tab === 'patch') {
        const year = document.getElementById('cve_year').value.trim();
        const module = document.getElementById('cve_module').value.trim();
        if (year) params.set('release_year',year);
        if (module) params.set('mainline_module',module);
        const data = await fetchJson('/api/gms-update-center/security-patches?' + params.toString());
        if (generation !== queryGeneration || document.getElementById('q').value.trim()) return;
        total = data.total;
        detailRecords.length=0;
        sections = tableHtml(PATCH_HEADERS, patchRows(data.items), '无记录', PATCH_WIDTHS);
      } else if (tab === 'critical') {
        setGerritBusy(true);
        if (!area.querySelector('.critical-matrix')) area.innerHTML = '<span class="muted">加载公告记录…</span>';
        try { await loadCriticalIndex(generation); }
        catch (err) {
          if (generation === queryGeneration && tab === 'critical') {
            setGerritBusy(false);
            if (err.status === 401 || err.status === 403) {
              criticalItems = null;
              gerritByPatch = null;
              criticalGerritReady = false;
            }
            throw err;
          }
        }
        return;
      } else {
        const type = document.getElementById('issue_type').value;
        const section = document.getElementById('product_section').value;
        const exemption = document.getElementById('exemption_id').value.trim();
        if (type) params.set('issue_type', type);
        if (section) params.set('product_section', section);
        if (exemption) params.set('exemption_id', exemption);
        const data = await fetchJson('/api/gms-update-center/exemptions?' + params.toString());
        if (generation !== queryGeneration || document.getElementById('q').value.trim()) return;
        total = data.total;
        detailRecords.length=0;
        sections = tableHtml(EXEMPTION_HEADERS, exemptionRows(data.items), '无记录', EXEMPTION_WIDTHS);
      }
      area.innerHTML = sections;
      sectionBadgeText('');
      updatePager();

    }
    function showToast(message, type='info') {
      const el = document.getElementById('toast');
      if (!el) return;
      el.textContent = message;
      el.className = 'toast' + (type === 'error' ? ' error' : '');
      el.style.display = 'block';
      clearTimeout(showToast._timer);
      showToast._timer = setTimeout(() => { el.style.display = 'none'; }, 3600);
    }
    function notifyUser(title, message, level) {
      level = level || 'info';
      try {
        if (window.parent && window.parent !== window) {
          window.parent.postMessage({type:'gms-update-monitor-notification', title:title, message:message, level:level}, window.location.origin);
        }
      } catch (_) {}
      showToast(title + (message ? ': ' + message : ''), level);
    }
    function startSuiteDownload(url) {
      const safeUrl = safeExternalHref(url);
      if (!safeUrl) { showToast('下载地址不安全：仅允许 HTTPS 或本机来源', 'error'); return; }
      let frame = document.getElementById('suite-direct-download-frame');
      if (!frame) {
        frame = document.createElement('iframe');
        frame.id = 'suite-direct-download-frame';
        frame.name = 'suite-direct-download-frame';
        frame.style.display = 'none';
        document.body.appendChild(frame);
      }
      frame.src = safeUrl;
      showToast('已开始下载套件');
      notifyUser('GMS套件下载已开始', '浏览器已开始下载套件', 'info');
    }
    const SCAN_SOURCES = {
      requirements:['gms_requirements'], packages:['gms_downloads'],
      mainline:['mainline_preload'], exemptions:['mainline_preload'], patch:['mainline_preload'], artifacts:['cts_downloads','vts_downloads','gts_downloads','gms_downloads'],
      critical:['gms_bulletins'],
    };
    function showQueryError(err) {
      const area = document.getElementById('tables-area');
      if (!area.querySelector('table')) area.textContent = err.message || '查询失败';
      notifyUser('查询失败', err.message || '请重试', 'error');
    }
    function loadAll(reset=false) {
      return withActionButtonBusy(document.getElementById('gms-refresh-btn'), '刷新中', async () => {
        await Promise.all([loadSummary(), reload(reset)]);
      }).catch(showQueryError);
    }
    let syncInProgress = false;
    function setSyncBusy(busy) {
      syncInProgress = busy;
      document.querySelectorAll('header [data-click="startSync"]').forEach(b=>b.disabled=busy);
    }
    async function startSync(mode) {
      if (syncInProgress) return;
      const scanTab = tab;
      const endpoint = '/api/gms-update-center';
      const params = new URLSearchParams({mode});
      (SCAN_SOURCES[scanTab] || []).forEach(source=>params.append('source',source));
      const label = document.querySelector('[data-tab="'+scanTab+'"]').textContent;
      setSyncBusy(true);
      try {
        const response = await fetch(endpoint+'/scan?'+params.toString(),{method:'POST'});
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || '启动失败');
        await pollSync(endpoint,label);
      } catch (err) {
        notifyUser(label+'扫描失败',err.message,'error');
        setSyncBusy(false);
      }
    }
    async function pollSync(endpoint,label) {
      try {
        const data = await fetchJson(endpoint+'/scan/status');
        const status = data.data?.status || data.status;
        if (!status) throw new Error('扫描状态不可用');
        if (status.running) {
          document.getElementById('summary').textContent = label+'扫描中：'+(status.mode || '')+' · '+(status.sources||[]).filter(s=>['ready','unchanged','failed','partial','interrupted'].includes(s.state)).length+'/'+(status.source||[]).length;
          setTimeout(()=>pollSync(endpoint,label),3000);
          return;
        }
        if (status.state !== 'completed' || status.error || status.returncode !== 0) {
          const detail = String(status.stderr || '').split(/\r?\n/).map(v=>v.trim()).filter(Boolean).pop();
          notifyUser(label+'扫描失败',[status.error || '扫描未成功',detail].filter(Boolean).join('：'),'error');
        } else {
          notifyUser(label+'扫描完成',status.finished_at || '扫描已完成','success');
        }
        setSyncBusy(false);
        loadAll(true);
      } catch (err) {
        setSyncBusy(false);
        notifyUser(label+'扫描状态查询失败',err.message,'error');
      }
    }
    function updatePager() {
      document.getElementById('pageinfo').textContent=total ? (offset+1)+'-'+Math.min(offset+limit,total)+' / '+total : '0 / 0';
      document.querySelector('.pager [data-a0="-1"]').disabled=offset<=0;
      document.querySelector('.pager [data-a0="1"]').disabled=offset+limit>=total;
    }
    function page(delta) {
      if (globalSearchActive) return;
      const next = offset + delta * limit;
      if (next < 0 || next >= total) return;
      offset = next;
      if (tab === 'critical' && criticalItems) renderCritical();
      else reload(false).catch(showQueryError);
    }
    document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') runQuery(); });
    document.getElementById('type_filter').addEventListener('keydown', e => { if (e.key === 'Enter') runQuery(); });
    document.getElementById('tables-area').addEventListener('click', e => {
      const linkEl = e.target.closest('.suite-download-link');
      if (!linkEl) return;
      e.preventDefault();
      startSuiteDownload(linkEl.dataset.url || '');
    });
    document.getElementById('exemption_id').addEventListener('keydown', e => { if (e.key === 'Enter') reload(true).catch(showQueryError); });
    ['issue_type','product_section','source_key','monitor_version','requirement_kind','change_kind'].forEach(id => document.getElementById(id).addEventListener('change', () => reload(true).catch(showQueryError)));
    initCriticalFilters();
    document.getElementById('critical-legacy-toggle').addEventListener('click', () => {
      showLegacyGerritVersions = !showLegacyGerritVersions;
      renderCritical();
    });
    ['cve_year','cve_module'].forEach(id=>document.getElementById(id).addEventListener('keydown',e=>{ if(e.key==='Enter') runQuery(); }));
    document.getElementById('critical-detail-copy').addEventListener('click',async()=>{
      try { await navigator.clipboard.writeText(detailCopyText); showToast('已复制详情'); }
      catch (_) { showToast('复制失败，请手动选择正文复制','error'); }
    });
    document.getElementById('critical-detail-close').addEventListener('click', closeCriticalDetails);
    document.getElementById('critical-detail-done').addEventListener('click', closeCriticalDetails);
    document.querySelectorAll('.tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
    syncFilterVisibility();
    rememberTab();
    loadAll(true);
    fetchJson('/api/gms-update-center/scan/status').then(data=>{ if(data.data?.status?.running && !syncInProgress){setSyncBusy(true);pollSync('/api/gms-update-center','GMS 更新');} }).catch(()=>{});
