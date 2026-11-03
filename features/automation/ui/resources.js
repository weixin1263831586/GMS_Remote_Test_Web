// Catalog reads never hash, download, archive or delete files without a user action.
function resourceBytes(bytes) {
    const value = Number(bytes) || 0;
    return value >= 1024**3 ? `${(value / 1024**3).toFixed(2)} GiB` : `${(value / 1024**2).toFixed(1)} MiB`;
}

async function loadResourceCatalog() {
    const [library, workers] = await Promise.all([api('/api/cluster/suite-library'), api('/api/cluster/workers')]);
    const select = qs('resource-worker');
    const previous = select.value;
    select.innerHTML = (workers.workers || []).filter(worker => ['online', 'busy'].includes(worker.status)).map(worker => `<option value="${esc(worker.id)}">${esc(worker.name || worker.id)}${worker.capabilities?.suite_cache_sha256_v1 ? '' : '（需升级校验缓存能力）'}</option>`).join('');
    if (Array.from(select.options).some(option => option.value === previous)) select.value = previous;
    qs('resource-catalog').innerHTML = (library.archives || []).map(item => {
        const key = item.name;
        const cached = atsResourceFingerprints[key];
        const verified = cached && cached.size === item.size && cached.modified === item.modified;
        if (!verified) delete atsResourceFingerprints[key];
        const version = item.name.match(/\d+(?:\.\d+)*(?:[_-]r\d+)/i)?.[0] || '未标注';
        return `<article class="run-card"><div class="run-main"><strong>${esc(item.name)}</strong><div class="muted">版本 ${esc(version)} · ${esc(resourceBytes(item.size))} · ${item.complete ? '完整压缩包' : '压缩包不完整'}</div><div class="resource-checksum">${verified ? esc(cached.sha256) : '尚未计算 SHA-256'}</div></div><div class="run-actions"><button type="button" data-click="fingerprintResource" data-r0="el" data-a1="${esc(key)}" ${item.complete ? '' : 'disabled'}>校验 / 指纹</button><button type="button" data-click="warmResource" data-a0="${esc(key)}" ${verified && item.complete ? '' : 'disabled'}>预热到 Worker</button></div></article>`;
    }).join('') || '<div class="empty-state">Controller 套件库暂无压缩包，可在套件管理中添加。</div>';
    await loadRetentionPreview();
}

async function fingerprintResource(button, name) {
    button.disabled = true;
    try {
        toast('正在计算资源指纹，大文件可能需要较长时间');
        atsResourceFingerprints[name] = await api(`/api/cluster/suite-library/${encodeURIComponent(name)}/fingerprint`, {method: 'POST'});
        await loadResourceCatalog();
        toast('资源指纹已计算');
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
}

async function warmResource(name) {
    try {
        const item = atsResourceFingerprints[name];
        const worker = qs('resource-worker').value;
        if (!item || !worker) throw new Error('请先计算指纹并选择 Worker');
        const safe = name.replace(/[^A-Za-z0-9._+-]/g, '_');
        const url = `${window.location.origin}/api/cluster/suite-library-download/${encodeURIComponent(safe)}/${encodeURIComponent(name)}?worker_id=${encodeURIComponent(worker)}`;
        const result = await api('/api/cluster/suites/download', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({worker_id: worker, filename: name, size_bytes: item.size, url, sha256: item.sha256})});
        atsResourceCommands[result.command_id] = {name, worker, status: 'queued'};
        await refreshResourceCommands();
        toast('资源预热任务已提交，缓存命中时不会重新下载');
    } catch (error) { toast(error.message); }
}

async function refreshResourceCommands() {
    for (const [id, transfer] of Object.entries(atsResourceCommands)) {
        if (['completed', 'failed', 'cancelled'].includes(transfer.status)) continue;
        try {
            const data = await api(`/api/cluster/commands/${encodeURIComponent(id)}`);
            const command = data.command || data;
            transfer.status = command.status;
            transfer.cacheHit = Boolean(command.result?.cache_hit);
        } catch (_) { /* Transfer status recovers on the next visible refresh. */ }
    }
    qs('resource-transfers').innerHTML = Object.values(atsResourceCommands).map(item => `<div>${esc(item.name)} → ${esc(item.worker)}：${esc(statusLabel(item.status))}${item.cacheHit ? ' · 缓存命中' : ''}</div>`).join('');
}

