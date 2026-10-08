// Cluster page entry chunk; loaded by page.html in dependency order.
document.addEventListener('click',event=>{
 const button=event.target.closest('button[data-action]');if(!button)return;
 const action=button.dataset.action,workerId=button.dataset.workerId||'',jobId=button.dataset.jobId||'';
 if(action==='toggle-worker-menu'){event.stopPropagation();button.nextElementSibling?.classList.toggle('open')}
 else if(action==='worker-config')openWorkerConfig(workerId);
 else if(action==='restart-vnc')restartWorkerVnc(workerId);
 else if(action==='local-software')reconfigureLocalSoftware(button);
 else if(action==='redeploy-worker')redeployWorker(workerId,button.dataset.sshHost||'');
 else if(action==='delete-worker')deleteWorker(workerId);
 else if(action==='deploy-archive')deployArchive(button.dataset.archiveKey||'');
 else if(action==='show-job')showJob(jobId);
 else if(action==='cancel-job')cancelJob(jobId);
 else if(action==='delete-job')deleteJob(jobId);
 else if(action==='open-job-report-file')openClusterJobReportFile(selectedClusterJob,button.dataset.artifactId||'',button.dataset.filename||'test_result_failures_suite.html');
 else if(action==='download-job-report')downloadClusterJobReport(selectedClusterJob,button.dataset.artifactId||'');
});
document.querySelector('#cluster-search')?.addEventListener('input',render);
document.querySelector('#job-status-filter')?.addEventListener('change',render);
window.addEventListener('gms:embedded-workspace',event=>applyClusterWorkspace(event.detail?.context||{},event.detail?.type==='workspace-context-navigate').catch(e=>toast(e.message)));
window.showJob=showJob;window.cancelJob=cancelJob;window.deployArchive=deployArchive;window.restartWorkerVnc=restartWorkerVnc;window.redeployWorker=redeployWorker;window.openWorkerConfig=openWorkerConfig;window.saveWorkerConfig=saveWorkerConfig;window.deleteWorker=deleteWorker;window.updateDashGauges=updateDashGauges;window.updateDashTrend=updateDashTrend;window.renderDashPieDetail=renderDashPieDetail;window.renderDashboard=renderDashboard;document.querySelector('#refresh').onclick=()=>refresh(true);const dashRefreshBtn=document.querySelector('#dash-refresh-charts');if(dashRefreshBtn)dashRefreshBtn.onclick=()=>{dashTrendLastFetch=0;refresh(true)};document.querySelector('#reload-library').onclick=loadLibrary;document.querySelector('#job-worker').onchange=()=>{clusterJobWorkerDraft=document.querySelector('#job-worker').value;updateJobOptions();syncClusterWorkspace()};document.querySelector('#job-suite').onchange=()=>syncClusterWorkspace();document.querySelector('#job-device').onchange=()=>syncClusterWorkspace();document.querySelector('#create-job').onclick=createJob;document.querySelector('#job-open-test').onclick=()=>selectedClusterJob&&window.GmsEmbeddedWorkspace?.navigate('test',{scope_mode:'cluster',worker_id:selectedClusterJob.assigned_worker_id,device_ids:(selectedClusterJob.leases||[]).map(x=>x.device_id),suite_key:selectedClusterJob.suite_key||'',cluster_job_id:selectedClusterJob.id,attempt_id:selectedClusterJob.current_attempt_id||''});document.querySelector('#job-open-report').onclick=()=>openClusterJobReport();document.querySelector('#job-open-ats').onclick=()=>selectedClusterJob&&window.GmsEmbeddedWorkspace?.navigate('automation',{scope_mode:'cluster',worker_id:selectedClusterJob.assigned_worker_id,device_ids:(selectedClusterJob.leases||[]).map(x=>x.device_id),suite_key:selectedClusterJob.suite_key||'',cluster_job_id:selectedClusterJob.id,attempt_id:selectedClusterJob.current_attempt_id||''});document.querySelector('#show-onboarding').onclick=()=>{const idEl=document.querySelector('#new-worker-id');if(idEl){idEl.readOnly=false;idEl.style.opacity='';idEl.value=''}document.querySelector('#onboarding').hidden=false;updateDeployCommand()};document.querySelector('#close-onboarding').onclick=()=>document.querySelector('#onboarding').hidden=true;['new-worker-id','new-worker-host','controller-url','suite-root'].forEach(id=>document.querySelector(`#${id}`).oninput=updateDeployCommand);document.querySelector('#controller-url').value=location.origin;document.querySelector('#copy-deploy').onclick=()=>navigator.clipboard.writeText(document.querySelector('#deploy-command').textContent).then(()=>toast('命令已复制'));document.querySelector('#auto-deploy').onclick=autoDeployWorker;document.querySelector('#close-config-modal')&&(document.querySelector('#close-config-modal').onclick=()=>{document.querySelector('#worker-config-modal').hidden=true});document.querySelector('#save-worker-config')&&(document.querySelector('#save-worker-config').onclick=saveWorkerConfig);document.addEventListener('click',e=>{if(!e.target.closest('.worker-menu-wrap'))document.querySelectorAll('.worker-menu.open').forEach(m=>m.classList.remove('open'))});
// Tab switching. 与外层 gms_current_page 的刷新恢复保持一致；使用
// sessionStorage 可在当前浏览器标签页刷新后恢复，同时不会让另一个标签页
// 的集群视图互相覆盖。
function selectClusterDashboardTab(tab,{persist=true}={}){
 const target=CLUSTER_DASHBOARD_TABS.has(tab)?tab:'dashboard';
 document.querySelectorAll('.dash-tab').forEach(button=>{
  const active=button.dataset.dashTab===target;
  button.classList.toggle('active',active);
  button.setAttribute('aria-selected',active?'true':'false');
  button.tabIndex=active?0:-1;
 });
 document.getElementById('tab-dashboard').hidden=target!=='dashboard';
 document.getElementById('tab-management').hidden=target!=='management';
 if(persist){
  try{window.sessionStorage.setItem(CLUSTER_DASHBOARD_TAB_STORAGE_KEY,target)}catch(_error){}
  const url=new URL(window.location.href);
  if(target==='dashboard')url.searchParams.delete('tab');else url.searchParams.set('tab',target);
  window.history.replaceState({},'',url.toString());
 }
 if(target==='dashboard'){setTimeout(()=>{scheduleDashboardChartsResize();if(typeof updateDashTrend==='function')updateDashTrend(true)},50)}
 return target;
}
document.querySelectorAll('.dash-tab').forEach(button=>button.addEventListener('click',()=>selectClusterDashboardTab(button.dataset.dashTab)));
// 页签获得焦点后，遵循 WAI-ARIA Tabs 键盘模式：左右循环，Home/End 跳至首尾。
document.querySelector('.dash-tabs')?.addEventListener('keydown',event=>{
 const keys=['ArrowLeft','ArrowRight','Home','End'];
 if(!keys.includes(event.key))return;
 const tabs=Array.from(document.querySelectorAll('.dash-tab')).filter(button=>!button.disabled&&button.offsetParent!==null);
 const index=tabs.indexOf(document.activeElement);
 if(index===-1||!tabs.length)return;
 const nextIndex=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
 event.preventDefault();
 tabs[nextIndex].focus();
 selectClusterDashboardTab(tabs[nextIndex].dataset.dashTab);
});
// 滚轮落在 tab 栏时转发给激活 dash 面板：main 容器 overflow:hidden，
// 页面滚动由面板自身承载。不转发时滚轮冒泡到 main 被吞，“滚不动”，
// 与 Gerrit/Redmine 的滚动语义不一致。
document.querySelector('.dash-tabs')?.addEventListener('wheel',event=>{
 if(Math.abs(event.deltaY)<=Math.abs(event.deltaX))return;
 const pane=document.querySelector('.dash-tab-pane:not([hidden])');
 if(!pane)return;
 const delta=event.deltaMode===1?event.deltaY*40:event.deltaY;
 const layers=[pane,...pane.querySelectorAll(':scope > *'),...pane.querySelectorAll(':scope > * > *'),...pane.querySelectorAll(':scope > * > * > *')];
 const scroller=layers.find(node=>node.scrollHeight>node.clientHeight+1&&['auto','scroll'].includes(getComputedStyle(node).overflowY));
 if(scroller)scroller.scrollTop+=delta;
},{passive:true});
let initialClusterDashboardTab=new URLSearchParams(window.location.search).get('tab')||'';
if(!CLUSTER_DASHBOARD_TABS.has(initialClusterDashboardTab)){
 try{initialClusterDashboardTab=window.sessionStorage.getItem(CLUSTER_DASHBOARD_TAB_STORAGE_KEY)||''}catch(_error){}
}
selectClusterDashboardTab(initialClusterDashboardTab,{persist:false});
window.addEventListener('resize',scheduleDashboardChartsResize,{passive:true});
if(typeof ResizeObserver!=='undefined'){
 dashResizeObserver=new ResizeObserver(scheduleDashboardChartsResize);
 const dashboard=document.querySelector('#dashboard');if(dashboard)dashResizeObserver.observe(dashboard);
}
// 保底通知：page.html 中已在前置 <script> 尽早调用 markReady()，
// 这里重复调用是幂等的（surfaceReadySent 守卫），确保万无一失。
window.GmsEmbeddedWorkspace?.markReady();
function syncClusterAutoRefresh(event){
 const visible=event?.detail?.visible??window.GmsEmbeddedWorkspace?.isVisible?.()??true;
 if(clusterRefreshInterval){clearInterval(clusterRefreshInterval);clusterRefreshInterval=null}
 if(!visible)return;
 refresh();
 clusterRefreshInterval=setInterval(()=>refresh(),10000);
}
window.addEventListener('gms:embedded-visibility',syncClusterAutoRefresh);
syncClusterAutoRefresh();


// act-bridge 委托目标（替代历史 inline handler）。
function selectDashGaugesWorker(id){dashGaugesWorker=id;updateDashGauges();}
function selectDashTrendWorker(id){dashTrendWorker=id;updateDashTrend();}
function generateWorkerToken(){
  const bytes=crypto.getRandomValues(new Uint8Array(32));
  document.querySelector('#worker-token').value=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
}
