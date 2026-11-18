    const UPDATE_MONITOR_TAB_STORAGE_KEY = 'gms_update_monitor_tab';
    const updateMonitorTabs = ['changes', 'artifacts', 'packages', 'mainline', 'requirements'];
    const BULLETIN_MONTH_NUM = {January:'1',February:'2',March:'3',April:'4',May:'5',June:'6',July:'7',August:'8',September:'9',October:'10',November:'11',December:'12'};
    let initialTab = new URLSearchParams(window.location.search).get('tab') || '';
    if (!updateMonitorTabs.includes(initialTab)) {
      try { initialTab = window.sessionStorage.getItem(UPDATE_MONITOR_TAB_STORAGE_KEY) || ''; } catch (_) { initialTab = ''; }
    }
    let tab = updateMonitorTabs.includes(initialTab) ? initialTab : 'changes';
    let globalSearchActive = false;
    let queryGeneration = 0;
    let offset = 0;
    const limit = 100;
    let total = 0;
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
    const link = (url, text='下载') => {
      const href = safeExternalHref(url);
      return href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : '';
    };
    const suiteDownloadLink = url => {
      const href = safeExternalHref(url);
      return href ? `<a href="#" class="suite-download-link" data-url="${esc(href)}">下载</a>` : '';
    };
    // 统一表格模板：表头表格常驻滚动区外，表体在 .table-scroll 内滚动，
    // 两表共用同一 colgroup 保证列对齐。
    function tableHtml(headers, rows, emptyText, widths) {
      const cols = (widths || []).map(w => `<col${w ? ` style="width:${esc(w)}"` : ''}>`).join('');
      const colgroup = cols ? `<colgroup>${cols}</colgroup>` : '';
      const head = `<table class="table-head-table">${colgroup}<thead><tr>${headers.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead></table>`;
      const bodyRows = rows.length ? rows.join('') : `<tr><td colspan="${headers.length}" class="muted">${esc(emptyText)}</td></tr>`;
      const body = `<div class="table-scroll"><table class="table-body-table">${colgroup}<tbody>${bodyRows}</tbody></table></div>`;
      return `<div class="table-split">${head}${body}</div>`;
    }
    // 各 Tab 的表头/列宽/行渲染（未指定宽度的列独占剩余空间，放长文本列）。
    const TABLE_META = {
      changes: {
        label: '变更',
        headers: ['来源', '对象', '类型', '内容', '时间'],
        widths: ['95px', '110px', '70px', '', '145px'],
        rows: items => items.map(x => `<tr><td>${esc(x.source_key)}</td><td>${esc(x.entity_type)}</td><td>${esc(x.change_type)}</td><td class="wrap">${esc(JSON.stringify(x.after || x.before || {})).slice(0, 700)}</td><td>${esc(x.detected_at)}</td></tr>`),
      },
      artifacts: {
        label: '测试套件更新',
        headers: ['套件', 'Android', 'API level', '套件版本', '套件文件名', '架构', '页面', '下载'],
        widths: ['80px', '250px', '70px', '26%', '', '60px', '70px', '70px'],
        rows: items => items.map(x => {
          const title = x.release_name || x.file_name || '';
          const androidText = x.suite_type === 'GTS' && x.target_platform ? x.target_platform : x.android_version;
          return `<tr><td>${esc(x.suite_type)}</td><td class="wrap">${esc(androidText)}</td><td>${esc(x.api_level)}</td><td class="wrap">${esc(title)}</td><td class="wrap">${esc(x.file_name)}</td><td>${esc(x.arch)}</td><td>${link(x.section_url, '页面')}</td><td>${suiteDownloadLink(x.download_url)}</td></tr>`;
        }),
      },
      packages: {
        label: 'GMS包更新',
        headers: ['章节', 'Android', '文件', '生效日期', 'Gerrit Tag', '链接'],
        widths: ['200px', '150px', '30%', '150px', '', '56px'],
        rows: items => items.map(x => `<tr><td>${esc(x.section)}</td><td>${esc(x.android_version)}</td><td class="wrap">${esc(x.file_name || x.description)}</td><td>${esc(x.required_from)}</td><td>${esc(x.partner_gerrit_tag)}</td><td>${link(x.download_url)}</td></tr>`),
      },
      mainline: {
        label: 'Mainline包更新',
        headers: ['年份', '月份', 'PRELOAD版本', 'Partner Zip', 'CI构建', 'Notes'],
        widths: ['80px', '90px', '32%', '140px', '90px', '70px'],
        rows: items => items.map(x => {
          const zipText = x.partner_zip_build_id || x.partner_zip_label || '';
          return `<tr><td>${esc(x.year)}</td><td>${esc(x.month_label || x.month)}</td><td class="wrap">${link(x.notes_url, x.preload_version)}</td><td>${esc(zipText)}</td><td>${link(x.ci_build_url, zipText ? '构建' : '')}</td><td>${link(x.notes_url, '查看')}</td></tr>`;
        }),
      },
      requirements: {
        label: 'GMS要求',
        headers: ['Android', '类型', '章节', 'Requirement ID', '要求摘要'],
        widths: ['90px', '70px', '28%', '150px', ''],
        rows: items => items.map(x => `<tr><td>${esc(x.android_version)}</td><td>${esc(x.change_kind)}</td><td class="wrap">${esc(x.section_title)}</td><td class="wrap">${esc(x.requirement_ids)}</td><td class="wrap">${esc(x.text_excerpt).slice(0, 700)}</td></tr>`),
      },
    };
    // 全局搜索聚合表（分组行）：与 mainline-known-issues 同一列规范。
    const GLOBAL_HEADERS = ['类别', '标识', '摘要', '时间/日期', '操作'];
    const GLOBAL_WIDTHS = ['110px', '200px', '', '150px', '100px'];
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
      globalSearchActive = false; // 切 Tab 即退出全局搜索
      try { window.sessionStorage.setItem(UPDATE_MONITOR_TAB_STORAGE_KEY, tab); } catch (_) {}
      const url = new URL(window.location.href);
      if (tab === 'changes') url.searchParams.delete('tab');
      else url.searchParams.set('tab', tab);
      window.history.replaceState({}, '', url.toString());
      document.querySelectorAll('.tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
      reload(true);
    }
    // 全局搜索：一次查询并行命中全部 5 类内容，后端端点均支持 q 参数。
    const GLOBAL_SEARCH_SOURCES = [
      {key: 'changes', label: '变更', endpoint: '/api/gms-update-monitor/changes'},
      {key: 'artifacts', label: '测试套件更新', endpoint: '/api/gms-update-monitor/artifacts'},
      {key: 'packages', label: 'GMS包更新', endpoint: '/api/gms-update-monitor/packages'},
      {key: 'mainline', label: 'Mainline包更新', endpoint: '/api/gms-update-monitor/mainline'},
      {key: 'requirements', label: 'GMS要求', endpoint: '/api/gms-update-monitor/requirements/version-tags'}
    ];
    function globalRow(kind, x) {
      if (kind === 'changes') {
        return `<tr><td>变更</td><td>${esc(x.source_key)} · ${esc(x.entity_type)}</td><td class="wrap">[${esc(x.change_type)}] ${esc(JSON.stringify(x.after || x.before || {})).slice(0, 400)}</td><td>${esc(x.detected_at)}</td><td></td></tr>`;
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
    async function searchAll() {
      const generation = ++queryGeneration;
      const q = document.getElementById('q').value.trim();
      if (!q) { globalSearchActive = false; reload(true); return; }
      const params = new URLSearchParams({q, limit: 100, offset: 0});
      const results = await Promise.all(GLOBAL_SEARCH_SOURCES.map(async src => {
        try {
          const r = await fetch(src.endpoint + '?' + params.toString());
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const data = await r.json();
          if (!data.success || !Array.isArray(data.data?.items)) throw new Error('invalid search response');
          return {src, items: data.data.items, total: Number(data.meta?.total ?? data.data.items.length), available: true};
        } catch (_) {
          return {src, items: [], total: 0, available: false};
        }
      }));
      if (generation !== queryGeneration || document.getElementById('q').value.trim() !== q) return;
      globalSearchActive = true;
      const rows = [];
      const counts = [];
      let totalAll = 0;
      const unavailable = [];
      results.forEach(({src, items, total, available}) => {
        if (!available) {
          unavailable.push(src.label);
          counts.push(src.label + ' 查询不可用');
          return;
        }
        counts.push(`${src.label} ${total}`);
        totalAll += total;
        if (!items.length) return;
        rows.push(`<tr class="group-row"><td colspan="5">${esc(src.label)} · 命中 ${total} 条${items.length < total ? `（显示前 ${items.length} 条）` : ''}</td></tr>`);
        items.forEach(x => rows.push(globalRow(src.key, x)));
      });
      document.getElementById('tables-area').innerHTML = rows.length
        ? tableHtml(GLOBAL_HEADERS, rows, '无记录', GLOBAL_WIDTHS)
        : unavailable.length
          ? `<span class="muted">部分来源查询不可用（${esc(unavailable.join('、'))}），其余来源无匹配结果</span>`
          : `<span class="muted">无匹配「${esc(q)}」的结果</span>`;
      sectionBadgeText(`全局搜索「${q}」共 ${totalAll} 条${unavailable.length ? '（部分来源不可用）' : ''}`);
      document.getElementById('pageinfo').textContent = `全局搜索「${q}」共 ${totalAll} 条（${counts.join(' · ')}）`;
      document.querySelector('.pager').style.display = 'none';
    }
    function runQuery() {
      const q = document.getElementById('q').value.trim();
      if (q) { searchAll().catch(e => showToast('全局搜索失败: ' + e.message, 'error')); return; }
      globalSearchActive = false;
      document.querySelector('.pager').style.display = '';
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
      const generation = ++queryGeneration;
      if (reset) offset = 0;
      document.querySelector('.pager').style.display = '';
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
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      if (!data.success) throw new Error(data.error || 'query failed');
      if (generation !== queryGeneration || document.getElementById('q').value.trim() !== (params.get('q') || '')) return;
      total = data.meta.total;
      renderTable(data.data.items);
      document.getElementById('pageinfo').textContent = `${total ? offset + 1 : 0}-${Math.min(offset + limit, total)} / ${total}`;
    }
    function sectionBadgeText(text) {
      document.getElementById('section-badge').textContent = text;
    }
    function renderTable(items) {
      const meta = TABLE_META[tab];
      sectionBadgeText(`${meta.label} · 共 ${total} 条`);
      document.getElementById('tables-area').innerHTML = tableHtml(meta.headers, meta.rows(items), '无记录', meta.widths);
    }
    function page(delta) {
      const next = offset + delta * limit;
      if (next < 0 || next >= total) return;
      offset = next; reload(false);
    }
    function loadAll() {
      loadSummary().catch(e => document.getElementById('summary').textContent = e.message);
      const query = document.getElementById('q').value.trim();
      (query ? searchAll() : reload(false)).catch(e => {
        document.getElementById('tables-area').innerHTML = `<span class="muted">${esc(e.message)}</span>`;
      });
    }
    document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') runQuery(); });
    document.getElementById('type_filter').addEventListener('keydown', e => { if (e.key === 'Enter') { globalSearchActive = false; reload(true); } });
    document.getElementById('tables-area').addEventListener('click', e => {
      const linkEl = e.target.closest('.suite-download-link');
      if (!linkEl) return;
      e.preventDefault();
      startSuiteDownload(linkEl.dataset.url || '');
    });
    document.querySelectorAll('.tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
    try { window.sessionStorage.setItem(UPDATE_MONITOR_TAB_STORAGE_KEY, tab); } catch (_) {}
    loadAll();