async function loadRetentionPreview() {
    const data = await api('/api/cluster/retention/preview');
    atsRetentionPreview = data.items || [];
    qs('retention-archive-days').value = data.policy?.archive_days ?? 7;
    qs('retention-delete-days').value = data.policy?.delete_days ?? 30;
    qs('retention-enabled').checked = Boolean(data.policy?.enabled);
    qs('retention-size').textContent = `当前返回 ${atsRetentionPreview.length} 项，共 ${resourceBytes(data.total_bytes)}；可处理 ${resourceBytes(data.candidate_bytes)}（最多返回 ${data.limited_to || 1000} 项）`;
    qs('retention-preview').innerHTML = atsRetentionPreview.length ? `<table><thead><tr><th>选择</th><th>任务 / 文件</th><th>大小</th><th>建议操作</th><th>固定保留</th></tr></thead><tbody>${atsRetentionPreview.slice(0, 100).map(item => `<tr><td><input type="checkbox" class="retention-selection" value="${esc(item.id)}" aria-label="选择产物 ${esc(item.filename)}" ${item.operation ? '' : 'disabled'}></td><td>${esc(item.job_id)} / ${esc(item.filename)}</td><td>${esc(resourceBytes(item.size_bytes))}</td><td>${item.operation === 'archive' ? '压缩归档' : item.operation === 'delete' ? '删除过期归档日志' : '保留'}</td><td><input type="checkbox" data-change="pinRetainedArtifact" data-r0="el" data-a1="${esc(item.id)}" aria-label="固定保留 ${esc(item.filename)}" ${item.pinned ? 'checked' : ''}></td></tr>`).join('')}</tbody></table>` : '<div class="empty-state">暂无本人任务产物。</div>';
}

async function saveRetentionPolicy() {
    try {
        const body = {enabled: qs('retention-enabled').checked, archive_days: Number(qs('retention-archive-days').value), delete_days: Number(qs('retention-delete-days').value)};
        if (!await confirmTestPlanAction('保存日志保留策略', `归档 ${body.archive_days} 天前的日志；${body.delete_days ? `删除 ${body.delete_days} 天前已归档日志` : '保留所有归档日志'}。\n${body.enabled ? '启用每小时自动处理。' : '保持手动处理。'}测试报告和已固定产物保留。`)) return;
        await api('/api/cluster/retention/policy', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        await loadRetentionPreview();
        toast('日志保留策略已保存');
    } catch (error) { toast(error.message); }
}

async function pinRetainedArtifact(input, id) {
    try {
        await api(`/api/cluster/retention/artifacts/${encodeURIComponent(id)}/pin`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pinned: input.checked})});
        await loadRetentionPreview();
    } catch (error) { input.checked = !input.checked; toast(error.message); }
}

async function applySelectedRetention() {
    try {
        const ids = new Set(Array.from(document.querySelectorAll('.retention-selection:checked')).map(input => input.value));
        const selections = atsRetentionPreview.filter(item => ids.has(item.id)).map(item => ({id: item.id, sha256: item.sha256, operation: item.operation}));
        if (!selections.length || selections.length > 25) throw new Error('请选择 1–25 项预览产物');
        if (!await confirmTestPlanAction('执行日志归档 / 清理', `将处理所选 ${selections.length} 项日志，其中 ${selections.filter(item => item.operation === 'delete').length} 项会删除。执行前会重新检查任务状态、固定标记和指纹。是否继续？`)) return;
        await api('/api/cluster/retention/apply', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({selections})});
        await loadRetentionPreview();
        toast('所选日志归档 / 清理已完成');
    } catch (error) { toast(error.message); }
}
