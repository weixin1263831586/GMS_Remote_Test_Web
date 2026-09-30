// Cluster suite library and deployment chunk; loaded by page.html in dependency order.
function formatBytes(v){const n=Number(v)||0;if(n>=1073741824)return `${(n/1073741824).toFixed(2)} GB`;if(n>=1048576)return `${(n/1048576).toFixed(1)} MB`;return `${n} B`}
function archiveFolder(name){return String(name).replace(/\.(tar\.gz|tar\.bz2|zip|tgz|tar)$/i,'').replace(/[^A-Za-z0-9._+-]+/g,'_')}
function renderLibrary(){
 const body=document.querySelector('#library');if(!body)return;
 // 保存当前草稿（目标 Worker、目录名、打开的菜单与焦点），
 // 后台每 10 秒的 refresh 会触发整体重绘——不保留时未提交的下发
 // 草稿被重置为默认目录、目标回到第一台 Worker。
 // 草稿按压缩包稳定标识（name|size）而非数组下标保存：清单前插入
 // 新压缩包后，按 i 恢复会把 cts.zip 的草稿套到 new-archive.zip 上。
 const drafts={};
 state.library.forEach(a=>{
  const key=archiveKey(a);
  const row=document.querySelector(`#library tr[data-archive-key="${cssEscape(key)}"]`);
  if(!row)return;
  const w=row.querySelector('select.library-worker'),f=row.querySelector('input.library-folder');
  if(w)drafts[key]={worker:w.value,folder:f?f.value:''};
 });
 // 菜单实际用 .open class 控制（toggle-worker-menu handler）；之前查
 // .worker-menu:not([hidden]) 永远落空，重绘后打开的菜单会丢失，
 // 合成 click 恢复还会误触其他菜单。这里记录 worker id 后直接恢复
 // .open class。
 const openMenuWrap=document.querySelector('#workers .worker-menu-wrap.open');
 const openMenuWorker=openMenuWrap?.closest('.card')
   ?.querySelector('[data-action="worker-config"]')?.getAttribute('data-worker-id')||'';
 const active=document.activeElement;
 const activeKey=active?.closest?.('tr')?.getAttribute('data-archive-key')||'';
 const activeField=active?(active.tagName==='INPUT'?'folder':active.tagName==='SELECT'?'worker':''):'';
 const workers=commandWorkers();
 body.innerHTML=state.library.map((a,i)=>{
  const key=archiveKey(a);
  // 同时保留 id（library-worker-${i}）与 class：既有冒烟测试与
  // deployArchive 兼容旧 id 选择器；草稿保存/恢复已改用 archive key。
  return `<tr data-archive-key="${esc(key)}"><td title="${esc(a.name)}">${esc(a.name)}${a.complete===false?' <span style="color:#c62828" title="压缩包不完整（可能下载中断），无法下发">不完整</span>':''}</td><td>${formatBytes(a.size)}</td><td>${new Date(a.modified*1000).toLocaleString()}</td><td><select id="library-worker-${i}" class="library-worker">${workers.map(w=>`<option value="${esc(w.id)}">${esc(w.name||w.id)}</option>`).join('')}</select></td><td><input id="library-folder-${i}" class="library-folder" value="${esc(archiveFolder(a.name))}"></td><td><button class="primary" data-action="deploy-archive" data-archive-key="${esc(key)}" ${workers.length&&a.complete!==false?'':'disabled title="没有可接收命令的在线 Worker 或压缩包不完整"'}>下发并解压</button> <span class="progress-wrap"><span class="progress-track"><span class="progress-bar"></span></span><span class="deploy-progress"></span></span></td></tr>`;
 }).join('')||'<tr><td colspan="6" class="empty">Controller 套件目录中没有压缩包</td></tr>';
 // 恢复草稿：仅当条目仍存在且选项仍然有效。
 Object.keys(drafts).forEach(key=>{
  const row=document.querySelector(`#library tr[data-archive-key="${cssEscape(key)}"]`);
  if(!row)return;
  const w=row.querySelector('select.library-worker'),f=row.querySelector('input.library-folder');
  if(!w)return;
  if(drafts[key].worker&&Array.from(w.options).some(o=>o.value===drafts[key].worker))w.value=drafts[key].worker;
  if(f&&drafts[key].folder)f.value=drafts[key].folder;
 });
 // 恢复焦点（正在编辑的下发草稿输入框/下拉框），光标落在文本末尾。
 if(activeKey&&activeField){
  const row=document.querySelector(`#library tr[data-archive-key="${cssEscape(activeKey)}"]`);
  const restored=row?.querySelector(activeField==='folder'?'input.library-folder':'select.library-worker');
  if(restored&&restored!==document.body){
   restored.focus();
   if(activeField==='folder'&&typeof restored.setSelectionRange==='function'&&restored.value.length){
    try{restored.setSelectionRange(restored.value.length,restored.value.length)}catch(e){}
   }
  }
 }
 // 恢复打开的菜单：直接恢复 .open class，不经由合成 click。
 if(openMenuWorker){
  document.querySelector(
    `#workers .card [data-action="worker-config"][data-worker-id="${cssEscape(openMenuWorker)}"]`)
   ?.closest('.card')?.querySelector('.worker-menu-wrap')?.classList.add('open');
 }
}
function archiveKey(a){return `${a.name}|${a.size}`}
function cssEscape(value){return window.CSS&&CSS.escape?CSS.escape(String(value)):String(value).replace(/([^\w-])/g,'\\$1')}
async function loadLibrary(){const button=document.querySelector('#reload-library'),original=button?.textContent||'↻ 刷新测试套件';if(button){button.disabled=true;button.textContent='刷新中…';button.setAttribute('aria-busy','true')}try{const d=await api('/api/cluster/suite-library');state.library=d.archives||[];renderLibrary();toast('测试套件已更新')}catch(e){toast(e.message)}finally{if(button){button.disabled=false;button.textContent=original;button.removeAttribute('aria-busy')}}}
async function waitCommand(id,progress,onProgress){for(let i=0;i<7200;i++){const d=await api(`/api/cluster/commands/${encodeURIComponent(id)}`),c=d.command;if(c.status==='completed')return c.result||{};if(['failed','cancelled'].includes(c.status))throw new Error(c.error||`${c.command_type}失败`);if(onProgress&&c.result?.downloaded_bytes)onProgress(c.result);else if(i%10===0)progress.textContent=`处理中 ${Math.floor(i/10)}s`;await new Promise(r=>setTimeout(r,1000))}throw new Error('操作超时')}
async function deployArchive(key){
 // 定位下发条目用稳定 archive key，不再用数组下标（清单刷新
 // 重排后下标会指向另一份压缩包）。
 const archive=state.library.find(a=>archiveKey(a)===key);
 if(!archive)return;
 const row=document.querySelector(`#library tr[data-archive-key="${cssEscape(key)}"]`);
 const worker=row?.querySelector('select.library-worker')?.value,folder=row?.querySelector('input.library-folder')?.value.trim(),progress=row?.querySelector('.deploy-progress'),bar=row?.querySelector('.progress-bar');if(!worker||!folder)return;activeDeployments.add(key);
 try{
  bar.className='progress-bar';bar.style.width='2%';progress.textContent=`准备下发 ${formatBytes(archive.size)}…`;
  const ext=archive.name.toLowerCase().endsWith('.tar.gz')?'.tar.gz':(archive.name.match(/\.[A-Za-z0-9]+$/)?.[0]||'.zip'),safe=`suite-${Math.abs([...archive.name].reduce((a,c)=>((a<<5)-a+c.charCodeAt(0))|0,0))}${ext}`;
  const url=`${location.origin}/api/cluster/suite-library-download/${safe}/${encodeURIComponent(archive.name)}?worker_id=${encodeURIComponent(worker)}`;
  const accepted=await api('/api/cluster/suites/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({worker_id:worker,url,filename:archive.name,size_bytes:archive.size})});
  const downloaded=await waitCommand(accepted.command_id,progress,p=>{const total=p.total_bytes||archive.size,ratio=total?Math.min(p.downloaded_bytes/total,1):0;bar.style.width=`${Math.max(2,Math.round(ratio*80))}%`;progress.textContent=`正在下发 ${formatBytes(p.downloaded_bytes)} / ${formatBytes(total)} (${Math.round(ratio*100)}%)`});
  bar.style.width='82%';progress.textContent='下载完成，正在解压…';
  const extracted=await api('/api/cluster/suites/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({worker_id:worker,archive_path:downloaded.archive_path,target_dir_name:folder})});
  await waitCommand(extracted.command_id,progress);bar.style.width='';bar.className='progress-bar done';progress.textContent='部署完成';toast(`${archive.name} 已部署到 ${worker}`)
 }catch(e){bar.style.width='';bar.className='progress-bar failed';progress.textContent=`失败：${e.message}`;toast(e.message)}finally{setTimeout(()=>{activeDeployments.delete(key);renderLibrary()},15000)}
}
