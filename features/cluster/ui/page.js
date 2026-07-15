const state={workers:[],devices:[],suites:[],jobs:[],tests:[],library:[],status:{local_worker_id:'worker-local'}};
const activeDeployments=new Set();
let clusterWorkspace={};
let selectedClusterJob=null;
let applyingClusterWorkspace=false;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const oneDecimal=value=>(Number(value)||0).toFixed(1);
async function api(path,options){const r=await fetch(path,options),text=await r.text();let d={};try{d=text?JSON.parse(text):{}}catch(_){d={detail:text||r.statusText}}if(!r.ok||d.success===false)throw new Error(d.detail||d.error||`HTTP ${r.status}`);return d}
function toast(message){const el=document.querySelector('#toast');el.textContent=message;el.style.display='block';setTimeout(()=>el.style.display='none',3000);if(message==='Worker 已安装并成功注册')notifyCompletion('测试主机部署完成',message);else if(message.includes(' 已部署到 '))notifyCompletion('测试套件部署完成',message)}
function notifyCompletion(title,message){window.parent.postMessage({type:'cluster-notification',title,message,level:'success'},location.origin)}
function badge(s){return `<span class="status ${esc(s)}">${esc(s)}</span>`}
function localWorkerId(){return state.status.local_worker_id||'worker-local'}
function terminalJob(status){return ['completed','failed','cancelled'].includes(status)}
function activeDevices(workerId){return state.devices.filter(device=>device.worker_id===workerId&&device.state!=='offline')}
function workerAssessment(worker){
 const devices=activeDevices(worker.id);
 const cpu=Number(worker.cpu_percent||0),memory=Number(worker.memory_percent||0),running=Number(worker.running_jobs||0);
 let loadLabel='空闲',loadClass='ok';
 if(worker.status==='offline'){loadLabel='离线';loadClass='bad'}
 else if(cpu>=75||memory>=85){loadLabel='高负载';loadClass='bad'}
 else if(running>0){loadLabel='测试中';loadClass='warn'}
 else if(cpu>=40||memory>=70){loadLabel='中负载';loadClass='warn'}
 return {devices,loadLabel,loadClass};
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
 state.workers.sort((a,b)=>(a.id===localId?-1:b.id===localId?1:String(a.registered_at||'').localeCompare(String(b.registered_at||''))));
 const modeHint=document.querySelector('#cluster-mode-status');
 if(modeHint)modeHint.textContent=state.status.enabled
  ? `集群模式已启用 · 本机 ${localId} · 远端派发${state.status.remote_dispatch_enabled?'已启用':'未启用'}`
  : `当前为单机模式 · 本机 ${localId}；启用集群模式后可向 Worker 派发任务`;
 document.querySelector('#workers').innerHTML=state.workers.map(worker=>{
  const tests=state.tests.filter(test=>test.worker_id===worker.id);
  const hasActiveJob=state.jobs.some(job=>job.assigned_worker_id===worker.id&&!terminalJob(job.status));
  const deleteBlocked=hasActiveJob||Number(worker.running_jobs||0)>0;
  const assessment=workerAssessment(worker);
  const deleteButton=worker.id===localId?'':` <button class="worker-delete" data-worker-id="${esc(worker.id)}" onclick="deleteWorker(this.dataset.workerId)" ${deleteBlocked?'disabled title="请先停止该主机上的平台或外部测试"':''}>删除主机</button>`;
  const vncButton=worker.capabilities?.ssh_user?` <button class="worker-vnc" data-worker-id="${esc(worker.id)}" onclick="restartWorkerVnc(this.dataset.workerId)" title="重启该主机的 x11vnc/websockify">重启桌面</button>`:'';
  return `<div class="card"><div class="card-title"><span>${esc(worker.name||worker.id)}</span><span>${badge(worker.status)}${deleteButton}${vncButton}</span></div><p>${esc(worker.hostname)} · ${esc(worker.id)} · ${esc(worker.address||'')}</p><div class="meta"><div class="meta-row"><span>CPU ${oneDecimal(worker.cpu_percent)}%</span><span>内存 ${oneDecimal(worker.memory_percent)}%（可用 ${oneDecimal(worker.memory_available_gb)}G）</span><span>磁盘 ${oneDecimal(worker.disk_free_gb)}G</span><span>系统负载 ${oneDecimal(worker.load_1m)}</span></div><div class="meta-row"><span>设备 ${assessment.devices.length}</span><span>套件 ${state.suites.filter(suite=>suite.worker_id===worker.id&&suite.available).length}</span><span>任务 ${worker.running_jobs}/${worker.max_jobs}（外部 ${worker.external_jobs||0}）</span></div></div><div class="host-assessment"><span class="assessment ${assessment.loadClass}">负载：${assessment.loadLabel}</span></div><div class="capabilities"><span class="cap ${worker.capabilities?.ssh_user?'ok':''}">终端 ${worker.capabilities?.ssh_user?'✓':'未配置'}</span><span class="cap ${worker.capabilities?.novnc_port?'ok':''}">noVNC ${worker.capabilities?.novnc_port?'✓':'不可用'}</span><span class="cap ${worker.capabilities?.tradefed?'ok':''}">Tradefed ${worker.capabilities?.tradefed?'✓':'不可用'}</span></div>${(worker.warnings||[]).map(value=>`<div class="host-warning">⚠ ${esc(value)}</div>`).join('')}<div class="host-tests">${tests.map(test=>`<div><strong>${test.source==='external'?'手工/外部':'平台'} ${esc(test.suite_type||'XTS')}</strong> · PID ${esc(test.pid||'-')} · 设备 ${esc((test.devices||[]).join(', ')||'未识别')} · 运行 ${Math.floor((test.elapsed_seconds||0)/3600)}h</div>`).join('')||'<span class="muted">当前无测试</span>'}</div></div>`;
 }).join('')||'<div class="empty">暂无 Worker，请点击“添加主机”查看接入命令</div>';
 document.querySelector('#devices').innerHTML=state.devices.filter(device=>device.state!=='offline').map(device=>`<tr><td>${esc(device.worker_id)}</td><td>${esc(device.serial)}</td><td>${badge(device.state)}</td><td>${esc(device.properties?.model||device.properties?.product||'')}</td></tr>`).join('')||'<tr><td colspan="4" class="empty">暂无在线设备</td></tr>';
 document.querySelector('#suites').innerHTML=state.suites.filter(suite=>suite.available).map(suite=>`<tr><td>${esc(suite.worker_id)}</td><td>${esc(suite.suite_type)}</td><td>${esc(suite.suite_version)}</td><td title="${esc(suite.tools_path)}">${esc(suite.tools_path)}</td></tr>`).join('')||'<tr><td colspan="4" class="empty">暂无可用套件</td></tr>';
 document.querySelector('#jobs').innerHTML=state.jobs.map(job=>{
  let action=` <button onclick="deleteJob('${esc(job.id)}')">删除</button>`;
  if(job.status==='stopping')action=' <button disabled>停止中…</button>';
  else if(!terminalJob(job.status))action=` <button onclick="cancelJob('${esc(job.id)}')">停止</button>`;
  const created=job.created_at?new Date(job.created_at).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'-';
  return `<tr><td>${esc(job.id.slice(0,16))}</td><td>${esc(job.assigned_worker_id)}</td><td>${badge(job.status)}</td><td>${esc((job.leases||[]).map(item=>item.serial).join(', '))}</td><td>${esc(created)}</td><td><button onclick="showJob('${esc(job.id)}')">查看</button>${action}</td></tr>`;
 }).join('')||'<tr><td colspan="6" class="empty">暂无集群任务</td></tr>';
 renderJobForm();
 if(!activeDeployments.size)renderLibrary();
}
function formatBytes(v){const n=Number(v)||0;if(n>=1073741824)return `${(n/1073741824).toFixed(2)} GB`;if(n>=1048576)return `${(n/1048576).toFixed(1)} MB`;return `${n} B`}
function archiveFolder(name){return String(name).replace(/\.(tar\.gz|tar\.bz2|zip|tgz|tar)$/i,'').replace(/[^A-Za-z0-9._+-]+/g,'_')}
function renderLibrary(){const body=document.querySelector('#library');if(!body)return;const workers=commandWorkers();body.innerHTML=state.library.map((a,i)=>`<tr><td title="${esc(a.name)}">${esc(a.name)}</td><td>${formatBytes(a.size)}</td><td>${new Date(a.modified*1000).toLocaleString()}</td><td><select id="library-worker-${i}">${workers.map(w=>`<option value="${esc(w.id)}">${esc(w.name||w.id)}</option>`).join('')}</select></td><td><input id="library-folder-${i}" value="${esc(archiveFolder(a.name))}"></td><td><button class="primary" onclick="deployArchive(${i})" ${workers.length?'':'disabled title="没有可接收命令的在线 Worker"'}>下发并解压</button> <span class="progress-wrap"><span class="progress-track"><span class="progress-bar" id="library-bar-${i}"></span></span><span class="deploy-progress" id="library-progress-${i}"></span></span></td></tr>`).join('')||'<tr><td colspan="6" class="empty">Controller 套件目录中没有压缩包</td></tr>'}
async function loadLibrary(){const d=await api('/api/cluster/suite-library');state.library=d.archives||[];renderLibrary()}
async function waitCommand(id,progress,onProgress){for(let i=0;i<7200;i++){const d=await api(`/api/cluster/commands/${encodeURIComponent(id)}`),c=d.command;if(c.status==='completed')return c.result||{};if(['failed','cancelled'].includes(c.status))throw new Error(c.error||`${c.command_type}失败`);if(onProgress&&c.result?.downloaded_bytes)onProgress(c.result);else if(i%10===0)progress.textContent=`处理中 ${Math.floor(i/10)}s`;await new Promise(r=>setTimeout(r,1000))}throw new Error('操作超时')}
async function deployArchive(index){
 const archive=state.library[index],worker=document.querySelector(`#library-worker-${index}`).value,folder=document.querySelector(`#library-folder-${index}`).value.trim(),progress=document.querySelector(`#library-progress-${index}`),bar=document.querySelector(`#library-bar-${index}`);if(!worker||!folder)return;activeDeployments.add(index);
 try{
  bar.className='progress-bar';bar.style.width='2%';progress.textContent=`准备下发 ${formatBytes(archive.size)}…`;
  const ext=archive.name.toLowerCase().endsWith('.tar.gz')?'.tar.gz':(archive.name.match(/\.[A-Za-z0-9]+$/)?.[0]||'.zip'),safe=`suite-${Math.abs([...archive.name].reduce((a,c)=>((a<<5)-a+c.charCodeAt(0))|0,0))}${ext}`;
  const url=`${location.origin}/api/cluster/suite-library-download/${safe}/${encodeURIComponent(archive.name)}`;
  const accepted=await api('/api/cluster/suites/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({worker_id:worker,url,filename:archive.name,size_bytes:archive.size})});
  const downloaded=await waitCommand(accepted.command_id,progress,p=>{const total=p.total_bytes||archive.size,ratio=total?Math.min(p.downloaded_bytes/total,1):0;bar.style.width=`${Math.max(2,Math.round(ratio*80))}%`;progress.textContent=`正在下发 ${formatBytes(p.downloaded_bytes)} / ${formatBytes(total)} (${Math.round(ratio*100)}%)`});
  bar.style.width='82%';progress.textContent='下载完成，正在解压…';
  const extracted=await api('/api/cluster/suites/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({worker_id:worker,archive_path:downloaded.archive_path,target_dir_name:folder})});
  await waitCommand(extracted.command_id,progress);bar.style.width='';bar.className='progress-bar done';progress.textContent='部署完成';toast(`${archive.name} 已部署到 ${worker}`)
 }catch(e){bar.style.width='';bar.className='progress-bar failed';progress.textContent=`失败：${e.message}`;toast(e.message)}finally{setTimeout(()=>{activeDeployments.delete(index);renderLibrary()},15000)}
}
function clusterDeviceId(workerId,value){const text=String(value||'');return !text||workerId==='auto'||text.startsWith(`${workerId}:`)?text:`${workerId}:${text}`}
function renderJobForm(){
 const worker=document.querySelector('#job-worker'),suite=document.querySelector('#job-suite'),device=document.querySelector('#job-device');if(!worker||!suite||!device)return;
 const previousWorker=worker.value||((clusterWorkspace.scope_mode==='cluster'&&clusterWorkspace.worker_id)||'auto');
 worker.innerHTML='<option value="auto">自动选择</option>'+commandWorkers().map(w=>`<option value="${esc(w.id)}">${esc(w.name||w.id)}</option>`).join('');
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
 try{renderJobForm();const worker=document.querySelector('#job-worker');if(clusterWorkspace.scope_mode==='cluster'&&clusterWorkspace.worker_id&&Array.from(worker.options).some(o=>o.value===clusterWorkspace.worker_id)){worker.value=clusterWorkspace.worker_id;updateJobOptions()}const suite=document.querySelector('#job-suite'),device=document.querySelector('#job-device');if(clusterWorkspace.suite_key&&Array.from(suite.options).some(o=>o.value===clusterWorkspace.suite_key))suite.value=clusterWorkspace.suite_key;const wanted=clusterDeviceId(worker.value,(clusterWorkspace.device_ids||[])[0]);if(wanted&&Array.from(device.options).some(o=>o.value===wanted))device.value=wanted;if(navigate&&clusterWorkspace.cluster_job_id&&state.jobs.some(j=>j.id===clusterWorkspace.cluster_job_id))await showJob(clusterWorkspace.cluster_job_id,false)}finally{applyingClusterWorkspace=false}
}
async function refresh(){const button=document.querySelector('#refresh');if(button){button.disabled=true;button.textContent='刷新中…'}try{const [w,d,s,j,t,l,status]=await Promise.all([api('/api/cluster/workers'),api('/api/cluster/devices'),api('/api/cluster/suites'),api('/api/cluster/jobs'),api('/api/cluster/worker-tests'),api('/api/cluster/suite-library'),api('/api/cluster/status')]);Object.assign(state,{workers:w.workers,devices:d.devices,suites:s.suites,jobs:j.jobs,tests:t.tests||[],library:l.archives||[],status});render();await applyClusterWorkspace(clusterWorkspace)}catch(e){toast(e.message)}finally{if(button){button.disabled=false;button.textContent='↻ 刷新'}}}
async function createJob(){try{const device=document.querySelector('#job-device').value;const body={worker_id:document.querySelector('#job-worker').value,suite_key:document.querySelector('#job-suite').value,devices:device?[device]:[],device_count:1,owner_id:'web-cluster'};if(!body.worker_id||!body.suite_key)throw new Error('请选择 Worker 和套件');const d=await api('/api/cluster/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast(`任务已创建 ${d.job.id}`);syncClusterWorkspace({cluster_job_id:d.job.id,attempt_id:d.job.current_attempt_id||'',worker_id:d.job.assigned_worker_id||body.worker_id,device_ids:(d.job.leases||[]).map(x=>x.device_id),suite_key:d.job.suite_key||body.suite_key});await refresh();showJob(d.job.id)}catch(e){toast(e.message)}}
async function showJob(id,scroll=true){try{const [j,e,a]=await Promise.all([api(`/api/cluster/jobs/${id}`),api(`/api/cluster/jobs/${id}/events`),api(`/api/cluster/jobs/${id}/artifacts`)]);selectedClusterJob=j.job;document.querySelector('#detail').hidden=false;document.querySelector('#job-detail').textContent=JSON.stringify(j.job,null,2);document.querySelector('#job-logs').textContent=e.events.map(x=>`[${x.source}] ${x.message}`).join('\n')||'暂无日志';document.querySelector('#artifacts').innerHTML=a.artifacts.map(x=>`<a href="/api/cluster/jobs/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(x.id)}/download">${esc(x.filename)} (${x.size_bytes} bytes)</a>`).join(' · ');syncClusterWorkspace({scope_mode:'cluster',worker_id:j.job.assigned_worker_id,device_ids:(j.job.leases||[]).map(x=>x.device_id),suite_key:j.job.suite_key||'',cluster_job_id:j.job.id,attempt_id:j.job.current_attempt_id||'',artifact_id:a.artifacts[0]?.id||''});if(scroll)document.querySelector('#detail').scrollIntoView({behavior:'smooth'})}catch(e){toast(e.message)}}
async function cancelJob(id){try{await api(`/api/cluster/jobs/${id}/cancel`,{method:'POST'});toast('停止命令已下发');await refresh()}catch(e){toast(e.message)}}
async function deleteJob(id){if(!confirm('确定删除该任务历史及事件记录？'))return;try{await api(`/api/cluster/jobs/${id}`,{method:'DELETE'});toast('任务历史已删除');await refresh()}catch(e){toast(e.message)}}
async function deleteWorker(id){if(!confirm(`确定从集群删除 ${id}？该主机的设备和套件记录也会移除。`))return;try{await api(`/api/cluster/workers/${encodeURIComponent(id)}`,{method:'DELETE'});toast(`${id} 已从集群删除`);await refresh()}catch(e){toast(e.message)}}
async function restartWorkerVnc(id){if(!confirm(`重启 ${id} 的桌面 VNC 服务？这会中断当前桌面连接。`))return;try{toast(`正在重启 ${id} 的 VNC…`);const d=await api(`/api/cluster/workers/${encodeURIComponent(id)}/restart-vnc`,{method:'POST'});if(d.success)toast(`${id} 桌面 VNC 已恢复`);else toast(`${id} VNC 重启后 RFB 握手仍失败，请检查远端日志`);await refresh()}catch(e){toast(e.message)}}
function normalizedWorkerId(value){return String(value||'').trim().toLowerCase().replace(/[^a-z0-9._-]+/g,'-').replace(/^-+|-+$/g,'')}
function updateDeployCommand(){const id=normalizedWorkerId(document.querySelector('#new-worker-id').value)||'WORKER_ID',host=document.querySelector('#new-worker-host').value.trim()||'USER@HOST',address=host.includes('@')?host.split('@').slice(1).join('@'):'HOST',controller=document.querySelector('#controller-url').value.trim()||location.origin,root=document.querySelector('#suite-root').value.trim()||'~/GMS-Suite';document.querySelector('#deploy-command').textContent=`rsync -azR worker_agent scripts/install_cluster_worker.sh scripts/run_GSI_Burn.sh scripts/run_GMS_Test_Auto.sh tools/upgrade_tool tools/scrcpy-linux-x86_64-v3.3.4 tools/GMS-Host-Tools ${host}:~/gms-worker-setup/ && ssh ${host} 'cd ~/gms-worker-setup && bash scripts/install_cluster_worker.sh ${id} ${controller} WORKER_TOKEN - ${root} ${address}'`}
async function autoDeployWorker(){const button=document.querySelector('#auto-deploy'),errorBox=document.querySelector('#deploy-error');const body={worker_id:normalizedWorkerId(document.querySelector('#new-worker-id').value),ssh_host:document.querySelector('#new-worker-host').value.trim(),controller_url:document.querySelector('#controller-url').value.trim(),suite_root:document.querySelector('#suite-root').value.trim(),token:document.querySelector('#worker-token').value,password:document.querySelector('#worker-password').value};if(!body.worker_id||!body.ssh_host||!body.token){toast('请填写 Worker 名称、SSH 主机和 Worker Token');return}document.querySelector('#new-worker-id').value=body.worker_id;if(errorBox){errorBox.hidden=true;errorBox.textContent=''}button.disabled=true;button.textContent='安装并等待注册…';try{await api('/api/cluster/workers/deploy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('Worker 已安装并成功注册');document.querySelector('#worker-password').value='';document.querySelector('#onboarding').hidden=true;await refresh()}catch(e){if(errorBox){errorBox.hidden=false;errorBox.textContent=`自动部署失败\n${e.message}\n\n可复制上方命令到终端手动执行。`}toast('自动部署失败，请查看详细信息')}finally{button.disabled=false;button.textContent='自动部署'}}
window.addEventListener('gms:embedded-workspace',event=>applyClusterWorkspace(event.detail?.context||{},event.detail?.type==='workspace-context-navigate').catch(e=>toast(e.message)));
window.showJob=showJob;window.cancelJob=cancelJob;window.deployArchive=deployArchive;window.restartWorkerVnc=restartWorkerVnc;document.querySelector('#refresh').onclick=refresh;document.querySelector('#reload-library').onclick=loadLibrary;document.querySelector('#job-worker').onchange=()=>{updateJobOptions();syncClusterWorkspace()};document.querySelector('#job-suite').onchange=()=>syncClusterWorkspace();document.querySelector('#job-device').onchange=()=>syncClusterWorkspace();document.querySelector('#create-job').onclick=createJob;document.querySelector('#job-open-test').onclick=()=>selectedClusterJob&&window.GmsEmbeddedWorkspace?.navigate('test',{scope_mode:'cluster',worker_id:selectedClusterJob.assigned_worker_id,device_ids:(selectedClusterJob.leases||[]).map(x=>x.device_id),suite_key:selectedClusterJob.suite_key||'',cluster_job_id:selectedClusterJob.id,attempt_id:selectedClusterJob.current_attempt_id||''});document.querySelector('#job-open-report').onclick=()=>selectedClusterJob&&window.GmsEmbeddedWorkspace?.navigate('reports',{scope_mode:'cluster',worker_id:selectedClusterJob.assigned_worker_id,cluster_job_id:selectedClusterJob.id,attempt_id:selectedClusterJob.current_attempt_id||''});document.querySelector('#job-open-ats').onclick=()=>selectedClusterJob&&window.GmsEmbeddedWorkspace?.navigate('automation',{scope_mode:'cluster',worker_id:selectedClusterJob.assigned_worker_id,device_ids:(selectedClusterJob.leases||[]).map(x=>x.device_id),suite_key:selectedClusterJob.suite_key||'',cluster_job_id:selectedClusterJob.id,attempt_id:selectedClusterJob.current_attempt_id||''});document.querySelector('#show-onboarding').onclick=()=>{document.querySelector('#onboarding').hidden=false;updateDeployCommand()};document.querySelector('#close-onboarding').onclick=()=>document.querySelector('#onboarding').hidden=true;['new-worker-id','new-worker-host','controller-url','suite-root'].forEach(id=>document.querySelector(`#${id}`).oninput=updateDeployCommand);document.querySelector('#controller-url').value=location.origin;document.querySelector('#copy-deploy').onclick=()=>navigator.clipboard.writeText(document.querySelector('#deploy-command').textContent).then(()=>toast('命令已复制'));document.querySelector('#auto-deploy').onclick=autoDeployWorker;refresh();setInterval(refresh,15000);
