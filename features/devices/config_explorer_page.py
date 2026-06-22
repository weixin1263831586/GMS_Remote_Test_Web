"""Static HTML for the config explorer tool page.

Kept as a Python string so the router can serve it as HTMLResponse without
adding files to the static/ tree. Self-contained: defines its own theme to
match the platform look (dark/light via prefers-color-scheme) and talks to
the /api/config-explorer* endpoints on the same origin.
"""

CONFIG_EXPLORER_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>配置资源查看器 - GMS远程测试</title>
<style>
  :root {
    --bg-color: #f5f7fa;
    --card-bg: #ffffff;
    --border-color: #e0e4e8;
    --text-primary: #1a1a1a;
    --text-secondary: #4a4a4a;
    --text-muted: #888;
    --primary-color: #4285f4;
    --success-color: #34a853;
    --danger-color: #ea4335;
    --warning-color: #fbbc04;
    --light-bg: #f0f2f5;
    --shadow-sm: 0 1px 3px rgba(0,0,0,0.08);
    --shadow-md: 0 4px 12px rgba(0,0,0,0.1);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg-color: #1a1a1a;
      --card-bg: #2a2a2a;
      --border-color: #3a3a3a;
      --text-primary: #e8e8e8;
      --text-secondary: #b0b0b0;
      --text-muted: #808080;
      --light-bg: #333;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg-color); color: var(--text-primary);
    font-size: 14px; line-height: 1.5;
  }
  .header {
    background: var(--card-bg); border-bottom: 1px solid var(--border-color);
    padding: 12px 20px; display: flex; align-items: center; justify-content: space-between;
    position: sticky; top: 0; z-index: 10; box-shadow: var(--shadow-sm);
  }
  .header h1 { font-size: 18px; font-weight: 600; }
  .header .sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
  .toolbar {
    background: var(--card-bg); border-bottom: 1px solid var(--border-color);
    padding: 12px 20px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  }
  .toolbar label { font-size: 12px; color: var(--text-secondary); font-weight: 500; }
  select, input[type="text"] {
    background: var(--bg-color); color: var(--text-primary);
    border: 1px solid var(--border-color); border-radius: 5px;
    padding: 6px 10px; font-size: 13px; min-width: 140px;
  }
  input[type="text"] { min-width: 220px; }
  .btn {
    padding: 6px 16px; border: none; border-radius: 5px; font-size: 13px;
    font-weight: 500; cursor: pointer; transition: opacity 0.2s;
  }
  .btn-primary { background: var(--primary-color); color: #fff; }
  .btn:hover { opacity: 0.85; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .checkbox-wrap { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-secondary); }
  .stat-bar {
    padding: 8px 20px; background: var(--light-bg); border-bottom: 1px solid var(--border-color);
    font-size: 12px; color: var(--text-secondary); display: flex; gap: 20px; flex-wrap: wrap;
  }
  .stat-bar b { color: var(--text-primary); }
  .table-wrap { padding: 12px 20px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; background: var(--card-bg);
    border-radius: 6px; overflow: hidden; box-shadow: var(--shadow-sm); }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border-color);
    font-size: 13px; vertical-align: top; }
  th { background: var(--light-bg); font-weight: 600; position: sticky; top: 0; }
  tr:hover td { background: var(--light-bg); }
  td.value { font-family: "SF Mono", Consolas, "Liberation Mono", Menlo, monospace; font-size: 12px; }
  .changed { color: var(--warning-color); font-weight: 600; }
  .tag { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px;
    font-weight: 500; background: var(--light-bg); color: var(--text-secondary); }
  .tag-overlayed { background: var(--warning-color); color: #000; }
  .empty, .loading { text-align: center; padding: 60px 20px; color: var(--text-muted); }
  .loading::after { content: ''; display: inline-block; width: 16px; height: 16px;
    border: 2px solid var(--border-color); border-top-color: var(--primary-color);
    border-radius: 50%; animation: spin 0.8s linear infinite; vertical-align: middle; margin-left: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .search-hint { font-size: 11px; color: var(--text-muted); }
  .note { padding: 8px 20px; font-size: 12px; color: var(--text-muted);
    background: var(--light-bg); border-bottom: 1px solid var(--border-color); }
  code { background: var(--light-bg); padding: 1px 5px; border-radius: 3px;
    font-family: "SF Mono", Consolas, monospace; font-size: 12px; }
</style>
</head>
<body>
  <div class="header">
    <div>
      <h1>🧩 配置资源查看器</h1>
      <div class="sub">查看设备上 <code>config_*</code> 等框架资源的 APK 默认值，以及经 vendor overlay 覆盖后的生效值</div>
    </div>
  </div>

  <div class="toolbar">
    <label>设备</label>
    <select id="device"><option value="">加载中...</option></select>
    <label>包名</label>
    <select id="package"><option value="android">android</option></select>
    <input type="text" id="name" placeholder="资源名过滤（如 config_supports）">
    <label>类型</label>
    <select id="type">
      <option value="">全部</option>
      <option value="bool">bool</option>
      <option value="integer">integer</option>
      <option value="string">string</option>
      <option value="dimen">dimen</option>
      <option value="array">array</option>
    </select>
    <span class="checkbox-wrap">
      <input type="checkbox" id="configOnly" checked> 仅 config_*
    </span>
    <span class="checkbox-wrap" title="计算 overlay 生效值：每个资源调用一次 adb，较慢">
      <input type="checkbox" id="withEffective"> 含 overlay 生效值
    </span>
    <button class="btn btn-primary" id="queryBtn" onclick="runQuery()">查询</button>
  </div>

  <div class="stat-bar" id="statBar" style="display:none;"></div>
  <div class="note" id="note">提示：默认只显示 <code>config_*</code> 资源的 APK 默认值。勾选「含 overlay 生效值」可对比 vendor overlay 改了哪些配置（后端并发查询，全量约1-2分钟）。</div>

  <div class="table-wrap">
    <div id="results" class="empty">选择设备并点击「查询」开始</div>
  </div>

<script>
const api = (p) => p; // same origin, relative URLs

async function loadDevices() {
  try {
    const r = await fetch(api('/api/config-explorer/devices'));
    const j = await r.json();
    const sel = document.getElementById('device');
    const devs = (j.data && j.data.devices) || [];
    sel.innerHTML = devs.length
      ? devs.map(d => `<option value="${d.serial}">${d.serial} (${d.state})</option>`).join('')
      : '<option value="">无设备</option>';
    if (devs.length) sel.value = devs[0].serial;
    loadPackages();
  } catch (e) { document.getElementById('device').innerHTML = '<option value="">加载失败</option>'; }
}

async function loadPackages() {
  const device = document.getElementById('device').value;
  try {
    const r = await fetch(api('/api/config-explorer/packages?device_id=' + encodeURIComponent(device)));
    const j = await r.json();
    const pkgs = (j.data && j.data.packages) || ['android'];
    document.getElementById('package').innerHTML =
      pkgs.map(p => `<option value="${p}">${p}</option>`).join('');
  } catch (e) { /* keep default android */ }
}

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function runQuery() {
  const device = document.getElementById('device').value;
  const pkg = document.getElementById('package').value || 'android';
  const name = document.getElementById('name').value.trim();
  const type = document.getElementById('type').value;
  const configOnly = document.getElementById('configOnly').checked;
  const withEffective = document.getElementById('withEffective').checked;

  if (!device) { alert('未检测到设备'); return; }

  const params = new URLSearchParams({ package: pkg, device_id: device, config_only: configOnly, with_effective: withEffective });
  if (name) params.set('name', name);
  if (type) params.set('type', type);

  const btn = document.getElementById('queryBtn');
  btn.disabled = true;
  const results = document.getElementById('results');
  results.className = 'loading';
  results.innerHTML = withEffective ? '正在查询生效值（逐个 adb 调用，请稍候）' : '查询中';

  try {
    const r = await fetch(api('/api/config-explorer?' + params));
    const j = await r.json();
    if (!j.success) { results.className = 'empty'; results.innerHTML = '❌ ' + escapeHtml(j.error || j.message || '查询失败'); return; }
    renderResults(j.data, withEffective);
  } catch (e) {
    results.className = 'empty';
    results.innerHTML = '❌ 请求失败: ' + escapeHtml(e.message);
  } finally {
    btn.disabled = false;
  }
}

function renderResults(data, withEffective) {
  const res = data.resources || [];
  const statBar = document.getElementById('statBar');
  const note = document.getElementById('note');

  if (!res.length) {
    document.getElementById('results').className = 'empty';
    document.getElementById('results').innerHTML = '没有匹配的资源';
    statBar.style.display = 'none';
    return;
  }

  let stat = `共 <b>${data.total}</b> 个资源`;
  if (data.apk_path) stat += ` · APK: <code>${escapeHtml(data.apk_path)}</code>`;
  if (withEffective) {
    stat += ` · 被 overlay 修改: <b>${data.overlayed_count}</b>`;
  }
  statBar.innerHTML = stat;
  statBar.style.display = 'flex';
  note.style.display = 'none';

  const effCol = withEffective
    ? '<th>生效值 (overlay)</th><th>状态</th><th>overlay来源</th>'
    : '';
  let rows = res.map(e => {
    const changed = e.overlay_changed === true;
    // 未被 overlay 覆盖时，生效值列留空（lookup 返回值与默认值相同，无意义）
    const effDisplay = changed
      ? escapeHtml(e.effective_value)
      : '<span style="color:var(--text-muted);">—</span>';
    // overlay 来源：只有被修改时才显示来源包名
    const srcDisplay = changed && e.overlay_source
      ? escapeHtml(e.overlay_source)
      : '<span style="color:var(--text-muted);">—</span>';
    const status = e.lookup_error
      ? `<span class="tag" title="${escapeHtml(e.lookup_error)}">错误</span>`
      : (changed ? '<span class="tag tag-overlayed">已修改</span>'
                 : (e.overlay_changed === false ? '<span class="tag">默认</span>' : ''));
    const effCell = withEffective
      ? `<td class="value ${changed ? 'changed' : ''}">${effDisplay}</td><td>${status}</td><td class="value">${srcDisplay}</td>`
      : '';
    return `<tr>
      <td><code>${escapeHtml(e.name)}</code></td>
      <td><span class="tag">${escapeHtml(e.type)}</span></td>
      <td class="value">${escapeHtml(e.default_value)}</td>
      ${effCell}
    </tr>`;
  }).join('');

  document.getElementById('results').className = '';
  document.getElementById('results').innerHTML =
    `<table><thead><tr><th>资源名</th><th>类型</th><th>默认值 (APK)</th>${effCol}</tr></thead><tbody>${rows}</tbody></table>`;
}

document.getElementById('device').addEventListener('change', loadPackages);
document.getElementById('name').addEventListener('keydown', e => { if (e.key === 'Enter') runQuery(); });
loadDevices();
</script>
</body>
</html>
"""
