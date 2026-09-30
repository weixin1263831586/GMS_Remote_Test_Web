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
 document.querySelector('#workers').innerHTML=state.workers.filter(worker=>matches([worker.id,worker.name,worker.hostname,worker.address].join(' '))).map(worker=>{
  const tests=state.tests.filter(test=>test.worker_id===worker.id);
  const hasActiveJob=state.jobs.some(job=>job.assigned_worker_id===worker.id&&!terminalJob(job.status));
  const deleteBlocked=hasActiveJob||Number(worker.running_jobs||0)>0;
  const assessment=workerAssessment(worker);
  const sshHost=worker.capabilities?.ssh_user?worker.capabilities.ssh_user+'@'+(worker.address||worker.hostname):'';
  const configButton=`<button class="worker-config" data-action="worker-config" data-worker-id="${esc(worker.id)}" title="配置参数">⚙ 配置</button>`;
  const menuItems=[];
  if(worker.capabilities?.ssh_user)menuItems.push(`<button data-action="restart-vnc" data-worker-id="${esc(worker.id)}">重启桌面</button>`);
  if(worker.id===localId)menuItems.push(`<button data-action="local-software">重新配置</button>`);
  if(worker.id!==localId)menuItems.push(`<button data-action="redeploy-worker" data-worker-id="${esc(worker.id)}" data-ssh-host="${esc(sshHost)}">重新部署</button>`);
  if(worker.id!==localId)menuItems.push(`<button class="danger" data-action="delete-worker" data-worker-id="${esc(worker.id)}" ${deleteBlocked?'disabled':''}>${deleteBlocked?'删除(请先停测试)':'删除主机'}</button>`);
  const moreMenu=menuItems.length?`<span class="worker-menu-wrap"><button class="worker-menu-toggle" data-action="toggle-worker-menu">⋯</button><div class="worker-menu">${menuItems.join('')}</div></span>`:'';
  return `<div class="card"><div class="card-title"><span>${esc(worker.name||worker.id)}</span><span>${workerBadge(worker)}${configButton}${moreMenu}</span></div><p>${esc(worker.hostname)} · ${esc(worker.id)} · ${esc(worker.address||'')}</p><div class="meta"><div class="meta-row"><span>Agent ${esc(worker.agent_version||'-')}</span><span>心跳 ${relativeTime(worker.last_heartbeat_at)}</span><span>CPU ${oneDecimal(worker.cpu_percent)}%</span><span>内存 ${oneDecimal(worker.memory_percent)}%（可用 ${oneDecimal(worker.memory_available_gb)}G）</span><span>磁盘 ${oneDecimal(worker.disk_free_gb)}G</span><span>系统负载 ${oneDecimal(worker.load_1m)}</span></div><div class="meta-row"><span>设备 ${assessment.devices.length}</span><span>套件 ${state.suites.filter(suite=>suite.worker_id===worker.id&&suite.available).length}</span><span>任务 ${worker.running_jobs}/${worker.max_jobs}（外部 ${worker.external_jobs||0}）</span></div></div><div class="host-assessment">${assessment.labels.map(l=>`<span class="assessment ${l.cls}">${l.text}</span>`).join('')}</div><div class="capabilities"><span class="cap ${worker.capabilities?.ssh_user?'ok':''}">终端 ${worker.capabilities?.ssh_user?'✓':'未配置'}</span><span class="cap ${worker.capabilities?.novnc_port?'ok':''}">noVNC ${worker.capabilities?.novnc_port?'✓':'不可用'}</span><span class="cap ${worker.capabilities?.tradefed?'ok':''}">Tradefed ${worker.capabilities?.tradefed?'✓':'不可用'}</span>${worker.id===localId?`<span class="cap ${localVpnConnected===true?'ok':localVpnConnected===false?'':''}">VPN ${localVpnConnected===true?'✓':localVpnConnected===false?'✗':'…'}</span>`:`<span class="cap ${(workerVpnCache[worker.id])===true?'ok':(workerVpnCache[worker.id])===false?'':''}">VPN ${(workerVpnCache[worker.id])===true?'✓':(workerVpnCache[worker.id])===false?'✗':'…'}</span>`}</div><div class="host-tests">${workerTestsMarkup(worker,tests)}</div>${(worker.warnings||[]).map(value=>`<div class="host-warning">⚠ ${esc(workerWarning(value))}</div>`).join('')}</div>`;
 }).join('')||'<div class="empty">暂无 Worker，请点击“添加主机”查看接入命令</div>';
 document.querySelector('#devices').innerHTML=state.devices.filter(device=>String(device.state||'').toLowerCase()!=='offline'&&matches([device.worker_id,device.serial,device.properties?.model,device.properties?.product].join(' '))).map(device=>{const t=device.transport||'local_usb';const tInfo={'local_usb':['本地','t-local'],'usbip':['USB/IP','t-usbip'],'adb_proxy':['ADB Proxy','t-proxy']}[t]||[t,'t-local'];const p=device.properties||{};let source='-';if(t==='adb_proxy')source=p.adb_proxy_source_name||p.adb_proxy_source_worker_id||'-';else if(t==='usbip')source=p.usbip_source_host||'-';else if(device.worker_id)source=device.worker_id;return `<tr><td>${esc(device.worker_id)}</td><td>${esc(device.serial)}</td><td><span class="status ${esc(tInfo[1])}">${esc(tInfo[0])}</span></td><td>${esc(source)}</td><td>${badge(device.state)}</td><td>${esc(p.model||p.product||'')}</td></tr>`}).join('')||'<tr><td colspan="6" class="empty">暂无匹配的在线设备</td></tr>';
 document.querySelector('#suites').innerHTML=state.suites.filter(suite=>suite.available&&matches([suite.worker_id,suite.suite_type,suite.suite_version,suite.tools_path].join(' '))).map(suite=>`<tr><td>${esc(suite.worker_id)}</td><td>${esc(suite.suite_type)}</td><td>${esc(suite.suite_version)}</td><td title="${esc(suite.tools_path)}">${esc(suite.tools_path)}</td></tr>`).join('')||'<tr><td colspan="4" class="empty">暂无匹配的可用套件</td></tr>';
 const jobFilter=document.querySelector('#job-status-filter')?.value||'';
 document.querySelector('#jobs').innerHTML=state.jobs.filter(job=>(!jobFilter||(jobFilter==='active'?!terminalJob(job.status):job.status===jobFilter))&&matches([job.id,job.client_display_id,job.assigned_worker_id,job.status,(job.leases||[]).map(item=>item.serial).join(' ')].join(' '))).map(job=>{
  const monitorOnly=Boolean(job.monitor_only);
  let action=monitorOnly?'<span class="muted" title="其他用户的运行任务仅供集群监控">只读监控</span>':`<button data-action="show-job" data-job-id="${esc(job.id)}">查看</button> <button data-action="delete-job" data-job-id="${esc(job.id)}">删除</button>`;
  if(!monitorOnly&&job.status==='stopping')action=`<button data-action="show-job" data-job-id="${esc(job.id)}">查看</button> <button disabled>停止中…</button>`;
  else if(!monitorOnly&&!terminalJob(job.status))action=`<button data-action="show-job" data-job-id="${esc(job.id)}">查看</button> <button data-action="cancel-job" data-job-id="${esc(job.id)}">停止</button>`;
  const created=job.created_at?new Date(job.created_at).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'-';
  return `<tr><td title="${esc(job.id)}">${esc(job.id.slice(0,16))}</td><td>${esc(job.client_display_id||'-')}</td><td>${esc(job.assigned_worker_id)}</td><td>${badge(job.status)}</td><td>${esc((job.leases||[]).map(item=>item.serial).join(', '))}</td><td>${esc(created)}</td><td>${action}</td></tr>`;
 }).join('')||'<tr><td colspan="7" class="empty">暂无集群任务</td></tr>';
 renderJobForm();
 if(!activeDeployments.size)renderLibrary();
}
