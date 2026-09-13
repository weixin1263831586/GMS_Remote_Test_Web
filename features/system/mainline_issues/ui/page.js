    let offset = 0;
    const limit = 100;
    let total = 0;
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    async function loadSummary() {
      const r = await fetch('/api/mainline-known-issues/summary');
      const data = await r.json();
      if (!data.success) throw new Error(data.error || 'summary failed');
      const parts = [`总数 ${data.total}`];
      for (const row of data.by_type) parts.push(`${row.issue_type}/${row.product_section}: ${row.count}`);
      if (data.year_range) {
        const yr = data.year_range;
        parts.push(`数据覆盖: ${yr.min_year === yr.max_year ? yr.min_year : yr.min_year + '-' + yr.max_year}`);
      }
      if (data.last_sync) {
        const ls = data.last_sync;
        const syncTime = ls.finished_at ? new Date(ls.finished_at).toLocaleString('zh-CN') : '未知';
        parts.push(`最近同步: ${syncTime}, ${ls.mode === 'full' ? '全量' : '增量'}${ls.pages_scanned ? ', ' + ls.pages_scanned + '页' : ''}, ${ls.issues_found || 0}条`);
      }
      document.getElementById('summary').textContent = parts.join(' | ');
    }
    async function reload(reset=false) {
      if (reset) offset = 0;
      const params = new URLSearchParams({limit, offset});
      for (const id of ['q','issue_type','product_section','exemption_id']) {
        const value = document.getElementById(id).value.trim();
        if (value) params.set(id, value);
      }
      const r = await fetch('/api/mainline-known-issues?' + params.toString());
      const data = await r.json();
      if (!data.success) throw new Error(data.error || 'query failed');
      total = data.total;
      document.getElementById('rows').innerHTML = data.items.map(item => `
        <tr>
          <td>${esc(item.issue_type)}</td>
          <td>${esc(item.product_section)}</td>
          <td>${esc(item.android_versions)}</td>
          <td>${esc(item.category)}</td>
          <td>${esc(item.release_year)} ${item.release_month ? '<span class="muted">' + esc(item.release_month.charAt(0).toUpperCase() + item.release_month.slice(1)) + '</span> ' : ''}<span class="muted">${esc(item.release_label)}</span></td>
          <td><code>${esc(item.exemption_id)}</code></td>
          <td><code>${esc(item.test_module)}</code></td>
          <td><code>${esc(item.test_case)}</code></td>
          <td><a href="${esc(item.source_url)}" target="_blank">打开</a></td>
        </tr>`).join('') || '<tr><td colspan="9" class="muted">无记录</td></tr>';
      document.getElementById('pageinfo').textContent = `${offset + 1}-${Math.min(offset + limit, total)} / ${total}`;
    }
    function page(delta) {
      const next = offset + delta * limit;
      if (next < 0 || next >= total) return;
      offset = next;
      reload(false);
    }
    document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') reload(true); });
    document.getElementById('exemption_id').addEventListener('keydown', e => { if (e.key === 'Enter') reload(true); });
    loadSummary().then(() => reload()).catch(err => {
      document.getElementById('summary').textContent = err.message;
    });
  