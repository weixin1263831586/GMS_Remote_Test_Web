// Cluster page state chunk; loaded by page.html in dependency order.
// 页面运行配置（由模板以 body data 属性注入；本文件为静态资源，
// 不再经 Jinja 渲染）。
window.GMS_CLUSTER_DEFAULT_MAX_JOBS =
    document.body ? document.body.dataset.defaultMaxJobs || '' : '';

const state={workers:[],devices:[],suites:[],jobs:[],tests:[],library:[],status:{local_worker_id:(window.__GMS_BOOTSTRAP__&&window.__GMS_BOOTSTRAP__.localWorkerId)||'ats-worker-controller'}};
const dashCharts={gauges:null,pie:null,trend:null};
let dashTrendWorker='';
let dashTrendLastFetch=0;
let dashMetricsHistory=[];
let dashRefreshTimer=null;
let dashCountdown=10;
let dashResizeFrame=0;
let dashResizeObserver=null;
let localVpnConnected=null;
let workerVpnCache={};
const activeDeployments=new Set();
const CLUSTER_DASHBOARD_TAB_STORAGE_KEY='gms_cluster_active_tab';
const CLUSTER_DASHBOARD_TABS=new Set(['dashboard','management']);
let clusterWorkspace={scope_mode:'cluster'};
let selectedClusterJob=null;
let selectedClusterReport=null;
let applyingClusterWorkspace=false;
let refreshPromise=null;
let clusterInitialRefreshSettled=false;
let clusterRefreshInterval=null;
let toastTimer=null;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const oneDecimal=value=>(Number(value)||0).toFixed(1);
const relativeTime=value=>{const timestamp=Date.parse(value||'');if(!Number.isFinite(timestamp))return '-';const seconds=Math.max(0,Math.floor((Date.now()-timestamp)/1000));if(seconds<60)return `${seconds}秒前`;if(seconds<3600)return `${Math.floor(seconds/60)}分钟前`;if(seconds<86400)return `${Math.floor(seconds/3600)}小时前`;return `${Math.floor(seconds/86400)}天前`};
async function api(path,options,retried=false){const r=await fetch(path,options),text=await r.text();let d={};try{d=text?JSON.parse(text):{}}catch(_){d={detail:text||r.statusText}}const detail=d.detail;if(r.status===403&&!retried&&detail&&typeof detail==='object'&&detail.elevation_required&&typeof window.parent?.requestElevatedAccess==='function'){const granted=await window.parent.requestElevatedAccess('执行集群敏感操作');if(granted)return api(path,options,true)}if(!r.ok||d.success===false){const message=typeof detail==='object'?(detail.message||JSON.stringify(detail)):(detail||d.error||`HTTP ${r.status}`);const error=new Error(message);if(r.status===403&&detail&&typeof detail==='object'&&detail.elevation_required)error.elevationRequired=true;throw error}return d}
function toast(message){const el=document.querySelector('#toast');el.textContent=message;el.style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.style.display='none',3000);if(message.startsWith('Worker 已安装并成功注册：'))notifyCompletion('测试主机部署完成',message);else if(message.includes(' 已部署到 '))notifyCompletion('测试套件部署完成',message)}
async function confirmAction(title,message){if(typeof window.parent?.showConfirmDialog==='function')return window.parent.showConfirmDialog(title,message);return window.confirm(message)}
function notifyCompletion(title,message){window.parent.postMessage({type:'cluster-notification',title,message,level:'success'},location.origin)}
const statusLabels={online:'在线',offline:'离线',busy:'忙碌',draining:'停止派发',available:'可用',allocated:'已分配',reserved:'已预留',external_busy:'外部占用',unauthorized:'未授权',unknown:'未知',fastboot:'Fastboot',created:'已创建',queued:'排队',leasing:'分配设备',assigned:'已分配',dispatching:'派发中',running:'运行中',stopping:'停止中',collecting:'收集报告',worker_lost:'主机失联',completed:'完成',failed:'失败',cancelled:'已取消'};
function badge(s){return `<span class="status ${esc(s)}" title="${esc(s)}">${esc(statusLabels[s]||s)}</span>`}
function workerActivity(worker){const reported=Math.max(0,Number(worker.running_jobs||0)),external=Math.max(0,Number(worker.external_jobs||0)),running=Math.max(reported,external);return {running,external:Math.min(running,external),managed:Math.max(0,running-external)}}
function workerBadge(worker){const activity=workerActivity(worker),status=worker.status==='busy'&&activity.external>0&&!activity.managed?'external_busy':worker.status;return badge(status)}
function compactDuration(seconds){const value=Math.max(0,Math.floor(Number(seconds)||0)),days=Math.floor(value/86400),hours=Math.floor((value%86400)/3600),minutes=Math.floor((value%3600)/60);if(days)return `${days}天${hours?`${hours}小时`:''}`;if(hours)return `${hours}小时${minutes?`${minutes}分钟`:''}`;if(minutes)return `${minutes}分钟`;return `${value}秒`}
function workerWarning(value){const text=String(value||''),inactive=text.match(/^Tradefed output has been inactive for (\d+) seconds;/);if(inactive)return `Tradefed 已 ${compactDuration(inactive[1])} 未产生输出，当前模块可能耗时较长或已停滞`;if(text==='Tradefed is running but its device could not be identified')return 'Tradefed 正在运行，但无法识别其占用设备';if(text==='An external Tradefed process has no identifiable device; new tests are blocked')return '外部 Tradefed 无法识别占用设备，已阻止派发新测试';return text}
function workerTestsMarkup(worker,tests){const activity=workerActivity(worker),visibleExternal=tests.filter(test=>test.source==='external').length,visibleManaged=tests.length-visibleExternal,hiddenExternal=Math.max(0,activity.external-visibleExternal),hiddenManaged=Math.max(0,activity.managed-visibleManaged),hidden=hiddenExternal+hiddenManaged;let rows=tests.map(test=>`<div><strong>${test.source==='external'?'手工/外部':'平台'} ${esc(test.suite_type||'XTS')}</strong> · PID ${esc(test.pid||'-')} · 设备 ${esc((test.devices||[]).join(', ')||'未识别')} · 运行 ${compactDuration(test.elapsed_seconds)}</div>`).join('');if(hidden){const kind=hiddenExternal&&!hiddenManaged?'外部':hiddenManaged&&!hiddenExternal?'平台':'运行中';rows+=`<div class="muted">检测到 ${hidden} 个${kind}测试，详情暂不可用</div>`}return rows||'<span class="muted">当前无测试</span>'}
function localWorkerId(){return state.status.local_worker_id||window.__GMS_BOOTSTRAP__?.localWorkerId||'ats-worker-controller'}
function terminalJob(status){return ['completed','failed','cancelled'].includes(status)}
