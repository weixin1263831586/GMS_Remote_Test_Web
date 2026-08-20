const state={workers:[],devices:[],suites:[],jobs:[],tests:[],library:[],status:{local_worker_id:'ats-worker-controller'}};
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
let clusterWorkspace={scope_mode:'cluster'};
let selectedClusterJob=null;
let selectedClusterReport=null;
let applyingClusterWorkspace=false;
let refreshPromise=null;
let clusterRefreshInterval=null;
let toastTimer=null;
const clusterModalStack=[];
function syncClusterModalState(){
 const visible=Array.from(document.querySelectorAll('.modal-backdrop:not([hidden])'));
 const visibleIds=new Set(visible.map(modal=>modal.id));
 const active=clusterModalStack.filter(id=>visibleIds.has(id));
 visible.forEach(modal=>{if(!active.includes(modal.id))active.push(modal.id)});
 clusterModalStack.length=0;active.forEach(id=>clusterModalStack.push(id));
 const topIndex=clusterModalStack.length-1;
 clusterModalStack.forEach((id,index)=>{
  const modal=document.getElementById(id);if(!modal)return;
  modal.style.zIndex=String(10000+index*20);modal.inert=index!==topIndex;
  modal.setAttribute('role','dialog');modal.setAttribute('aria-hidden',index===topIndex?'false':'true');
  if(index===topIndex)modal.setAttribute('aria-modal','true');else modal.removeAttribute('aria-modal');
 });
 document.querySelectorAll('.modal-backdrop[hidden]').forEach(modal=>{modal.inert=false;modal.setAttribute('aria-hidden','true');modal.removeAttribute('aria-modal');modal.style.removeProperty('z-index')});
 document.body.classList.toggle('modal-open',clusterModalStack.length>0);
}
function closeTopClusterModal(){
 const id=clusterModalStack[clusterModalStack.length-1],modal=id&&document.getElementById(id);
 if(!modal)return;modal.hidden=true;syncClusterModalState();
}
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&clusterModalStack.length){event.preventDefault();event.stopPropagation();closeTopClusterModal()}});
document.addEventListener('click',event=>{const modal=event.target?.classList?.contains('modal-backdrop')?event.target:null;if(modal&&clusterModalStack[clusterModalStack.length-1]===modal.id){modal.hidden=true;syncClusterModalState()}});
new MutationObserver(syncClusterModalState).observe(document.body,{subtree:true,attributes:true,attributeFilter:['hidden']});
syncClusterModalState();
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const oneDecimal=value=>(Number(value)||0).toFixed(1);
const relativeTime=value=>{const timestamp=Date.parse(value||'');if(!Number.isFinite(timestamp))return '-';const seconds=Math.max(0,Math.floor((Date.now()-timestamp)/1000));if(seconds<60)return `${seconds}秒前`;if(seconds<3600)return `${Math.floor(seconds/60)}分钟前`;if(seconds<86400)return `${Math.floor(seconds/3600)}小时前`;return `${Math.floor(seconds/86400)}天前`};
async function api(path,options,retried=false){const r=await fetch(path,options),text=await r.text();let d={};try{d=text?JSON.parse(text):{}}catch(_){d={detail:text||r.statusText}}const detail=d.detail;if(r.status===403&&!retried&&detail&&typeof detail==='object'&&detail.elevation_required&&typeof window.parent?.requestElevatedAccess==='function'){const granted=await window.parent.requestElevatedAccess('执行集群敏感操作');if(granted)return api(path,options,true)}if(!r.ok||d.success===false){const message=typeof detail==='object'?(detail.message||JSON.stringify(detail)):(detail||d.error||`HTTP ${r.status}`);throw new Error(message)}return d}
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
function localWorkerId(){return state.status.local_worker_id||'ats-worker-controller'}
function terminalJob(status){return ['completed','failed','cancelled'].includes(status)}
function renderModeStatus(){
 const modeHint=document.querySelector('#cluster-mode-status');if(!modeHint)return;
 if(state.status.enabled===undefined)return;
 const localId=localWorkerId(),clusterMode=Boolean(state.status.enabled&&clusterWorkspace.scope_mode==='cluster');
 modeHint.textContent=clusterMode
  ? `集群模式 · 本机 ${localId} · 远端派发${state.status.remote_dispatch_enabled?'已启用':'未启用'}`
  : `单机模式 · 本机 ${localId}`;
}
function renderDashboard(){
 const hasECharts=typeof echarts!=='undefined';
 const statsEl=document.querySelector('#dashboard-stats');if(!statsEl)return;
 // 1. Overview stat cards
 const workersOnline=state.workers.filter(w=>w.status!=='offline').length;
 const workersOffline=state.workers.filter(w=>w.status==='offline').length;
 const dashboardDevices=state.devices.filter(d=>d.state!=='offline');
 const devCounts=dashboardDevices.reduce((acc,d)=>{acc[d.state]=(acc[d.state]||0)+1;return acc},{});
 const devAvailable=devCounts.available||0,devBusy=(devCounts.external_busy||0)+(devCounts.allocated||0);
 const allActivity=state.workers.reduce((acc,w)=>{const a=workerActivity(w);acc.external+=a.external;acc.managed+=a.managed;return acc},{external:0,managed:0});
 const jobsActive=state.jobs.filter(j=>!terminalJob(j.status)).length;
 const jobsCompleted=state.jobs.filter(j=>j.status==='completed').length;
 const jobsFailed=state.jobs.filter(j=>j.status==='failed').length;
 const suitesAvailable=state.suites.filter(s=>s.available).length;
 statsEl.innerHTML=[
  {num:workersOnline,label:'主机',sub:workersOffline?`<span class="bad">${workersOffline} 离线</span>`:`${state.workers.length} 总计`},
  {num:`${devAvailable}/${dashboardDevices.length}`,label:'设备可用/总',sub:`<span class="warn">${devBusy} 占用</span>`},
  {num:allActivity.external+allActivity.managed,label:'测试运行',sub:`<span class="warn">${allActivity.external} 外部</span> · ${allActivity.managed} 平台`},
  {num:jobsActive,label:'任务运行',sub:`${jobsCompleted} 完成${jobsFailed?` · <span class="bad">${jobsFailed} 失败</span>`:''}`},
  {num:suitesAvailable,label:'可用套件',sub:`${new Set(state.suites.filter(s=>s.available).map(s=>s.suite_type)).size} 种类型`},
 ].map(c=>`<div class="dash-stat-card"><div class="num-label"><span class="num">${c.num}</span><span class="label">${c.label}</span></div><div class="sub">${c.sub}</div></div>`).join('');
 statsEl.setAttribute('aria-busy','false');
 // 2-4. ECharts: init DOM once, then only update data
 if(hasECharts){
  initDashChartContainers();
  updateDashGauges();
  updateDashDevicePie();
  updateDashTrend();
 }else{
  renderDashboardFallback();
 }
 const updateEl=document.querySelector('#dash-last-update');
 if(updateEl)updateEl.textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
 // 5. Active tests across all workers, grouped by host
 const testsEl=document.querySelector('#dashboard-tests');
 if(testsEl){
  const tests=state.tests.filter(t=>t.status==='running');
  const localId=localWorkerId();
  const byWorker={};
  tests.forEach(t=>{const wid=t.worker_id||'-';(byWorker[wid]=byWorker[wid]||[]).push(t)});
  const sortedWorkers=Object.keys(byWorker).sort((a,b)=>(a===localId?-1:b===localId?1:String(a).localeCompare(String(b),undefined,{numeric:true})));
  let html=`<div class="dash-panel-title">当前测试执行 (${tests.length})</div><div style="flex:1;min-height:0;overflow:auto">`;
  if(!tests.length){html+='<div class="dash-empty">当前无运行中的测试</div>'}
  else{
   sortedWorkers.forEach(wid=>{
    const worker=state.workers.find(w=>w.id===wid);
    html+=`<div class="dash-test-group"><div class="dash-test-group-title">${esc(worker?.name||wid)}</div>`;
    byWorker[wid].forEach(t=>{const ext=t.source==='external';html+=`<div class="dash-test-row"><span class="badge-mini ${ext?'ext':'mgd'}">${ext?'外部':'平台'}</span><strong>${esc(t.suite_type||'XTS')}</strong><span class="dev">${esc((t.devices||[]).join(', ')||'未识别')}</span><span class="dur">${compactDuration(t.elapsed_seconds)}</span></div>`});
    html+='</div>';
   });
  }
  html+='</div>';
  testsEl.innerHTML=html;
 }
 scheduleDashboardChartsResize();
}
function renderDashboardFallback(){
 // echarts 仍在加载时保持骨架占位，避免先闪现降级布局再被图表替换；
 // 仅在 echarts 确认加载失败（本地 vendor 与 CDN 均不可用）后才渲染降级内容。
 if(window.dashEchartsStatus!=='failed')return;
 const workers=state.workers.filter(w=>w.status!=='offline');
 const metric=(label,value)=>{const numeric=Math.max(0,Math.min(100,Number(value)||0));return `<div style="display:grid;grid-template-columns:42px 1fr 42px;gap:6px;align-items:center;margin:7px 0"><span>${label}</span><span style="height:7px;background:#303642;border-radius:5px;overflow:hidden"><span style="display:block;width:${numeric}%;height:100%;background:${numeric>=85?dashColor.red:numeric>=70?dashColor.yellow:dashColor.green}"></span></span><strong>${Math.round(numeric)}%</strong></div>`};
 const gauges=document.querySelector('#dash-gauges');
 if(gauges){gauges.innerHTML='<div class="dash-panel-title">CPU / 内存实时利用率</div>'+(workers.map(w=>`<div style="padding:5px 0;border-bottom:1px solid var(--border)"><strong>${esc(w.name||w.id)}</strong>${metric('CPU',w.cpu_percent)}${metric('内存',w.memory_percent)}</div>`).join('')||'<div class="dash-empty">暂无在线 Worker</div>')}
 const devices=document.querySelector('#dash-device-pie');
 if(devices){const activeDevices=state.devices.filter(d=>d.state!=='offline');devices.innerHTML=`<div class="dash-panel-title">设备状态分布 (${activeDevices.length})</div>`+Object.entries(dashStateNames).map(([key,name])=>`<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)"><span>${esc(name)}</span><strong style="color:${dashStateColors[key]}">${activeDevices.filter(d=>d.state===key).length}</strong></div>`).join('')}
 const trend=document.querySelector('#dash-trend');
 if(trend){trend.innerHTML='<div class="dash-panel-title">资源趋势</div><div class="dash-empty">图表组件未加载；当前资源数据已在上方按 Worker 展示。</div>'}
}
const dashColor={text:'#9299a8',green:'#48c78e',yellow:'#e5b94f',red:'#ef6572',blue:'#4f8cff',purple:'#a78bfa',muted:'#6b7280'};
function scheduleDashboardChartsResize(){
 if(dashResizeFrame)return;
 dashResizeFrame=requestAnimationFrame(()=>{
  dashResizeFrame=0;
  Object.values(dashCharts).forEach(chart=>{if(chart&&!chart.isDisposed?.())chart.resize()});
 });
}
function initDashChartContainers(){
 const gaugesEl=document.querySelector('#dash-gauges');
 if(gaugesEl&&!gaugesEl.dataset.init){
  gaugesEl.innerHTML='<div class="dash-panel-title" style="margin-bottom:2px">CPU / 内存 / 磁盘利用率</div><div class="dash-gauges-host-tabs" id="dash-gauges-host-tabs" style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:0"></div><div id="dash-gauges-chart" style="width:100%;flex:1;min-height:120px"></div><div id="dash-gauges-detail" style="font-size:10px;color:var(--muted);text-align:center;padding-top:2px;border-top:1px solid var(--border);margin-top:0"></div>';
  gaugesEl.dataset.init='1';
 }
 const pieEl=document.querySelector('#dash-device-pie');
 if(pieEl&&!pieEl.dataset.init){
  pieEl.innerHTML='<div class="dash-panel-title" id="dash-pie-title">设备状态分布</div><div id="dash-pie-hosts" style="display:flex;flex-wrap:nowrap;gap:4px;flex:1;min-height:0"></div>';
  pieEl.dataset.init='1';
 }
 const trendEl=document.querySelector('#dash-trend');
 if(trendEl&&!trendEl.dataset.init){
  trendEl.innerHTML='<div class="dash-panel-title">资源趋势 (最近 24 小时)</div><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px"><div class="dash-trend-tabs" id="dash-trend-tabs" style="display:flex;gap:4px;flex-wrap:wrap"></div><span style="font-size:9px;color:'+dashColor.muted+';font-weight:400;white-space:nowrap"><span style="display:inline-block;width:8px;height:3px;border-radius:2px;background:'+dashColor.blue+';vertical-align:middle"></span> CPU% <span style="display:inline-block;width:8px;height:3px;border-radius:2px;background:'+dashColor.green+';vertical-align:middle;margin-left:4px"></span> 内存%</span></div><div id="dash-trend-chart" style="width:100%;flex:1;min-height:120px"></div>';
  trendEl.dataset.init='1';
 }
}
function getDashChart(key,selector){
 if(dashCharts[key])return dashCharts[key];
 const el=document.querySelector(selector);
 if(!el)return null;
 const chart=echarts.init(el);
 dashCharts[key]=chart;
 return chart;
}
let dashGaugesWorker='';
function updateDashGauges(){
 const workers=state.workers.filter(w=>w.status!=='offline');
 const localId=localWorkerId();
 // Sort with local worker first
 workers.sort((a,b)=>(a.id===localId?-1:b.id===localId?1:0));
 const tabsEl=document.querySelector('#dash-gauges-host-tabs');
 if(tabsEl){
  tabsEl.innerHTML=workers.map(w=>`<button class="dash-trend-btn${w.id===dashGaugesWorker?' active':''}" onclick="dashGaugesWorker='${esc(w.id)}';updateDashGauges()">${esc(w.name||w.id)}</button>`).join('');
 }
 if(!dashGaugesWorker&&workers.length)dashGaugesWorker=workers[0].id;
 const worker=workers.find(w=>w.id===dashGaugesWorker)||workers[0];
 if(!worker)return;
 const cpu=Number(worker.cpu_percent||0),mem=Number(worker.memory_percent||0);
 const diskTotal=Number(worker.disk_total_gb||0);
 const diskFree=Number(worker.disk_free_gb||0);
 const diskHasData=diskTotal>0;
 const diskUsed=diskHasData?100*(1-diskFree/diskTotal):0;
 const memTotal=Number(worker.memory_total_gb||0);
 const memAvail=Number(worker.memory_available_gb||0);
 const chart=getDashChart('gauges','#dash-gauges-chart');if(!chart)return;
 chart.setOption({
 series:[
  {name:'CPU',type:'gauge',center:['16%','52%'],radius:'85%',min:0,max:100,startAngle:200,endAngle:-20,splitNumber:4,
   axisLine:{lineStyle:{width:8,color:[[0.7,dashColor.green],[0.85,dashColor.yellow],[1,dashColor.red]]}},
   axisTick:{show:false},splitLine:{length:8,lineStyle:{color:'#444'}},axisLabel:{distance:8,color:dashColor.muted,fontSize:8},
   pointer:{width:3,length:'55%'},itemStyle:{color:cpu>=85?dashColor.red:cpu>=70?dashColor.yellow:dashColor.green},
   detail:{valueAnimation:true,formatter:'{value}%',color:'#e7eaf0',fontSize:12,offsetCenter:[0,'28%']},title:{show:true,offsetCenter:[0,'52%'],color:dashColor.muted,fontSize:10},
   data:[{value:Math.round(cpu*10)/10,name:'CPU'}]},
  {name:'内存',type:'gauge',center:['50%','52%'],radius:'85%',min:0,max:100,startAngle:200,endAngle:-20,splitNumber:4,
   axisLine:{lineStyle:{width:8,color:[[0.7,dashColor.green],[0.85,dashColor.yellow],[1,dashColor.red]]}},
   axisTick:{show:false},splitLine:{length:8,lineStyle:{color:'#444'}},axisLabel:{distance:8,color:dashColor.muted,fontSize:8},
   pointer:{width:3,length:'55%'},itemStyle:{color:mem>=85?dashColor.red:mem>=70?dashColor.yellow:dashColor.green},
   detail:{valueAnimation:true,formatter:'{value}%',color:'#e7eaf0',fontSize:12,offsetCenter:[0,'28%']},title:{show:true,offsetCenter:[0,'52%'],color:dashColor.muted,fontSize:10},
   data:[{value:Math.round(mem*10)/10,name:'内存'}]},
  {name:'磁盘',type:'gauge',center:['84%','52%'],radius:'85%',min:0,max:100,startAngle:200,endAngle:-20,splitNumber:4,
   axisLine:{lineStyle:{width:8,color:[[0.7,dashColor.green],[0.85,dashColor.yellow],[1,dashColor.red]]}},
   axisTick:{show:false},splitLine:{length:8,lineStyle:{color:'#444'}},axisLabel:{distance:8,color:dashColor.muted,fontSize:8},
   pointer:{show:diskHasData,width:3,length:'55%'},itemStyle:{color:diskUsed>=85?dashColor.red:diskUsed>=70?dashColor.yellow:dashColor.green},
   detail:{valueAnimation:true,formatter:diskHasData?'{value}%':'N/A',color:'#e7eaf0',fontSize:diskHasData?12:10,offsetCenter:[0,'28%']},title:{show:true,offsetCenter:[0,'52%'],color:dashColor.muted,fontSize:10},
   data:[{value:diskHasData?Math.round(diskUsed*10)/10:0,name:'磁盘'}]},
 ]
});
 const detailEl=document.querySelector('#dash-gauges-detail');
 if(detailEl){
  const memUsed=memTotal>0?(memTotal-memAvail):0;
  const diskStr=diskTotal>0?`${oneDecimal(diskTotal-diskFree)}/${oneDecimal(diskTotal)}G`:(diskFree>0?`${oneDecimal(diskFree)}G 可用`:'未知');
  detailEl.innerHTML=`CPU ${Math.round(cpu)}% · 内存 ${oneDecimal(memUsed)}/${oneDecimal(memTotal)}G · 磁盘 ${diskStr} · 负载 ${oneDecimal(worker.load_1m)}`;
 }
}
const dashPieNameToState={'可用':'available','外部占用':'external_busy','已分配':'allocated','Fastboot':'fastboot','未知':'unknown'};
const dashStateColors={available:'#48c78e',external_busy:'#e5b94f',allocated:'#a78bfa',fastboot:'#4f8cff',offline:'#ef6572',unknown:'#6b7280'};
const dashStateNames={available:'可用',external_busy:'外部占用',allocated:'已分配',fastboot:'Fastboot',unknown:'未知'};
function updateDashDevicePie(){
 const titleEl=document.querySelector('#dash-pie-title');
 const hostsEl=document.querySelector('#dash-pie-hosts');if(!hostsEl)return;
 const localId=localWorkerId();
 const workers=state.workers.filter(w=>w.status!=='offline').sort((a,b)=>(a.id===localId?-1:b.id===localId?1:0));
 const workerIds=new Set(workers.map(w=>w.id));
 const dashboardDevices=state.devices.filter(d=>d.state!=='offline');
 if(titleEl)titleEl.textContent='设备状态分布 ('+dashboardDevices.length+')';
 // Dispose charts for workers no longer present
 Object.keys(dashCharts).forEach(k=>{if(k.startsWith('pie_')){const wid=k.slice(4);if(!workerIds.has(wid)){dashCharts[k].dispose();delete dashCharts[k]}}});
 // Build/repair DOM structure only if worker set changed
 const currentIds=new Set(Array.from(hostsEl.querySelectorAll('[data-pie-worker]')).map(el=>el.dataset.pieWorker));
 const needRebuild=[...workerIds].sort().join(',')!==([...currentIds].sort().join(','));
 if(needRebuild){
  hostsEl.innerHTML=workers.map(w=>{
   const wdevs=dashboardDevices.filter(d=>d.worker_id===w.id);
   return `<div data-pie-worker="${esc(w.id)}" style="display:flex;flex-direction:column;align-items:center;flex:1 1 0;min-width:0;min-height:0"><div class="dash-pie-host-label" style="font-size:10px;color:var(--blue);font-weight:600;margin-bottom:2px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(w.name||w.id)} (<span class="dash-pie-count">${wdevs.length}</span>)</div><div id="dash-pie-${esc(w.id)}" style="width:100%;flex:1;min-height:120px"></div></div>`;
  }).join('');
  // Dispose all pie charts to force re-init against new DOM
  Object.keys(dashCharts).forEach(k=>{if(k.startsWith('pie_')){dashCharts[k].dispose();delete dashCharts[k]}});
 }
 workers.forEach(w=>{
  const wdevs=dashboardDevices.filter(d=>d.worker_id===w.id);
  const labelEl=hostsEl.querySelector(`[data-pie-worker="${CSS.escape(w.id)}"] .dash-pie-count`);
  if(labelEl)labelEl.textContent=wdevs.length;
  const el=document.querySelector('#dash-pie-'+CSS.escape(w.id));
  if(!el)return;
  const data=Object.entries(dashStateNames).map(([key,name])=>{
   const count=wdevs.filter(d=>d.state===key).length;
   return {name,value:count,itemStyle:{color:dashStateColors[key]},stateKey:key,workerId:w.id};
  }).filter(d=>d.value>0);
  let chart=dashCharts['pie_'+w.id];
  if(!chart){chart=echarts.init(el);dashCharts['pie_'+w.id]=chart;
   chart.setOption({
    tooltip:{trigger:'item',
     formatter:p=>{
      const devs=wdevs.filter(d=>d.state===p.data.stateKey);
      if(!devs.length)return p.name+': 0';
      return `${p.name} (${devs.length})<br/>`+devs.map(d=>'· '+esc(d.serial)).sort().join('<br/>');
     },
     backgroundColor:'rgba(29,33,43,.95)',borderColor:'#303642',textStyle:{color:'#e7eaf0',fontSize:11},
     extraCssText:'max-height:260px;overflow:auto;white-space:nowrap;'
    },
    series:[{type:'pie',radius:['30%','62%'],center:['50%','50%'],
     label:{show:true,color:dashColor.text,fontSize:9,formatter:'{c}'},
     labelLine:{length:4,length2:4},
     itemStyle:{borderColor:'#1d212b',borderWidth:1}}]
   });
  }
  chart.setOption({series:[{data:data.length?data:[{value:1,name:'无设备',itemStyle:{color:'#333'}}]}]});
 });
}
function renderDashPieDetail(){/* details now shown in tooltip */}
async function updateDashTrend(force=false){
 const el=document.querySelector('#dash-trend');if(!el)return;
 const tabsEl=document.querySelector('#dash-trend-tabs');
 const now=Date.now();
 if(force||now-dashTrendLastFetch>60000){
  dashTrendLastFetch=now;
  try{
   const d=await api('/api/cluster/metrics/history');
   dashMetricsHistory=d.metrics||[];
  }catch(e){dashMetricsHistory=[]}
 }
 const localId=localWorkerId();
 const workers=[...new Set((dashMetricsHistory||[]).map(m=>m.worker_id))];
 workers.sort((a,b)=>(a===localId?-1:b===localId?1:String(a).localeCompare(String(b),undefined,{numeric:true})));
 if(!dashTrendWorker&&workers.length)dashTrendWorker=workers[0];
 if(tabsEl)tabsEl.innerHTML=workers.map(w=>`<button class="dash-trend-btn${w===dashTrendWorker?' active':''}" onclick="dashTrendWorker='${esc(w)}';updateDashTrend()">${esc(w)}</button>`).join('')||'<span class="dash-empty">无数据</span>';
 const byWorker={};
 (dashMetricsHistory||[]).forEach(m=>{(byWorker[m.worker_id]=byWorker[m.worker_id]||[]).push(m)});
 const data=byWorker[dashTrendWorker]||[];
 if(!data.length)return;
 // Build x-axis: show hour label only when the hour changes, blank otherwise
 let lastHour=-1;
 const times=data.map(m=>{try{const d=new Date(m.recorded_at);const h=d.getHours();if(h!==lastHour){lastHour=h;if(h%2===0)return(h<10?'0':'')+h+':00'}return ''}catch(e){return''}});
 // Ensure the first label is always shown
 if(times.length)times[0]=data[0].recorded_at?(()=>{try{const d=new Date(data[0].recorded_at);return(d.getHours()<10?'0':'')+d.getHours()+':00'}catch(e){return''}})():'';
 const cpu=data.map(m=>Math.round(Number(m.cpu_percent)*10)/10);
 const mem=data.map(m=>Math.round(Number(m.memory_percent)*10)/10);
 const chart=getDashChart('trend','#dash-trend-chart');if(!chart)return;
 chart.setOption({
  tooltip:{trigger:'axis',formatter:params=>{const m=data[params[0].dataIndex];const ts=m?(()=>{try{return new Date(m.recorded_at).toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}catch(e){return m.recorded_at}})():'';return ts+'<br/>'+params.map(p=>p.marker+' '+p.seriesName+': '+p.value+'%').join('<br/>')}},
  grid:{left:35,right:15,top:8,bottom:25},
  xAxis:{type:'category',data:times,axisLabel:{color:dashColor.muted,fontSize:9,interval:0,formatter:val=>val||' '},axisLine:{lineStyle:{color:'#303642'}},axisTick:{alignWithLabel:false}},
  yAxis:{type:'value',max:100,axisLabel:{color:dashColor.muted,fontSize:9},splitLine:{lineStyle:{color:'rgba(48,54,66,.5)'}}},
  series:[
   {name:'CPU %',type:'line',data:cpu,smooth:true,symbol:'none',lineStyle:{width:2,color:dashColor.blue},areaStyle:{color:{type:'linear',x:0,y:0,x2:0,y2:1,colorStops:[{offset:0,color:'rgba(79,140,255,.25)'},{offset:1,color:'rgba(79,140,255,0)'}]}}},
   {name:'内存 %',type:'line',data:mem,smooth:true,symbol:'none',lineStyle:{width:2,color:dashColor.green},areaStyle:{color:{type:'linear',x:0,y:0,x2:0,y2:1,colorStops:[{offset:0,color:'rgba(72,199,142,.2)'},{offset:1,color:'rgba(72,199,142,0)'}]}}},
  ]
 });
}
function activeDevices(workerId){return state.devices.filter(device=>device.worker_id===workerId&&!['offline','unknown'].includes(device.state))}
function workerAssessment(worker){
 const devices=activeDevices(worker.id);
 const cpu=Number(worker.cpu_percent||0),memory=Number(worker.memory_percent||0),activity=workerActivity(worker);
 let labels=[];
 if(worker.status==='offline'){labels=[{text:'离线',cls:'bad'}]}
 else if(worker.admission_blocked||worker.status==='draining'){labels=[{text:'已阻止派发',cls:'bad'}]}
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
 document.querySelector('#jobs').innerHTML=state.jobs.filter(job=>(!jobFilter||(jobFilter==='active'?!terminalJob(job.status):job.status===jobFilter))&&matches([job.id,job.assigned_worker_id,job.status,(job.leases||[]).map(item=>item.serial).join(' ')].join(' '))).map(job=>{
  let action=` <button data-action="delete-job" data-job-id="${esc(job.id)}">删除</button>`;
  if(job.status==='stopping')action=' <button disabled>停止中…</button>';
  else if(!terminalJob(job.status))action=` <button data-action="cancel-job" data-job-id="${esc(job.id)}">停止</button>`;
  const created=job.created_at?new Date(job.created_at).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'-';
  return `<tr><td>${esc(job.id.slice(0,16))}</td><td>${esc(job.assigned_worker_id)}</td><td>${badge(job.status)}</td><td>${esc((job.leases||[]).map(item=>item.serial).join(', '))}</td><td>${esc(created)}</td><td><button data-action="show-job" data-job-id="${esc(job.id)}">查看</button>${action}</td></tr>`;
 }).join('')||'<tr><td colspan="6" class="empty">暂无集群任务</td></tr>';
 renderJobForm();
 if(!activeDeployments.size)renderLibrary();
}
function formatBytes(v){const n=Number(v)||0;if(n>=1073741824)return `${(n/1073741824).toFixed(2)} GB`;if(n>=1048576)return `${(n/1048576).toFixed(1)} MB`;return `${n} B`}
function archiveFolder(name){return String(name).replace(/\.(tar\.gz|tar\.bz2|zip|tgz|tar)$/i,'').replace(/[^A-Za-z0-9._+-]+/g,'_')}
function renderLibrary(){const body=document.querySelector('#library');if(!body)return;const workers=commandWorkers();body.innerHTML=state.library.map((a,i)=>`<tr><td title="${esc(a.name)}">${esc(a.name)}${a.complete===false?' <span style="color:#c62828" title="压缩包不完整（可能下载中断），无法下发">不完整</span>':''}</td><td>${formatBytes(a.size)}</td><td>${new Date(a.modified*1000).toLocaleString()}</td><td><select id="library-worker-${i}">${workers.map(w=>`<option value="${esc(w.id)}">${esc(w.name||w.id)}</option>`).join('')}</select></td><td><input id="library-folder-${i}" value="${esc(archiveFolder(a.name))}"></td><td><button class="primary" data-action="deploy-archive" data-index="${i}" ${workers.length&&a.complete!==false?'':'disabled title="没有可接收命令的在线 Worker 或压缩包不完整"'}>下发并解压</button> <span class="progress-wrap"><span class="progress-track"><span class="progress-bar" id="library-bar-${i}"></span></span><span class="deploy-progress" id="library-progress-${i}"></span></span></td></tr>`).join('')||'<tr><td colspan="6" class="empty">Controller 套件目录中没有压缩包</td></tr>'}
async function loadLibrary(){const button=document.querySelector('#reload-library'),original=button?.textContent||'↻ 刷新测试套件';if(button){button.disabled=true;button.textContent='刷新中…';button.setAttribute('aria-busy','true')}try{const d=await api('/api/cluster/suite-library');state.library=d.archives||[];renderLibrary();toast('测试套件已更新')}catch(e){toast(e.message)}finally{if(button){button.disabled=false;button.textContent=original;button.removeAttribute('aria-busy')}}}
async function waitCommand(id,progress,onProgress){for(let i=0;i<7200;i++){const d=await api(`/api/cluster/commands/${encodeURIComponent(id)}`),c=d.command;if(c.status==='completed')return c.result||{};if(['failed','cancelled'].includes(c.status))throw new Error(c.error||`${c.command_type}失败`);if(onProgress&&c.result?.downloaded_bytes)onProgress(c.result);else if(i%10===0)progress.textContent=`处理中 ${Math.floor(i/10)}s`;await new Promise(r=>setTimeout(r,1000))}throw new Error('操作超时')}
async function deployArchive(index){
 const archive=state.library[index],worker=document.querySelector(`#library-worker-${index}`).value,folder=document.querySelector(`#library-folder-${index}`).value.trim(),progress=document.querySelector(`#library-progress-${index}`),bar=document.querySelector(`#library-bar-${index}`);if(!worker||!folder)return;activeDeployments.add(index);
 try{
  bar.className='progress-bar';bar.style.width='2%';progress.textContent=`准备下发 ${formatBytes(archive.size)}…`;
  const ext=archive.name.toLowerCase().endsWith('.tar.gz')?'.tar.gz':(archive.name.match(/\.[A-Za-z0-9]+$/)?.[0]||'.zip'),safe=`suite-${Math.abs([...archive.name].reduce((a,c)=>((a<<5)-a+c.charCodeAt(0))|0,0))}${ext}`;
  const url=`${location.origin}/api/cluster/suite-library-download/${safe}/${encodeURIComponent(archive.name)}?worker_id=${encodeURIComponent(worker)}`;
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
async function checkWorkerVpn(){
 const localId=localWorkerId();
 const promises=state.workers.filter(w=>w.status!=='offline').map(async w=>{
  try{const d=await api(`/api/cluster/workers/${encodeURIComponent(w.id)}/vpn-status`);workerVpnCache[w.id]=d.connected}catch(e){workerVpnCache[w.id]=null}
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
   const requests=[['workers','/api/cluster/workers','workers'],['devices','/api/cluster/devices','devices'],['suites','/api/cluster/suites','suites'],['jobs','/api/cluster/jobs','jobs'],['tests','/api/cluster/worker-tests','tests'],['library','/api/cluster/suite-library','archives'],['status','/api/cluster/status',null]];
   const results=await Promise.allSettled(requests.map(([,path])=>api(path))),errors=[];
   results.forEach((result,index)=>{const [stateKey,,payloadKey]=requests[index];if(result.status==='fulfilled')state[stateKey]=payloadKey?(result.value[payloadKey]||[]):result.value;else errors.push(`${stateKey}: ${result.reason.message}`)});
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
async function redeployWorker(id,presetHost){
 // 使用相同 Worker ID 预填重新部署表单。
 const modal=document.querySelector('#onboarding');if(!modal)return;
 const idEl=document.querySelector('#new-worker-id'),hostEl=document.querySelector('#new-worker-host'),ctrlEl=document.querySelector('#controller-url'),rootEl=document.querySelector('#suite-root'),tokenEl=document.querySelector('#worker-token'),pwdEl=document.querySelector('#worker-password'),errEl=document.querySelector('#deploy-error');
 if(idEl){idEl.value=id;idEl.readOnly=true;idEl.style.opacity='0.65'}
 if(hostEl)hostEl.value=presetHost||'';
 if(ctrlEl&&!ctrlEl.value)ctrlEl.value=location.origin;
 if(rootEl&&!rootEl.value)rootEl.value='~/GMS-Suite';
 if(tokenEl)tokenEl.value='';
 if(pwdEl)pwdEl.value='';
 if(errEl){errEl.hidden=true;errEl.textContent=''}
 updateDeployCommand&&updateDeployCommand();
 modal.hidden=false;
 toast(`已为 ${id} 打开重新部署表单，请填入 Worker Token 和 SSH 密码后点“自动部署”`);
}
let configWorkerId=null;
async function openWorkerConfig(id){
 const modal=document.querySelector('#worker-config-modal');if(!modal)return;
 configWorkerId=id;
 document.querySelector('#config-worker-name').textContent=id;
 document.querySelector('#config-error').hidden=true;document.querySelector('#config-error').textContent='';
 const input=document.querySelector('#config-max-jobs');input.value='';input.disabled=true;input.placeholder='加载中…';
 modal.hidden=false;
 try{const d=await api(`/api/cluster/workers/${encodeURIComponent(id)}/config`);const cfg=d.config||{};input.value=cfg.max_jobs??'';input.disabled=false;input.placeholder='{{DEFAULT_MAX_JOBS}}'}
 catch(e){document.querySelector('#config-error').hidden=false;document.querySelector('#config-error').textContent=e.message}
}
async function saveWorkerConfig(){
 if(!configWorkerId)return;const input=document.querySelector('#config-max-jobs');const btn=document.querySelector('#save-worker-config');const errEl=document.querySelector('#config-error');
 const maxJobs=parseInt(input.value,10);if(!maxJobs||maxJobs<1||maxJobs>32){errEl.hidden=false;errEl.textContent='max_jobs 必须是 1-32 的整数';return}
 errEl.hidden=true;errEl.textContent='';btn.disabled=true;btn.textContent='保存中…';
 try{await api(`/api/cluster/workers/${encodeURIComponent(configWorkerId)}/config`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_jobs:maxJobs})});toast(`${configWorkerId} 配置已保存并生效`);document.querySelector('#worker-config-modal').hidden=true;await refresh()}
 catch(e){errEl.hidden=false;errEl.textContent=e.message;toast(e.message)}
 finally{btn.disabled=false;btn.textContent='保存配置'}
}
function normalizedWorkerId(value){return String(value||'').trim().toLowerCase().replace(/[^a-z0-9._-]+/g,'-').replace(/^-+|-+$/g,'')}
function updateDeployCommand(){const id=normalizedWorkerId(document.querySelector('#new-worker-id').value)||'WORKER_ID',host=document.querySelector('#new-worker-host').value.trim()||'USER@HOST',address=host.includes('@')?host.split('@').slice(1).join('@'):'HOST',controller=document.querySelector('#controller-url').value.trim()||location.origin,root=document.querySelector('#suite-root').value.trim()||'~/GMS-Suite';document.querySelector('#deploy-command').textContent=`test -f tools/adbproxy-rs/dist/adbproxy-rs-linux-x86_64-musl.tar.gz || { echo '请先在 Controller 执行 scripts/build_adbproxy_rs.sh'; exit 1; }; test -f tools/gms-worker-native/dist/x86_64/SHA256SUMS || { echo '请先在 Controller 执行 scripts/build_gms_worker_native.sh'; exit 1; }; rsync -azR worker_agent foundation scripts/install_cluster_worker.sh scripts/install_gms_worker_native.sh scripts/install_adbproxy_rs.sh scripts/gms_worker_usbip.sh scripts/run_GSI_Burn.sh scripts/run_GMS_Test_Auto.sh tools/gms-worker-native/dist tools/adbproxy-rs/dist tools/upgrade_tool tools/misc.img tools/scrcpy-linux-x86_64-v3.3.4 tools/GMS-Host-Tools ${host}:~/gms-worker-setup/ && scp "$GMS_GTS_CREDENTIAL_FILE" ${host}:~/gms-worker-gts.json && ssh ${host} 'cd ~/gms-worker-setup && GMS_DEFAULT_MAX_JOBS={{DEFAULT_MAX_JOBS}} bash scripts/install_cluster_worker.sh ${id} ${controller} WORKER_TOKEN - ${root} ${address} ~/gms-worker-gts.json'`}
async function autoDeployWorker(){
 const button=document.querySelector('#auto-deploy'),errorBox=document.querySelector('#deploy-error');
 const body={worker_id:normalizedWorkerId(document.querySelector('#new-worker-id').value),ssh_host:document.querySelector('#new-worker-host').value.trim(),controller_url:document.querySelector('#controller-url').value.trim(),suite_root:document.querySelector('#suite-root').value.trim(),token:document.querySelector('#worker-token').value,password:document.querySelector('#worker-password').value,save_password:Boolean(document.querySelector('#save-worker-password')?.checked)};
 if(!body.worker_id||!body.ssh_host||!body.token){toast('请填写 Worker 名称、SSH 主机和 Worker Token');return}
 document.querySelector('#new-worker-id').value=body.worker_id;if(errorBox){errorBox.hidden=true;errorBox.textContent=''}button.disabled=true;button.textContent='校验 SSH 主机指纹…';
 try{
  const scan=await api('/api/cluster/workers/ssh-host-key/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssh_host:body.ssh_host})});
  const fingerprints=(scan.keys||[]).map(key=>`${key.key_type}  ${key.fingerprint}`).join('\n');
  if(!await confirmAction('确认 SSH 主机指纹',`请通过目标主机控制台或运维目录核对以下 SSH 指纹：\n\n${fingerprints}\n\n确认这些指纹属于 ${scan.host}:${scan.port} 后继续。`))throw new Error('管理员取消了 SSH 主机指纹确认');
  await api('/api/cluster/workers/ssh-host-key/trust',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssh_host:body.ssh_host,keys:scan.keys})});
  button.textContent='安装并等待注册…';
  await api('/api/cluster/workers/deploy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  toast(`Worker 已安装并成功注册：${body.worker_id}（${body.ssh_host}）`);document.querySelector('#worker-password').value='';document.querySelector('#onboarding').hidden=true;await refresh()
 }catch(e){if(errorBox){errorBox.hidden=false;errorBox.textContent=`自动部署失败\n${e.message}\n\n可复制上方命令到终端手动执行。`}toast('自动部署失败，请查看详细信息')}finally{button.disabled=false;button.textContent='自动部署'}
}
document.addEventListener('click',event=>{
 const button=event.target.closest('button[data-action]');if(!button)return;
 const action=button.dataset.action,workerId=button.dataset.workerId||'',jobId=button.dataset.jobId||'';
 if(action==='toggle-worker-menu'){event.stopPropagation();button.nextElementSibling?.classList.toggle('open')}
 else if(action==='worker-config')openWorkerConfig(workerId);
 else if(action==='restart-vnc')restartWorkerVnc(workerId);
 else if(action==='local-software')reconfigureLocalSoftware(button);
 else if(action==='redeploy-worker')redeployWorker(workerId,button.dataset.sshHost||'');
 else if(action==='delete-worker')deleteWorker(workerId);
 else if(action==='deploy-archive')deployArchive(Number(button.dataset.index));
 else if(action==='show-job')showJob(jobId);
 else if(action==='cancel-job')cancelJob(jobId);
 else if(action==='delete-job')deleteJob(jobId);
 else if(action==='open-job-report-file')openClusterJobReportFile(selectedClusterJob,button.dataset.artifactId||'',button.dataset.filename||'test_result_failures_suite.html');
 else if(action==='download-job-report')downloadClusterJobReport(selectedClusterJob,button.dataset.artifactId||'');
});
document.querySelector('#cluster-search')?.addEventListener('input',render);
document.querySelector('#job-status-filter')?.addEventListener('change',render);
window.addEventListener('gms:embedded-workspace',event=>applyClusterWorkspace(event.detail?.context||{},event.detail?.type==='workspace-context-navigate').catch(e=>toast(e.message)));
window.showJob=showJob;window.cancelJob=cancelJob;window.deployArchive=deployArchive;window.restartWorkerVnc=restartWorkerVnc;window.redeployWorker=redeployWorker;window.openWorkerConfig=openWorkerConfig;window.saveWorkerConfig=saveWorkerConfig;window.deleteWorker=deleteWorker;window.updateDashGauges=updateDashGauges;window.updateDashTrend=updateDashTrend;window.renderDashPieDetail=renderDashPieDetail;window.renderDashboard=renderDashboard;document.querySelector('#refresh').onclick=()=>refresh(true);const dashRefreshBtn=document.querySelector('#dash-refresh-charts');if(dashRefreshBtn)dashRefreshBtn.onclick=()=>{dashTrendLastFetch=0;refresh(true)};document.querySelector('#reload-library').onclick=loadLibrary;document.querySelector('#job-worker').onchange=()=>{updateJobOptions();syncClusterWorkspace()};document.querySelector('#job-suite').onchange=()=>syncClusterWorkspace();document.querySelector('#job-device').onchange=()=>syncClusterWorkspace();document.querySelector('#create-job').onclick=createJob;document.querySelector('#job-open-test').onclick=()=>selectedClusterJob&&window.GmsEmbeddedWorkspace?.navigate('test',{scope_mode:'cluster',worker_id:selectedClusterJob.assigned_worker_id,device_ids:(selectedClusterJob.leases||[]).map(x=>x.device_id),suite_key:selectedClusterJob.suite_key||'',cluster_job_id:selectedClusterJob.id,attempt_id:selectedClusterJob.current_attempt_id||''});document.querySelector('#job-open-report').onclick=()=>openClusterJobReport();document.querySelector('#job-open-ats').onclick=()=>selectedClusterJob&&window.GmsEmbeddedWorkspace?.navigate('automation',{scope_mode:'cluster',worker_id:selectedClusterJob.assigned_worker_id,device_ids:(selectedClusterJob.leases||[]).map(x=>x.device_id),suite_key:selectedClusterJob.suite_key||'',cluster_job_id:selectedClusterJob.id,attempt_id:selectedClusterJob.current_attempt_id||''});document.querySelector('#show-onboarding').onclick=()=>{const idEl=document.querySelector('#new-worker-id');if(idEl){idEl.readOnly=false;idEl.style.opacity='';idEl.value=''}document.querySelector('#onboarding').hidden=false;updateDeployCommand()};document.querySelector('#close-onboarding').onclick=()=>document.querySelector('#onboarding').hidden=true;['new-worker-id','new-worker-host','controller-url','suite-root'].forEach(id=>document.querySelector(`#${id}`).oninput=updateDeployCommand);document.querySelector('#controller-url').value=location.origin;document.querySelector('#copy-deploy').onclick=()=>navigator.clipboard.writeText(document.querySelector('#deploy-command').textContent).then(()=>toast('命令已复制'));document.querySelector('#auto-deploy').onclick=autoDeployWorker;document.querySelector('#close-config-modal')&&(document.querySelector('#close-config-modal').onclick=()=>{document.querySelector('#worker-config-modal').hidden=true});document.querySelector('#save-worker-config')&&(document.querySelector('#save-worker-config').onclick=saveWorkerConfig);document.addEventListener('click',e=>{if(!e.target.closest('.worker-menu-wrap'))document.querySelectorAll('.worker-menu.open').forEach(m=>m.classList.remove('open'))});
// Tab switching
document.querySelectorAll('.dash-tab').forEach(btn=>btn.addEventListener('click',()=>{
 document.querySelectorAll('.dash-tab').forEach(b=>b.classList.remove('active'));btn.classList.add('active');
 const tab=btn.dataset.dashTab;
 document.getElementById('tab-dashboard').hidden=tab!=='dashboard';
 document.getElementById('tab-management').hidden=tab!=='management';
 if(tab==='dashboard'){setTimeout(()=>{scheduleDashboardChartsResize();if(typeof updateDashTrend==='function')updateDashTrend(true)},50)}
}));
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
