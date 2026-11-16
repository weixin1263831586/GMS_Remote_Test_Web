    const MAINLINE_TAB_STORAGE_KEY = 'gms_mainline_known_issues_tab';
    const mainlineTabs = ['exemptions', 'patch', 'critical'];
    let initialTab = new URLSearchParams(window.location.search).get('tab') || '';
    if (!mainlineTabs.includes(initialTab)) {
      try { initialTab = window.sessionStorage.getItem(MAINLINE_TAB_STORAGE_KEY) || ''; } catch (_) { initialTab = ''; }
    }
    let tab = mainlineTabs.includes(initialTab) ? initialTab : 'exemptions';
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
    // 全局搜索（跨三类）统一聚合列规范，与 GMS Update Monitor 相同。
    const GLOBAL_HEADERS = ['类别', '标识', '摘要', '发布', '操作'];
    const GLOBAL_WIDTHS = ['130px', '260px', '', '210px', '80px'];
    async function fetchJson(url) {
      const r = await fetch(url);
      const data = await r.json();
      if (!data.success) throw new Error(data.error || 'query failed');
      return data;
    }
    function sectionBadgeText(text) {
      document.getElementById('section-badge').textContent = text;
    }
    function tableHtml(headers, rows, emptyText, widths) {
      // 表头表格常驻滚动区外（不依赖 th sticky），表体独立滚动；共用 colgroup 对齐。
      const cols = (widths || []).map(w => `<col${w ? ` style="width:${esc(w)}"` : ''}>`).join('');
      const colgroup = cols ? `<colgroup>${cols}</colgroup>` : '';
      const head = `<table class="table-head-table">${colgroup}<thead><tr>${headers.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead></table>`;
      const bodyRows = rows.length ? rows.join('') : `<tr><td colspan="${headers.length}" class="muted">${esc(emptyText)}</td></tr>`;
      const body = `<div class="table-scroll"><table class="table-body-table">${colgroup}<tbody>${bodyRows}</tbody></table></div>`;
      return `<div class="table-split">${head}${body}</div>`;
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
    function criticalDescription(item) {
      const excerpt = item.detail_excerpt
        ? `<details><summary>详情正文摘录</summary><div class="wrap">${esc(item.detail_excerpt)}</div></details>` : '';
      const pending = ['not_scanned', 'unavailable', 'stale'].includes(item.detail_status)
        ? '<div class="muted">详情未完整同步，请更新资料后核实。</div>' : '';
      return `${esc(item.description)}${excerpt}${pending}`;
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
          <td>${criticalDescription(item)}</td>
          <td>${esc(item.release_month || item.bulletin_month || '')}</td>
          <td>${esc(item.deadline)}</td>
          <td>${exLinks}</td>
          <td><a href="${esc(item.bulletin_url)}" target="_blank">打开</a></td>
        </tr>`;
      });
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
    // Tab 切换：一次只显示一张表，?tab= 与 sessionStorage 记忆当前页签。
    function setTab(next) {
      tab = mainlineTabs.includes(next) ? next : 'exemptions';
      offset = 0;
      try { window.sessionStorage.setItem(MAINLINE_TAB_STORAGE_KEY, tab); } catch (_) {}
      const url = new URL(window.location.href);
      if (tab === 'exemptions') url.searchParams.delete('tab');
      else url.searchParams.set('tab', tab);
      window.history.replaceState({}, '', url.toString());
      document.querySelectorAll('.tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
      syncFilterVisibility();
      reload(true);
    }
    // 小节/豁免ID/类型过滤仅对豁免条目表有意义。
    function syncFilterVisibility() {
      const exemptionOnly = tab === 'exemptions';
      ['issue_type', 'product_section', 'exemption_id'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = exemptionOnly ? '' : 'none';
      });
      document.querySelector('.filters')?.classList.toggle('single-filter', !exemptionOnly);
    }
    // 全局搜索：关键字非空时并行查询三类内容聚合为统一表。
    const GLOBAL_SEARCH_SOURCES = [
      {key: 'exemptions', label: '豁免条目', endpoint: '/api/mainline-known-issues'},
      {key: 'patch', label: '安全补丁 (CVE)', endpoint: '/api/mainline-known-issues/security-patches'},
      {key: 'critical', label: 'Critical_Patch 补丁', endpoint: '/api/mainline-known-issues/critical-patches'},
    ];
    function globalRow(kind, x) {
      if (kind === 'exemptions') {
        return `<tr><td>豁免条目</td><td>${esc(x.issue_type)} · ${esc(x.product_section)} · <code>${esc(x.exemption_id)}</code></td><td class="wrap"><code>${esc(x.test_module)}</code> / <code>${esc(x.test_case)}</code> · ${esc(x.category)} · ${esc(x.android_versions)}</td><td>${releaseCell({...x, release_month: x.release_month || releaseMonthOf(x.source_url)})}</td><td><a href="${esc(x.source_url)}" target="_blank">打开</a></td></tr>`;
      }
      if (kind === 'patch') {
        return `<tr><td>安全补丁 (CVE)</td><td><code>${esc(x.cve_id)}</code></td><td class="wrap">${esc(x.mainline_module)} · ${esc(x.version_note)}</td><td>${releaseCell({...x, release_month: x.release_month || releaseMonthOf(x.source_url)})}</td><td><a href="${esc(x.source_url)}" target="_blank">打开</a></td></tr>`;
      }
      return `<tr><td>Critical_Patch</td><td><code>${esc(x.patch_id)}</code> · ${esc(x.patch_kind)} · ${esc(x.android_versions)}</td><td class="wrap">${criticalDescription(x)}${x.deadline ? '（期限 ' + esc(x.deadline) + '）' : ''}</td><td>${esc(x.release_month || x.bulletin_month || '')}</td><td><a href="${esc(x.bulletin_url)}" target="_blank">打开</a></td></tr>`;
    }
    async function searchAll(q) {
      const section = document.getElementById('product_section').value;
      const results = await Promise.all(GLOBAL_SEARCH_SOURCES.map(async src => {
        const params = new URLSearchParams({q, limit, offset: 0});
        if (src.key === 'exemptions' && section) params.set('product_section', section);
        try {
          const data = await fetchJson(src.endpoint + '?' + params.toString());
          return {src, items: data.items || [], total: data.total || 0, available: data.available !== false,
                  complete: data.coverage_complete !== false};
        } catch (_) {
          return {src, items: [], total: 0, available: false, complete: false};
        }
      }));
      const rows = [];
      const counts = [];
      let totalAll = 0;
      results.forEach(({src, items, total: srcTotal, available, complete}) => {
        counts.push(`${src.label} ${available ? srcTotal : '查询不可用'}${available && !complete ? '（详情未完整同步）' : ''}`);
        totalAll += srcTotal;
        if (!available || !items.length) return;
        rows.push(`<tr class="group-row"><td colspan="5">${esc(src.label)} · 命中 ${srcTotal} 条${items.length < srcTotal ? `（显示前 ${items.length} 条）` : ''}</td></tr>`);
        items.forEach(x => rows.push(globalRow(src.key, x)));
      });
      document.getElementById('tables-area').innerHTML = rows.length
        ? tableHtml(GLOBAL_HEADERS, rows, '无记录', GLOBAL_WIDTHS)
        : `<span class="muted">无匹配「${esc(q)}」的结果</span>`;
      sectionBadgeText(`全局搜索「${q}」共 ${totalAll} 条`);
      document.querySelector('.pager').style.display = 'none';
      document.getElementById('pageinfo').textContent = `全局搜索「${q}」共 ${totalAll} 条（${counts.join(' · ')}）`;
    }
    function runQuery() {
      const q = document.getElementById('q').value.trim();
      if (q) {
        searchAll(q).catch(err => {
          document.getElementById('tables-area').innerHTML = `<span class="muted">${esc(err.message)}</span>`;
        });
        return;
      }
      reload(true);
    }
    async function reload(reset=false) {
      if (reset) offset = 0;
      const q = document.getElementById('q').value.trim();
      if (q) { await searchAll(q); return; }
      document.querySelector('.pager').style.display = '';
      const params = new URLSearchParams({limit, offset});
      if (q) params.set('q', q);
      const area = document.getElementById('tables-area');
      let sections = '';
      let badge = '';
      if (tab === 'patch') {
        const data = await fetchJson('/api/mainline-known-issues/security-patches?' + params.toString());
        total = data.total;
        sections = tableHtml(PATCH_HEADERS, patchRows(data.items), '无记录', PATCH_WIDTHS);
        badge = `安全补丁 (CVE) · 共 ${total} 条`;
      } else if (tab === 'critical') {
        const data = await fetchJson('/api/mainline-known-issues/critical-patches?' + params.toString());
        total = data.total;
        sections = tableHtml(CRITICAL_HEADERS, criticalRows(data.items), '无记录', CRITICAL_WIDTHS);
        badge = data.available ? `Critical_Patch · 共 ${total} 条${data.coverage_complete === false ? ' · 详情未完整同步' : ''}` : 'Critical_Patch · 数据源未扫描（GMS Update Monitor 中执行 Critical_Patch 扫描后可用）';
      } else {
        const type = document.getElementById('issue_type').value;
        if (type) params.set('issue_type', type);
        const data = await fetchJson('/api/mainline-known-issues?' + params.toString());
        total = data.total;
        sections = tableHtml(EXEMPTION_HEADERS, exemptionRows(data.items), '无记录', EXEMPTION_WIDTHS);
        badge = `${type ? `豁免条目（${esc(type)}）` : '豁免条目'} · 共 ${total} 条`;
      }
      area.innerHTML = sections;
      sectionBadgeText(badge);
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
    document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') runQuery(); });
    document.getElementById('exemption_id').addEventListener('keydown', e => { if (e.key === 'Enter') reload(true); });
    document.getElementById('issue_type').addEventListener('change', () => reload(true));
    document.querySelectorAll('.tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
    syncFilterVisibility();
    try { window.sessionStorage.setItem(MAINLINE_TAB_STORAGE_KEY, tab); } catch (_) {}
    loadSummary().then(() => reload()).catch(err => {
      document.getElementById('summary').textContent = err.message;
    });
