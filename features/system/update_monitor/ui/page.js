    const UPDATE_MONITOR_TAB_STORAGE_KEY = 'gms_update_monitor_tab';
    const updateMonitorTabs = ['changes', 'artifacts', 'packages', 'mainline', 'requirements'];
    let initialTab = new URLSearchParams(window.location.search).get('tab') || '';
    if (!updateMonitorTabs.includes(initialTab)) {
      try { initialTab = window.sessionStorage.getItem(UPDATE_MONITOR_TAB_STORAGE_KEY) || ''; } catch (_) { initialTab = ''; }
    }
    let tab = updateMonitorTabs.includes(initialTab) ? initialTab : 'changes';
    let offset = 0;
    const limit = 100;
    let total = 0;
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const link = (url, text='下载') => url ? `<a href="${esc(url)}" target="_blank">${esc(text)}</a>` : '';
    const suiteDownloadLink = url => url ? `<a href="#" class="suite-download-link" data-url="${esc(url)}">下载</a>` : '';
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
    function setTab(next) {
      tab = updateMonitorTabs.includes(next) ? next : 'changes'; offset = 0;
      try { window.sessionStorage.setItem(UPDATE_MONITOR_TAB_STORAGE_KEY, tab); } catch (_) {}
      const url = new URL(window.location.href);
      if (tab === 'changes') url.searchParams.delete('tab');
      else url.searchParams.set('tab', tab);
      window.history.replaceState({}, '', url.toString());
      document.querySelectorAll('.tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
      reload(true);
    }
    async function loadSummary() {
      const r = await fetch('/api/gms-update-monitor/summary');
      const data = await r.json();
      if (!data.success) throw new Error(data.error || 'summary failed');
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
      document.getElementById('summary').textContent = parts.join(' | ') || '暂无扫描数据';
    }
    function startSuiteDownload(url) {
      if (!url) return;
      let frame = document.getElementById('suite-direct-download-frame');
      if (!frame) {
        frame = document.createElement('iframe');
        frame.id = 'suite-direct-download-frame';
        frame.name = 'suite-direct-download-frame';
        frame.style.display = 'none';
        document.body.appendChild(frame);
      }
      frame.src = url;
      showToast('已开始下载套件');
      notifyUser('GMS套件下载已开始', '浏览器已开始下载套件', 'info');
    }
    async function startSync(mode) {
      const buttons = document.querySelectorAll('header button');
      buttons.forEach(b => b.disabled = true);
      try {
        const r = await fetch('/api/gms-update-monitor/sync?mode=' + encodeURIComponent(mode), {method:'POST'});
        const data = await r.json();
        if (!r.ok || !data.success) throw new Error(data.error || '启动失败');
        pollSync();
      } catch (e) {
        notifyUser('GMS更新扫描启动失败', e.message, 'error');
        buttons.forEach(b => b.disabled = false);
      }
    }
    async function pollSync() {
      const r = await fetch('/api/gms-update-monitor/sync/status');
      const data = await r.json();
      const status = data.data.status;
      if (status.running) {
        document.getElementById('summary').textContent = `扫描中: ${status.mode}`;
        setTimeout(pollSync, 3000);
        return;
      }
      document.querySelectorAll('header button').forEach(b => b.disabled = false);
      if (status.error) {
        let message = status.error;
        const stderrLines = String(status.stderr || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);
        const stderrDetail = (stderrLines[stderrLines.length - 1] || '').replace(/^error:\s*/i, '');
        if (stderrDetail && !message.includes(stderrDetail)) message += ': ' + stderrDetail;
        notifyUser('GMS更新扫描失败', message, 'error');
      } else {
        notifyUser('GMS更新扫描完成', status.finished_at ? ('完成时间: ' + status.finished_at) : '扫描已完成', 'success');
      }
      loadAll();
    }
    function paramsBase() {
      const params = new URLSearchParams({limit, offset});
      const q = document.getElementById('q').value.trim();
      const source = document.getElementById('source_key').value;
      const type = document.getElementById('type_filter').value.trim();
      if (q) params.set('q', q);
      if (source) params.set('source_key', source);
      return {params, type};
    }
    async function reload(reset=false) {
      if (reset) offset = 0;
      const {params, type} = paramsBase();
      let endpoint = '/api/gms-update-monitor/changes';
      if (tab === 'artifacts') {
        endpoint = '/api/gms-update-monitor/artifacts';
        if (type) params.set(type.match(/^Android/i) ? 'android_version' : 'suite_type', type);
      } else if (tab === 'packages') {
        endpoint = '/api/gms-update-monitor/packages';
        params.delete('source_key');
        if (type) params.set('android_version', type);
      } else if (tab === 'mainline') {
        endpoint = '/api/gms-update-monitor/mainline';
        params.delete('source_key');
        if (type) params.set('year', type);
      } else if (tab === 'requirements') {
        endpoint = '/api/gms-update-monitor/requirements/version-tags';
        params.delete('source_key');
        if (type && /^Android\s+1[5-7]$/i.test(type)) params.set('android_version', type);
        else if (type && /^(added|changed|specific)$/i.test(type)) params.set('change_kind', type);
      } else if (type) {
        params.set('entity_type', type);
      }
      const r = await fetch(endpoint + '?' + params.toString());
      const data = await r.json();
      if (!data.success) throw new Error(data.error || 'query failed');
      total = data.meta.total;
      renderRows(data.data.items);
      document.getElementById('pageinfo').textContent = `${total ? offset + 1 : 0}-${Math.min(offset + limit, total)} / ${total}`;
    }
    function renderRows(items) {
      if (tab === 'changes') {
        document.getElementById('thead').innerHTML = '<tr><th style="width:95px">来源</th><th style="width:110px">对象</th><th style="width:70px">类型</th><th>内容</th><th style="width:145px">时间</th></tr>';
        document.getElementById('rows').innerHTML = items.map(x => `<tr><td>${esc(x.source_key)}</td><td>${esc(x.entity_type)}</td><td>${esc(x.change_type)}</td><td class="wrap">${esc(JSON.stringify(x.after || x.before || {})).slice(0, 700)}</td><td>${esc(x.detected_at)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">无记录</td></tr>';
      } else if (tab === 'artifacts') {
        document.getElementById('thead').innerHTML = '<tr><th style="width:80px">套件</th><th style="width:250px">Android</th><th style="width:70px">API level</th><th style="width:26%">套件版本</th><th>套件文件名</th><th style="width:60px">架构</th><th style="width:70px">页面</th><th style="width:70px">下载</th></tr>';
        document.getElementById('rows').innerHTML = items.map(x => {
          const title = x.release_name || x.file_name || '';
          const androidText = x.suite_type === 'GTS' && x.target_platform ? x.target_platform : x.android_version;
          return `<tr><td>${esc(x.suite_type)}</td><td class="wrap">${esc(androidText)}</td><td>${esc(x.api_level)}</td><td class="wrap">${esc(title)}</td><td class="wrap">${esc(x.file_name)}</td><td>${esc(x.arch)}</td><td>${link(x.section_url, '页面')}</td><td>${suiteDownloadLink(x.download_url)}</td></tr>`;
        }).join('') || '<tr><td colspan="8" class="muted">无记录</td></tr>';
      } else if (tab === 'packages') {
        document.getElementById('thead').innerHTML = '<tr><th style="width:200px">章节</th><th style="width:150px">Android</th><th style="width:30%">文件</th><th style="width:150px">生效日期</th><th>Gerrit Tag</th><th style="width:56px">链接</th></tr>';
        document.getElementById('rows').innerHTML = items.map(x => `<tr><td>${esc(x.section)}</td><td>${esc(x.android_version)}</td><td class="wrap">${esc(x.file_name || x.description)}</td><td>${esc(x.required_from)}</td><td>${esc(x.partner_gerrit_tag)}</td><td>${link(x.download_url)}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">无记录</td></tr>';
      } else if (tab === 'mainline') {
        document.getElementById('thead').innerHTML = '<tr><th style="width:80px">年份</th><th style="width:90px">月份</th><th style="width:32%">PRELOAD版本</th><th style="width:140px">Partner Zip</th><th style="width:90px">CI构建</th><th style="width:70px">Notes</th></tr>';
        document.getElementById('rows').innerHTML = items.map(x => {
          const zipText = x.partner_zip_build_id || x.partner_zip_label || '';
          return `<tr><td>${esc(x.year)}</td><td>${esc(x.month_label || x.month)}</td><td class="wrap">${link(x.notes_url, x.preload_version)}</td><td>${esc(zipText)}</td><td>${link(x.ci_build_url, zipText ? '构建' : '')}</td><td>${link(x.notes_url, '查看')}</td></tr>`;
        }).join('') || '<tr><td colspan="6" class="muted">无记录</td></tr>';
      } else {
        document.getElementById('thead').innerHTML = '<tr><th style="width:90px">Android</th><th style="width:70px">类型</th><th style="width:28%">章节</th><th style="width:150px">Requirement ID</th><th>要求摘要</th></tr>';
        document.getElementById('rows').innerHTML = items.map(x => `<tr><td>${esc(x.android_version)}</td><td>${esc(x.change_kind)}</td><td class="wrap">${esc(x.section_title)}</td><td class="wrap">${esc(x.requirement_ids)}</td><td class="wrap">${esc(x.text_excerpt).slice(0, 700)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">无记录</td></tr>';
      }
    }
    function page(delta) {
      const next = offset + delta * limit;
      if (next < 0 || next >= total) return;
      offset = next; reload(false);
    }
    function loadAll() { loadSummary().catch(e => document.getElementById('summary').textContent = e.message); reload(false).catch(e => document.getElementById('rows').innerHTML = `<tr><td class="muted">${esc(e.message)}</td></tr>`); }
    document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') reload(true); });
    document.getElementById('type_filter').addEventListener('keydown', e => { if (e.key === 'Enter') reload(true); });
    document.getElementById('rows').addEventListener('click', e => {
      const linkEl = e.target.closest('.suite-download-link');
      if (!linkEl) return;
      e.preventDefault();
      startSuiteDownload(linkEl.dataset.url || '');
    });
    document.querySelectorAll('.tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
    try { window.sessionStorage.setItem(UPDATE_MONITOR_TAB_STORAGE_KEY, tab); } catch (_) {}
    loadAll();
