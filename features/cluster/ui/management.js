// Cluster management tab render chunk; loaded by page.html in dependency order.
function activeDevices(workerId){return state.devices.filter(device=>device.worker_id===workerId&&!['offline','unknown'].includes(device.state))}
const ADMISSION_REASON_LABELS={offline:'离线',draining:'排空中',low_disk:'磁盘不足',low_memory:'内存不足',max_jobs:'任务满载',external_tradefed:'外部Tradefed'};
function admissionReasonSuffix(worker){
 const reasons=(worker.admission_reasons||[]).map(reason=>ADMISSION_REASON_LABELS[reason]||reason);
 return reasons.length?`（${reasons.slice(0,2).join('/')}）`:'';
}
function workerAssessment(worker){
 const devices=activeDevices(worker.id);
 const cpu=Number(worker.cpu_percent||0),memory=Number(worker.memory_percent||0),activity=workerActivity(worker);
 let labels=[];
 if(worker.status==='offline'){labels=[{text:'离线',cls:'bad'}]}
 else if(worker.admission_blocked||worker.status==='draining'){labels=[{text:'已阻止派发'+admissionReasonSuffix(worker),cls:'bad'}]}
 else{
  if(activity.running>0)labels.push({text:activity.external&&activity.managed?'平台/外部测试中':activity.external?'外部测试中':'平台测试中',cls:'warn'});
  if(cpu>=75||memory>=85)labels.push({text:'高负载',cls:'bad'});
  else if(cpu>=40||memory>=70)labels.push({text:'中负载',cls:'warn'});
  if(!labels.length)labels=[{text:'空闲',cls:'ok'}];
 }
 return {devices,labels};
}
function commandWorkers(){
 return state.workers.filter(worker=>{
  if(!state.status.enabled||['offline','draining'].includes(worker.status))return false;
  if(worker.id===localWorkerId())return !String(worker.agent_version||'').startsWith('controller-');
  return Boolean(state.status.remote_dispatch_enabled);
 });
}
function render(){
 const localId=localWorkerId();
 const query=String(document.querySelector('#cluster-search')?.value||'').trim().toLowerCase();
 const matches=value=>!query||String(value||'').toLowerCase().includes(query);
 state.workers.sort((a,b)=>(a.id===localId?-1:b.id===localId?1:String(a.name||a.id||'').localeCompare(String(b.name||b.id||''),undefined,{numeric:true})));
 renderModeStatus();
 renderDashboard();

 const currentWorkerDetails=new Set(Array.from(document.querySelectorAll('#workers details[data-worker-details][open]')).map(el=>el.dataset.workerDetails));
 const filteredWorkers=state.workers.filter(worker=>matches([worker.id,worker.name,worker.hostname,worker.address].join(' ')));
 // Host cards communicate their count without an extra title bar.
 const deviceCountEl=document.querySelector('#management-device-count');
 if(deviceCountEl)deviceCountEl.textContent=`(${state.devices.filter(d=>d.state!=='offline').length})`;
 const suiteCountEl=document.querySelector('#management-suite-count');
 if(suiteCountEl)suiteCountEl.textContent=`(${state.suites.filter(s=>s.available).length})`;
 const workerCards=filteredWorkers.map(worker=>{
  const tests=state.tests.filter(test=>test.worker_id===worker.id);
  const hasActiveJob=state.jobs.some(job=>job.assigned_worker_id===worker.id&&!terminalJob(job.status));
  const deleteBlocked=hasActiveJob||Number(worker.running_jobs||0)>0;
  const assessment=workerAssessment(worker);
  const sshHost=worker.capabilities?.ssh_user?worker.capabilities.ssh_user+'@'+(worker.address||worker.hostname):'';
  const configButton=`<button class="worker-config" data-action="worker-config" data-worker-id="${esc(worker.id)}" title="配置参数">⚙ 配置</button>`;
  const operationButtons=[];
  if(worker.capabilities?.ssh_user)operationButtons.push(`<button class="worker-operation" data-action="restart-vnc" data-worker-id="${esc(worker.id)}">重启桌面</button>`);
  if(worker.id===localId)operationButtons.push('<button class="worker-operation" data-action="local-software">重新配置</button>');
  else{
   operationButtons.push(`<button class="worker-operation" data-action="redeploy-worker" data-worker-id="${esc(worker.id)}" data-ssh-host="${esc(sshHost)}">重新部署</button>`);
   operationButtons.push(`<button class="worker-operation worker-delete" data-action="delete-worker" data-worker-id="${esc(worker.id)}" ${deleteBlocked?'disabled title="该主机存在运行中的测试，请先停止测试"':''}>删除主机</button>`);
  }
  const capability=(label,ok)=>`<span class="cap ${ok===true?'ok':''}">${label} ${ok===true?'✓':ok===false?'×':'…'}</span>`;
  const vpn=worker.id===localId?localVpnConnected:workerVpnCache[worker.id];
  const detailed=`<div class="worker-extra"><div>Agent ${esc(worker.agent_version||'-')} · 最近心跳 ${relativeTime(worker.last_heartbeat_at)}</div><div>可用内存 ${oneDecimal(worker.memory_available_gb)} GiB · 可用磁盘 ${oneDecimal(worker.disk_free_gb)} GiB · 系统负载 ${oneDecimal(worker.load_1m)}</div><div>并发 ${esc(worker.running_jobs)}/${esc(worker.max_jobs)} · 外部 ${esc(worker.external_jobs||0)} · noVNC ${worker.capabilities?.novnc_port?'可用':'不可用'} · Tradefed ${worker.capabilities?.tradefed?'可用':'不可用'}</div><div class="host-tests">${workerTestsMarkup(worker,tests)}</div>${(worker.warnings||[]).map(value=>`<div class="host-warning">⚠ ${esc(workerWarning(value))}</div>`).join('')}</div>`;
  const suiteCount=state.suites.filter(s=>s.worker_id===worker.id&&s.available).length;
  // MemTotal and MemAvailable are measured by the Worker in GiB.
  const total=Number(worker.memory_total_gb),available=Number(worker.memory_available_gb);
  const hasTotal=Number.isFinite(total)&&total>0;
  const hasAvailable=Number.isFinite(available)&&available>=0&&(hasTotal||available>0);
  const usage=hasTotal||hasAvailable?`${oneDecimal(worker.memory_percent)}%`:'—';
  const memoryDetail=`总 ${hasTotal?`${oneDecimal(total)} GiB`:'—'} · 可用 ${hasAvailable?`${oneDecimal(available)} GiB`:'—'}`;
  const diskTotal=Number(worker.disk_total_gb),diskFree=Number(worker.disk_free_gb??NaN);
  const hasDiskTotal=Number.isFinite(diskTotal)&&diskTotal>0;
  const hasDiskFree=Number.isFinite(diskFree)&&diskFree>=0&&(hasDiskTotal||diskFree>0);
  const diskDetail=`总 ${hasDiskTotal?oneDecimal(diskTotal):'—'} · 可用 ${hasDiskFree?oneDecimal(diskFree):'—'}${hasDiskTotal||hasDiskFree?' GiB':''}`;
  const hasDiskPercent=Number.isInteger(worker.disk_percent);
  // Bound remote readings before using them in CSS progress indicators.
  const boundedPercent=value=>{
   const numeric=Number(value);
   return Number.isFinite(numeric)?Math.max(0,Math.min(numeric,100)):0;
  };
  const cpuMeter=boundedPercent(worker.cpu_percent);
  const memoryMeter=hasTotal?boundedPercent(worker.memory_percent):0;
  const diskMeter=hasDiskPercent?boundedPercent(worker.disk_percent):0;
  const tone=assessment.labels.some(label=>label.cls==='bad')?'danger'
   :assessment.labels.some(label=>label.cls==='warn')?'warning':'normal';
  return `<article class="worker-card" data-worker-id="${esc(worker.id)}" data-health="${tone}" aria-label="Worker ${esc(worker.name||worker.id)}">
    <div class="worker-card-header">
     <div class="worker-card-identity">
      <span class="worker-card-icon" aria-hidden="true"><svg class="ui-icon" focusable="false"><use href="/static/icons/ui.svg#server"></use></svg></span>
      <div class="worker-card-identity-copy">
       <strong class="worker-main-name" title="${esc(worker.name||worker.id)}">${esc(worker.name||worker.id)}</strong>
       <div class="worker-card-identity-meta">
        <span class="worker-subline" title="${esc(worker.address||worker.hostname||'-')}">${esc(worker.address||worker.hostname||'-')}</span>
        <details class="worker-meta-details" data-worker-details="${esc(worker.id)}" ${currentWorkerDetails.has(worker.id)?"open":""}><summary title="Agent ${esc(worker.agent_version||'-')} · 点击查看运行详情" aria-label="查看 ${esc(worker.id)} 运行详情">Agent ${esc(worker.agent_version||'-')} ▾</summary>${detailed}</details>
       </div>
      </div>
     </div>
     <div class="worker-actions" role="group" aria-label="${esc(worker.name||worker.id)} 主机操作">${configButton}${operationButtons.join("")}</div>
    </div>
    <div class="worker-card-state">
     <div class="worker-summary-state">${workerBadge(worker)}${assessment.labels.map(l=>`<span class="assessment ${l.cls}">${esc(l.text)}</span>`).join("")}</div>
     <div class="worker-service-list">${capability("终端",Boolean(worker.capabilities?.ssh_user))}${capability("VPN",vpn)}</div>
    </div>
    <div class="worker-card-metrics">
     <div class="worker-card-metric worker-cpu">
      <div class="worker-metric-inline">
       <span class="worker-card-label">CPU</span>
       <strong>${oneDecimal(worker.cpu_percent)}%</strong>
       <div class="worker-meter" role="progressbar" aria-label="CPU 使用率" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${cpuMeter}" style="--meter-width:${cpuMeter}%"><span class="worker-meter-fill"></span></div>
      </div>
     </div>
     <div class="worker-card-metric worker-memory" title="总内存：系统可使用的物理内存（MemTotal）。可用内存：系统估算值（MemAvailable，计入部分可回收缓存）。">
      <div class="worker-metric-inline">
       <span class="worker-card-label">内存</span>
       <strong class="worker-memory-percent">${usage}</strong>
       <div class="worker-meter ${hasTotal?'':'worker-meter-unknown'}" role="progressbar" aria-label="内存使用率" aria-valuemin="0" aria-valuemax="100" ${hasTotal?`aria-valuenow="${memoryMeter}"`:'aria-valuetext="未知"'} style="--meter-width:${memoryMeter}%"><span class="worker-meter-fill"></span></div>
      </div>
      <span class="worker-memory-detail">${memoryDetail}</span>
     </div>
     <div class="worker-card-metric worker-disk" title="采集路径所在分区的磁盘容量（GiB）。使用率按已用空间 ÷（已用空间 + 可用空间）计算。">
      <div class="worker-metric-inline">
       <span class="worker-card-label">磁盘</span>
       <strong class="worker-disk-percent">${hasDiskPercent?`${diskMeter}%`:'—'}</strong>
       <div class="worker-meter ${hasDiskPercent?'':'worker-meter-unknown'}" role="progressbar" aria-label="磁盘使用率" aria-valuemin="0" aria-valuemax="100" ${hasDiskPercent?`aria-valuenow="${diskMeter}"`:'aria-valuetext="未上报"'} style="--meter-width:${diskMeter}%"><span class="worker-meter-fill"></span></div>
      </div>
      <span class="worker-disk-detail" title="${diskDetail}">${diskDetail}</span>
     </div>
     <div class="worker-card-metric worker-counts worker-devices"><span class="worker-card-label">设备</span><strong>${assessment.devices.length}</strong></div>
     <div class="worker-card-metric worker-counts worker-suites"><span class="worker-card-label">套件</span><strong>${suiteCount}</strong></div>
     <div class="worker-card-metric worker-counts worker-jobs"><span class="worker-card-label">任务</span><strong>${esc(worker.running_jobs)}/${esc(worker.max_jobs)}</strong></div>
    </div>
   </article>`;
 }).join('');
 document.querySelector('#workers').setAttribute('aria-busy','false');
 const workerLoadError=clusterFirstLoadFailed('workers')
  ?'<div class="cluster-load-failed" role="alert">Worker 主机列表读取失败，请点击顶部「刷新」重试</div>'
  :null;
 const workerColumns=Math.max(1,Math.min(filteredWorkers.length,3));
 document.querySelector('#workers').innerHTML=workerLoadError||(filteredWorkers.length?`<div class="worker-card-grid" data-columns="${workerColumns}">${workerCards}</div>`:'<div class="empty">暂无匹配 Worker，请检查搜索条件或点击“添加主机”接入</div>');
 document.querySelector('#devices').innerHTML=state.devices.filter(device=>String(device.state||'').toLowerCase()!=='offline'&&matches([device.worker_id,device.serial,device.properties?.model,device.properties?.product].join(' '))).map(device=>{const t=device.transport||'local_usb';const tInfo={'local_usb':['本地','t-local'],'usbip':['USB/IP','t-usbip'],'adb_proxy':['ADB Proxy','t-proxy']}[t]||[t,'t-local'];const p=device.properties||{};let source='-';if(t==='adb_proxy')source=p.adb_proxy_source_name||p.adb_proxy_source_worker_id||'-';else if(t==='usbip')source=p.usbip_source_host||'-';else if(device.worker_id)source=device.worker_id;return `<tr><td>${esc(device.worker_id)}</td><td title="${esc(device.serial)}">${esc(device.serial)}</td><td><span class="status ${esc(tInfo[1])}">${esc(tInfo[0])}</span></td><td>${badge(device.state)}</td><td title="${esc(p.model||p.product||'')}">${esc(p.model||p.product||'')}</td><td title="${esc(source)}">${esc(source)}</td></tr>`}).join('')||clusterLoadFailureRow('devices','设备资源池',6)||'<tr><td colspan="6" class="empty">暂无匹配的在线设备</td></tr>';
 document.querySelector('#suites').innerHTML=state.suites.filter(suite=>suite.available&&matches([suite.worker_id,suite.suite_type,suite.suite_version,suite.tools_path].join(' '))).map(suite=>`<tr><td>${esc(suite.worker_id)}</td><td>${esc(suite.suite_type)}</td><td>${esc(suite.suite_version)}</td><td title="${esc(suite.tools_path)}">${esc(suite.tools_path)}</td></tr>`).join('')||clusterLoadFailureRow('suites','已部署套件',4)||'<tr><td colspan="4" class="empty">暂无匹配的可用套件</td></tr>';
 const jobFilter=document.querySelector('#job-status-filter')?.value||'';
 document.querySelector('#jobs').innerHTML=state.jobs.filter(job=>(!jobFilter||(jobFilter==='active'?!terminalJob(job.status):job.status===jobFilter))&&matches([job.id,job.client_display_id,job.assigned_worker_id,job.status,(job.leases||[]).map(item=>item.serial).join(' ')].join(' '))).map(job=>{
  const monitorOnly=Boolean(job.monitor_only);
  let action=monitorOnly?'<span class="muted" title="其他用户的运行任务仅供集群监控">只读监控</span>':`<button data-action="show-job" data-job-id="${esc(job.id)}">查看</button> <button data-action="delete-job" data-job-id="${esc(job.id)}">删除</button>`;
  if(!monitorOnly&&job.status==='stopping')action=`<button data-action="show-job" data-job-id="${esc(job.id)}">查看</button> <button disabled>停止中…</button>`;
  else if(!monitorOnly&&!terminalJob(job.status))action=`<button data-action="show-job" data-job-id="${esc(job.id)}">查看</button> <button data-action="cancel-job" data-job-id="${esc(job.id)}">停止</button>`;
  const created=job.created_at?new Date(job.created_at).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'-';
  return `<tr><td title="${esc(job.id)}">${esc(job.id.slice(0,16))}</td><td>${esc(job.client_display_id||'-')}</td><td>${esc(job.assigned_worker_id)}</td><td>${badge(job.status)}</td><td>${esc((job.leases||[]).map(item=>item.serial).join(', '))}</td><td>${esc(created)}</td><td>${action}</td></tr>`;
 }).join('')||clusterLoadFailureRow('jobs','集群任务监控',7)||'<tr><td colspan="7" class="empty">暂无集群任务</td></tr>';

 // Refresh preserves expanded worker diagnostics; update fixed popover position.
 positionOpenWorkerDetails();
 for(const id of ['devices','suites','jobs'])document.getElementById(id)?.setAttribute('aria-busy','false');
 if(!activeDeployments.size)renderLibrary();
}

// Worker diagnostics are viewport-anchored so their popover remains readable
// even when the compact Worker list becomes internally scrollable.
function positionOpenWorkerDetails(){
 document.querySelectorAll('#workers .worker-meta-details[open]').forEach(details=>{
  const anchor=details.querySelector('summary');
  const popup=details.querySelector('.worker-extra');
  if(!anchor||!popup)return;
  const box=anchor.getBoundingClientRect();
  const width=Math.min(360,window.innerWidth-24);
  const left=Math.max(12,Math.min(box.left,window.innerWidth-width-12));
  const maxHeight=Math.min(window.innerHeight*.55,340);
  const needed=Math.min(popup.scrollHeight,maxHeight);
  const below=box.bottom+8;
  const top=below+needed<=window.innerHeight-8?below:Math.max(8,box.top-8-needed);
  popup.style.setProperty('--worker-popover-left',left+'px');
  popup.style.setProperty('--worker-popover-top',top+'px');
 });
}
