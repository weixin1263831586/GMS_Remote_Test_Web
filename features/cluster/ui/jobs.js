// Cluster job lifecycle chunk (form/workspace/vpn/refresh/reports); loaded by page.html in dependency order.
function clusterDeviceId(workerId,value){const text=String(value||'');return !text||workerId==='auto'||text.startsWith(`${workerId}:`)?text:`${workerId}:${text}`}
function renderJobForm(){
 const worker=document.querySelector('#job-worker'),suite=document.querySelector('#job-suite'),device=document.querySelector('#job-device');if(!worker||!suite||!device)return;
 const previousWorker=worker.value||((clusterWorkspace.scope_mode==='cluster'&&clusterWorkspace.worker_id)||'auto');
 const workers=commandWorkers(),localIndex=workers.findIndex(item=>item.id===localWorkerId()),workerOptions=workers.map(w=>`<option value="${esc(w.id)}">${esc(w.name||w.id)}</option>`);
 worker.innerHTML=localIndex>=0?workerOptions[localIndex]+'<option value="auto">自动选择</option>'+workerOptions.filter((_,index)=>index!==localIndex).join(''):'<option value="auto">自动选择</option>'+workerOptions.join('');
 worker.value=Array.from(worker.options).some(o=>o.value===previousWorker)?previousWorker:'auto';
 updateJobOptions();
}
function updateJobOptions(){
 const wid=document.querySelector('#job-worker').value,suite=document.querySelector('#job-suite'),device=document.querySelector('#job-device');
 const previousSuite=suite.value||clusterWorkspace.suite_key||'',previousDevice=device.value||(clusterWorkspace.device_ids||[])[0]||'';
 const eligibleIds=new Set(commandWorkers().map(worker=>worker.id));
 const availableSuites=state.suites.filter(s=>(wid==='auto'?eligibleIds.has(s.worker_id):s.worker_id===wid)&&s.available);
 suite.innerHTML=[...new Map(availableSuites.map(s=>[s.suite_key,s])).values()].map(s=>`<option value="${esc(s.suite_key)}">${esc(s.suite_type)} ${esc(s.suite_version)}</option>`).join('');
 if(Array.from(suite.options).some(o=>o.value===previousSuite))suite.value=previousSuite;
 device.innerHTML=wid==='auto'?'<option value="">自动选择设备</option>':'<option value="">自动选择设备</option>'+state.devices.filter(d=>d.worker_id===wid&&d.state==='available').map(d=>`<option value="${esc(d.id)}">${esc(d.serial)}</option>`).join('');
 const normalized=clusterDeviceId(wid,previousDevice);if(Array.from(device.options).some(o=>o.value===normalized))device.value=normalized;
 const create=document.querySelector('#create-job');if(create){create.disabled=!commandWorkers().length||!suite.value;create.title=create.disabled?'没有满足条件的在线 Worker 和套件':''}
}
function syncClusterWorkspace(extra={}){
 if(applyingClusterWorkspace)return;const worker=document.querySelector('#job-worker')?.value||'auto',device=document.querySelector('#job-device')?.value||'',suite=document.querySelector('#job-suite')?.value||'';
 window.GmsEmbeddedWorkspace?.update({scope_mode:'cluster',worker_id:worker==='auto'?(clusterWorkspace.worker_id||commandWorkers()[0]?.id||localWorkerId()):worker,device_ids:device?[device]:[],suite_key:suite,origin_page:'cluster',...extra});
}
async function applyClusterWorkspace(next,navigate=false){
 clusterWorkspace={...clusterWorkspace,...(next||{})};applyingClusterWorkspace=true;
 try{renderModeStatus();renderJobForm();const worker=document.querySelector('#job-worker');if(clusterWorkspace.scope_mode==='cluster'&&clusterWorkspace.worker_id&&Array.from(worker.options).some(o=>o.value===clusterWorkspace.worker_id)){worker.value=clusterWorkspace.worker_id;updateJobOptions()}const suite=document.querySelector('#job-suite'),device=document.querySelector('#job-device');if(clusterWorkspace.suite_key&&Array.from(suite.options).some(o=>o.value===clusterWorkspace.suite_key))suite.value=clusterWorkspace.suite_key;const wanted=clusterDeviceId(worker.value,(clusterWorkspace.device_ids||[])[0]);if(wanted&&Array.from(device.options).some(o=>o.value===wanted))device.value=wanted;if(navigate&&clusterWorkspace.cluster_job_id&&state.jobs.some(j=>j.id===clusterWorkspace.cluster_job_id))await showJob(clusterWorkspace.cluster_job_id,false)}finally{applyingClusterWorkspace=false}
}
async function checkLocalVpn(){try{const d=await api('/api/vpn/status');localVpnConnected=Boolean(d.connected)}catch(e){localVpnConnected=null}}
let vpnElevationPromptAt=0;
async function checkWorkerVpn(){
 const localId=localWorkerId();
 const promises=state.workers.filter(w=>w.status!=='offline').map(async w=>{
  try{const d=await api(`/api/cluster/workers/${encodeURIComponent(w.id)}/vpn-status`);workerVpnCache[w.id]=d.connected}catch(e){
   // 403 elevation 需弹提权框，但自动刷新周期会反复命中：
   // 节流为 3 分钟一次，窗口内的后续失败保持 VPN 状态未知。
   if(e?.elevationRequired&&Date.now()-vpnElevationPromptAt>180000){
    vpnElevationPromptAt=Date.now();
    if(typeof window.parent?.requestElevatedAccess==='function'){
     try{
      const granted=await window.parent.requestElevatedAccess('查看 Worker VPN 状态');
      if(granted){workerVpnCache[w.id]=await api(`/api/cluster/workers/${encodeURIComponent(w.id)}/vpn-status`).then(d=>d.connected).catch(()=>null);return}
     }catch(_){/* 弹框失败保持静默降级 */}
    }
   }
   workerVpnCache[w.id]=null
  }
 });
 await Promise.all(promises);
}
function setClusterRefreshBusy(busy){
 ['#refresh','#dash-refresh-charts'].forEach(selector=>{
  const button=document.querySelector(selector);if(!button)return;
  if(!button.dataset.idleLabel)button.dataset.idleLabel=button.textContent||'↻ 刷新';
  button.disabled=busy;
  button.textContent=busy?'刷新中…':button.dataset.idleLabel;
  if(busy)button.setAttribute('aria-busy','true');else button.removeAttribute('aria-busy');
 });
}
async function refresh(showBusy=false){
 if(showBusy)setClusterRefreshBusy(true);
 try{
  if(refreshPromise)return await refreshPromise;
  refreshPromise=(async()=>{
   const requests=[['workers','/api/cluster/workers','workers'],['devices','/api/cluster/devices','devices'],['suites','/api/cluster/suites','suites'],['jobs','/api/cluster/jobs?include_active=true','jobs'],['tests','/api/cluster/worker-tests','tests'],['library','/api/cluster/suite-library','archives'],['status','/api/cluster/status',null]];
   const results=await Promise.allSettled(requests.map(([,path])=>api(path))),errors=[];
   results.forEach((result,index)=>{const [stateKey,,payloadKey]=requests[index];if(result.status==='fulfilled')state[stateKey]=payloadKey?(result.value[payloadKey]||[]):result.value;else errors.push(`${stateKey}: ${result.reason.message}`)});
   clusterInitialRefreshSettled=true;
   checkLocalVpn().catch(()=>{});checkWorkerVpn().then(render).catch(()=>{});render();
   await applyClusterWorkspace(clusterWorkspace);
   if(errors.length)toast(`部分数据刷新失败：${errors.join('；')}`);
  })().finally(()=>{refreshPromise=null});
  return await refreshPromise;
 }finally{
  if(showBusy)setClusterRefreshBusy(false);
 }
}
async function createJob(){try{const device=document.querySelector('#job-device').value;const body={worker_id:document.querySelector('#job-worker').value,suite_key:document.querySelector('#job-suite').value,devices:device?[device]:[],device_count:1};if(!body.worker_id||!body.suite_key)throw new Error('请选择 Worker 和套件');const d=await api('/api/cluster/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast(`任务已创建 ${d.job.id}`);syncClusterWorkspace({cluster_job_id:d.job.id,attempt_id:d.job.current_attempt_id||'',worker_id:d.job.assigned_worker_id||body.worker_id,device_ids:(d.job.leases||[]).map(x=>x.device_id),suite_key:d.job.suite_key||body.suite_key});await refresh();showJob(d.job.id)}catch(e){toast(e.message)}}
function displayJobData(job){
 const client=String(job?.client_display_id||job?.owner_id||'unknown');
 const convert=value=>{
  if(Array.isArray(value))return value.map(convert);
  if(!value||typeof value!=='object')return value;
  const result={};
  Object.entries(value).forEach(([key,item])=>{
   if(key==='client_display_id')return;
   if(key==='owner_id'){if(!Object.hasOwn(result,'client'))result.client=client;return}
   result[key]=convert(item);
  });
  return result;
 };
 return convert(job);
}
function clusterReportContext(job,artifactId=''){
 const attemptId=job.current_attempt_id||'';
 return {scope_mode:'cluster',worker_id:job.assigned_worker_id,cluster_job_id:job.id,attempt_id:attemptId,report_id:attemptId?`cluster:${job.id}:${attemptId}`:'',report_timestamp:`cluster-${job.id}`,artifact_id:artifactId,origin_page:terminalJob(job.status)?'reports':'cluster'};
}
function openClusterJobReport(job=selectedClusterJob,artifactId=''){
 if(!job)return;
 const context=clusterReportContext(job,artifactId);
 syncClusterWorkspace(context);
 if(typeof window.parent?.analyzeReport==='function')window.parent.analyzeReport(context.report_timestamp,context.report_id);
 else window.GmsEmbeddedWorkspace?.navigate('reports',context);
}
async function getClusterJobReport(job=selectedClusterJob){
 if(!job)return null;
 if(selectedClusterReport?.cluster_job_id===job.id&&(!job.current_attempt_id||selectedClusterReport.attempt_id===job.current_attempt_id))return selectedClusterReport;
 const params=new URLSearchParams({cluster_job_id:job.id});
 if(job.current_attempt_id)params.set('attempt_id',job.current_attempt_id);
 const data=await api(`/api/reports/list?${params.toString()}`);
 selectedClusterReport=(data.reports||[])[0]||null;
 return selectedClusterReport;
}
function clusterReportWorkspace(job,report,artifactId=''){
 return {...clusterReportContext(job,artifactId),report_id:report.report_id||'',report_timestamp:report.timestamp||'',source_timestamp:report.source_timestamp||'',report_name:report.report_name||'',suite_path:report.suite_path||job.suite_path||'',artifact_id:artifactId};
}
async function openClusterJobReportFile(job=selectedClusterJob,artifactId='',filename='test_result_failures_suite.html'){
 if(!job)return;
 try{
  const report=await getClusterJobReport(job);
  if(!report)throw new Error('尚未找到该任务对应的测试报告');
  const context=clusterReportWorkspace(job,report,artifactId);
  syncClusterWorkspace(context);
  const testType=report.test_type||String(job.suite_key||'').split(':',1)[0];
  if(typeof window.parent?.openReportSuiteDirectory==='function')await window.parent.openReportSuiteDirectory(report.timestamp,context.suite_path,testType,'results',context,filename);
  else window.GmsEmbeddedWorkspace?.navigate('test-suites',context);
 }catch(error){toast(`打开失败报告文件失败：${error.message}`)}
}
async function downloadClusterJobReport(job=selectedClusterJob,artifactId=''){
 if(!job)return;
 try{
  const report=await getClusterJobReport(job);
  if(!report)throw new Error('尚未找到该任务对应的测试报告');
  const context=clusterReportWorkspace(job,report,artifactId);
  syncClusterWorkspace(context);
  if(typeof window.parent?.downloadReport==='function')await window.parent.downloadReport(report.timestamp,report.report_id||'',report.report_name||'');
  else window.GmsEmbeddedWorkspace?.navigate('reports',context);
 }catch(error){toast(`下载测试报告失败：${error.message}`)}
}
function artifactDownloadUrl(jobId,artifact){return `/api/cluster/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifact.id)}/download`}
function renderJobArtifacts(job,artifacts,report=null){
 const list=Array.isArray(artifacts)?artifacts:[];
 if(!list.length)return '<span class="empty">暂无任务产物</span>';
 const stdout=list.find(item=>item.filename==='stdout.log');
 const stderr=list.find(item=>item.filename==='stderr.log');
 const reportFiles=list.filter(item=>item.artifact_type==='report');
 const reportArchive=list.find(item=>item.artifact_type==='report-archive'||item.filename==='tradefed-results.zip');
 const handled=new Set([stdout,stderr,...reportFiles,reportArchive].filter(Boolean).map(item=>item.id));
 const entries=[];
 if(stdout)entries.push(`<a class="job-artifact-link" href="${artifactDownloadUrl(job.id,stdout)}" title="下载 stdout.log">测试终端日志 (${Math.max(0,Number(stdout.size_bytes)||0)} bytes)</a>`);
 if(stderr)entries.push(`<a class="job-artifact-link" href="${artifactDownloadUrl(job.id,stderr)}" title="下载 stderr.log">stderr.log (${Math.max(0,Number(stderr.size_bytes)||0)} bytes)</a>`);
 if(reportFiles.length)entries.push(`<button type="button" class="job-artifact-link" data-action="open-job-report-file" data-artifact-id="${esc(reportFiles[0].id)}" data-filename="test_result_failures_suite.html" title="定位失败报告文件">test_result_failures_suite.html</button>`);
 if(reportArchive){const rn=esc(report?.report_name||'');entries.push(`<button type="button" class="job-artifact-link" data-action="download-job-report" data-artifact-id="${esc(reportArchive.id)}" title="按报告管理规则下载 results 和 logs">测试报告(${rn||'下载'})</button>`);}
 list.filter(item=>!handled.has(item.id)).forEach(item=>entries.push(`<a class="job-artifact-link" href="${artifactDownloadUrl(job.id,item)}">${esc(item.filename)} (${Math.max(0,Number(item.size_bytes)||0)} bytes)</a>`));
 return entries.join('<span class="job-artifact-separator">&nbsp;&nbsp;&nbsp;&nbsp;</span>');
}
async function showJob(id,scroll=true){
 try{
  const reportParams=new URLSearchParams({cluster_job_id:id});
  const [j,e,a,r]=await Promise.all([api(`/api/cluster/jobs/${id}`),api(`/api/cluster/jobs/${id}/events`),api(`/api/cluster/jobs/${id}/artifacts`),api(`/api/reports/list?${reportParams.toString()}`).catch(()=>({reports:[]}))]);
  selectedClusterJob=j.job;
  selectedClusterReport=(r.reports||[]).find(item=>!j.job.current_attempt_id||item.attempt_id===j.job.current_attempt_id)||(r.reports||[])[0]||null;
  document.querySelector('#detail').hidden=false;
  document.querySelector('#job-summary').innerHTML=`<span>任务 ${esc(j.job.id)}</span><span>客户端 ${esc(j.job.client_display_id||'-')}</span><span>Worker ${esc(j.job.assigned_worker_id)}</span><span>状态 ${badge(j.job.status)}</span><span>设备 ${esc((j.job.leases||[]).map(x=>x.serial).join(', ')||'-')}</span><span>套件 ${esc(j.job.suite_key||'-')}</span>`;
  document.querySelector('#job-detail').textContent=JSON.stringify(displayJobData(j.job),null,2);
  document.querySelector('#job-logs').textContent=e.events.map(x=>`[${x.source}] ${x.message}`).join('\n')||'暂无日志';
  document.querySelector('#artifacts').innerHTML=renderJobArtifacts(j.job,a.artifacts,selectedClusterReport);
  const reportArtifact=(a.artifacts||[]).find(item=>String(item.artifact_type||'').startsWith('report'));
  syncClusterWorkspace({...clusterReportContext(j.job,reportArtifact?.id||a.artifacts[0]?.id||''),device_ids:(j.job.leases||[]).map(x=>x.device_id),suite_key:j.job.suite_key||''});
  if(scroll)document.querySelector('#detail').scrollIntoView({behavior:'smooth'});
 }catch(e){toast(e.message)}
}
async function cancelJob(id){try{await api(`/api/cluster/jobs/${id}/cancel`,{method:'POST'});toast('停止命令已下发');await refresh()}catch(e){toast(e.message)}}
async function deleteJob(id){if(!await confirmAction('删除任务历史','确定删除该任务历史及事件记录？'))return;try{await api(`/api/cluster/jobs/${id}`,{method:'DELETE'});toast('任务历史已删除');await refresh()}catch(e){toast(e.message)}}
async function deleteWorker(id){if(!await confirmAction('删除集群主机',`确定停止 ${id} 上的 Worker Agent 并从集群删除？设备和套件记录会移除，但不会删除主机上的测试报告和测试数据。`))return;try{toast(`正在停止 ${id} 的 Worker Agent…`);await api(`/api/cluster/workers/${encodeURIComponent(id)}`,{method:'DELETE'});toast(`${id} 的 Worker Agent 已停止并从集群删除`);await refresh()}catch(e){toast(e.message)}}
async function restartWorkerVnc(id){if(!await confirmAction('重启桌面服务',`重启 ${id} 的桌面 VNC 服务？这会中断当前桌面连接。`))return;try{toast(`正在重启 ${id} 的 VNC…`);const d=await api(`/api/cluster/workers/${encodeURIComponent(id)}/restart-vnc`,{method:'POST'});if(d.success)toast(`${id} 桌面 VNC 已恢复`);else toast(`${id} VNC 重启后 RFB 握手仍失败，请检查远端日志`);await refresh()}catch(e){toast(e.message)}}
async function waitLocalSoftwareTask(taskId,button){for(let i=0;i<900;i++){const d=await api(`/api/cluster/workers/local/software/reconfigure/${encodeURIComponent(taskId)}`),task=d.task||{};if(task.status==='completed')return task;if(task.status==='failed')throw new Error(task.error||'Software 重配置失败');if(button)button.textContent=`配置中 ${i}s`;await new Promise(resolve=>setTimeout(resolve,1000))}throw new Error('Software 重配置超时')}
async function reconfigureLocalSoftware(button){if(!await confirmAction('重新配置本机软件','重新配置 Controller / Local Worker 的 JDK、ADB、Fastboot、AAPT 和 scrcpy？请先停止本机测试。'))return;const original=button?.textContent||'重新配置 Software';if(button)button.disabled=true;try{if(typeof window.parent?.requestElevatedAccess==='function'){const granted=await window.parent.requestElevatedAccess('重新配置 Controller / Local Worker Software',{allowAnonymousDev:true});if(!granted)throw new Error('已取消管理员提权')}toast('已提交本机 Software 重配置任务');const accepted=await api('/api/cluster/workers/local/software/reconfigure',{method:'POST'});await waitLocalSoftwareTask(accepted.task.id,button);toast('本机 Software 已重新配置');await refresh()}catch(e){toast(e.message)}finally{if(button){button.disabled=false;button.textContent=original}}}
