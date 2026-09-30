// Cluster dashboard render chunk; loaded by page.html in dependency order.
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
 // ECharts 资源可能早于首批接口返回并触发 renderDashboard。此时保留
 // page.html 的稳定骨架，不能用空 state 伪装成“加载完成”。
 if(!clusterInitialRefreshSettled)return;
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
