    let offset = 0;
    const limit = 100;
    let total = 0;
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const releaseMonthOf = url => {
      const m = String(url || '').match(/release-notes\/\d{4}\/([a-z-]+?)(?:\/|$)/);
      return m ? m[1].charAt(0).toUpperCase() + m[1].slice(1) : '';
    };
    const releaseCell = item => `${esc(item.release_year)} ${item.release_month ? '<span class="muted">' + esc(item.release_month) + '</span> ' : ''}<span class="muted">${esc(item.release_label)}</span>`;
    // table-layout:fixed 下未指定宽度的列独占剩余空间（放长文本列）。
    const EXEMPTION_HEADERS = ['类型', '文档小节', 'Android版本', '类别', '发布版本', '豁免ID', '测试模块', '测试用例', '来源'];
    const EXEMPTION_WIDTHS = ['92px', '114px', '124px', '144px', '224px', '128px', '16%', '', '80px'];
    const PATCH_HEADERS = ['CVE 编号', 'Mainline 模块', '版本说明', '发布版本', '来源'];
    const PATCH_WIDTHS = ['164px', '154px', '', '224px', '80px'];
    const CRITICAL_HEADERS = ['年份', '补丁 ID', '类型', 'Android 版本', '说明', '公告月份', '完成期限', '关联豁免', '来源'];
    const CRITICAL_WIDTHS = ['80px', '118px', '92px', '232px', '', '108px', '156px', '190px', '80px'];
    async function fetchJson(url) {
      const r = await fetch(url);
      const data = await r.json();
      if (!data.success) throw new Error(data.error || 'query failed');
      return data;
    }
    function tableHtml(headers, rows, emptyText, widths) {
      const cols = (widths || []).map(w => `<col${w ? ` style="width:${esc(w)}"` : ''}>`).join('');
      const colgroup = cols ? `<colgroup>${cols}</colgroup>` : '';
      const head = '<thead><tr>' + headers.map(h => `<th>${esc(h)}</th>`).join('') + '</tr></thead>';
      const body = rows.length
        ? `<tbody>${rows.join('')}</tbody>`
        : `<tbody><tr><td colspan="${headers.length}" class="muted">${esc(emptyText)}</td></tr></tbody>`;
      return `<table>${colgroup}${head}${body}</table>`;
    }
    function exemptionRows(items) {
      return items.map(item => `
        <tr>
          <td>${esc(item.issue_type)}</td>
          <td>${esc(item.product_section)}</td>
          <td>${esc(item.android_versions)}</td>
          <td>${esc(item.category)}</td>
          <td>${releaseCell({...item, release_month: item.release_month || releaseMonthOf(item.source_url)})}</td>
          <td><code>${esc(item.exemption_id)}</code></td>
          <td><code>${esc(item.test_module)}</code></td>
          <td><code>${esc(item.test_case)}</code></td>
          <td><a href="${esc(item.source_url)}" target="_blank">打开</a></td>
        </tr>`);
    }
    function patchRows(items) {
      return items.map(item => `
        <tr>
          <td><code>${esc(item.cve_id)}</code></td>
          <td>${esc(item.mainline_module)}</td>
          <td>${esc(item.version_note)}</td>
          <td>${releaseCell({...item, release_month: item.release_month || releaseMonthOf(item.source_url)})}</td>
          <td><a href="${esc(item.source_url)}" target="_blank">打开</a></td>
        </tr>`);
    }
    function criticalRows(items) {
      return items.map(item => {
        const exLinks = (item.exemptions || []).map(e =>
          `<code>${esc(e.issue_type)}</code> ${esc(e.test_module)}`).join('；') || '<span class="muted">—</span>';
        return `
        <tr>
          <td>${esc(item.year)}</td>
          <td><code>${esc(item.patch_id)}</code></td>
          <td>${esc(item.patch_kind)}</td>
          <td>${esc(item.android_versions)}</td>
          <td>${esc(item.description)}</td>
          <td>${esc(item.release_month || item.bulletin_month || '')}</td>
          <td>${esc(item.deadline)}</td>
          <td>${exLinks}</td>
          <td><a href="${esc(item.bulletin_url)}" target="_blank">打开</a></td>
        </tr>`;
      });
    }
    function sectionHtml(title, countText, tableMarkup) {
      return `<div class="result-section"><div class="section-title">${esc(title)} <span class="muted">${esc(countText)}</span></div>${tableMarkup}</div>`;
    }
    async function loadSummary() {
      const data = await fetchJson('/api/mainline-known-issues/summary');
      const parts = [`总数 ${data.total}`];
      for (const row of data.by_type) parts.push(`${row.issue_type}/${row.product_section}: ${row.count}`);
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
      document.getElementById('summary').textContent = parts.join(' | ');
    }
    async function reload(reset=false) {
      if (reset) offset = 0;
      const type = document.getElementById('issue_type').value;
      const params = new URLSearchParams({limit, offset});
      const q = document.getElementById('q').value.trim();
      if (q) params.set('q', q);
      const area = document.getElementById('tables-area');
      let sections = '';
      if (type === 'patch') {
        const data = await fetchJson('/api/mainline-known-issues/security-patches?' + params.toString());
        total = data.total;
        sections = sectionHtml('安全补丁 (CVE)', `共 ${data.total} 条`,
          tableHtml(PATCH_HEADERS, patchRows(data.items), '无记录', PATCH_WIDTHS));
      } else if (type === 'critical') {
        const data = await fetchJson('/api/mainline-known-issues/critical-patches?' + params.toString());
        total = data.total;
        sections = sectionHtml('Critical_Patch 补丁', data.available ? `共 ${data.total} 条` : '数据源未扫描（GMS Update Monitor 中执行 Critical_Patch 扫描后可用）',
          tableHtml(CRITICAL_HEADERS, criticalRows(data.items), '无记录', CRITICAL_WIDTHS));
      } else if (type) {
        params.set('issue_type', type);
        const data = await fetchJson('/api/mainline-known-issues?' + params.toString());
        total = data.total;
        sections = sectionHtml(`豁免条目（${esc(type)}）`, `共 ${data.total} 条`,
          tableHtml(EXEMPTION_HEADERS, exemptionRows(data.items), '无记录', EXEMPTION_WIDTHS));
      } else {
        // 全部类型：豁免 / 安全补丁 / Critical_Patch 三表并行查询，分区展示。
        const [ex, pa, cr] = await Promise.all([
          fetchJson('/api/mainline-known-issues?' + params.toString()),
          fetchJson('/api/mainline-known-issues/security-patches?' + params.toString()),
          fetchJson('/api/mainline-known-issues/critical-patches?' + params.toString()),
        ]);
        total = ex.total + pa.total + cr.total;
        sections = sectionHtml('豁免条目', `共 ${ex.total} 条`,
          tableHtml(EXEMPTION_HEADERS, exemptionRows(ex.items), '无记录', EXEMPTION_WIDTHS))
          + sectionHtml('安全补丁 (CVE)', `共 ${pa.total} 条`,
          tableHtml(PATCH_HEADERS, patchRows(pa.items), '无记录', PATCH_WIDTHS))
          + sectionHtml('Critical_Patch 补丁', cr.available ? `共 ${cr.total} 条` : '数据源未扫描',
          tableHtml(CRITICAL_HEADERS, criticalRows(cr.items), '无记录', CRITICAL_WIDTHS));
      }
      area.innerHTML = sections;
      document.getElementById('pageinfo').textContent = total > 0
        ? `${offset + 1}-${Math.min(offset + limit, total)} / ${total}`
        : `0 / 0`;
    }
    function page(delta) {
      const next = offset + delta * limit;
      if (next < 0 || next >= total) return;
      offset = next;
      reload(false);
    }
    document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') reload(true); });
    document.getElementById('exemption_id').addEventListener('keydown', e => { if (e.key === 'Enter') reload(true); });
    document.getElementById('issue_type').addEventListener('change', () => reload(true));
    loadSummary().then(() => reload()).catch(err => {
      document.getElementById('summary').textContent = err.message;
    });
