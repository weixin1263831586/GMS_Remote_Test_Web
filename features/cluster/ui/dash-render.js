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

 const makeStat=(item)=>`<div class="dash-stat-card"><span class="stat-icon"><svg class="ui-icon" aria-hidden="true"><use href="/static/icons/ui.svg#${item.icon}"></use></svg></span><span class="stat-text"><span class="label">${item.label}</span><span class="num">${item.num}</span><span class="sub">${item.sub}</span></span></div>`;
 statsEl.innerHTML=[
  {icon:'server',num:state.workers.length,label:'主机总数',sub:`${workersOnline} 在线${workersOffline?` · <span class="bad">${workersOffline} 离线</span>`:''}`},
  {icon:'device',num:`${devAvailable} / ${dashboardDevices.length}`,label:'设备可用 / 总量',sub:`<span class="warn">${devBusy} 已占用</span>`},
  {icon:'test',num:allActivity.external+allActivity.managed,label:'测试运行',sub:`${allActivity.external} 外部 · ${allActivity.managed} 平台`},
  {icon:'play',num:jobsActive,label:'任务运行',sub:`${jobsCompleted} 完成${jobsFailed?` · <span class="bad">${jobsFailed} 失败</span>`:''}`},
  {icon:'package',num:suitesAvailable,label:'可用套件',sub:`${new Set(state.suites.filter(s=>s.available).map(s=>s.suite_type)).size} 种类型`},
 ].map(makeStat).join('');
 statsEl.setAttribute('aria-busy','false');
 // 2-4. ECharts: init DOM once, then only update data
 // 实时主机/设备面板不依赖图表库；ECharts 仅负责历史趋势。
 initDashChartContainers();
 updateDashGauges();
 updateDashDevicePie();
 if(hasECharts)updateDashTrend();
 else renderDashboardFallback();
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
  let html=`<div class="dash-panel-title"><svg class="ui-icon" aria-hidden="true"><use href="/static/icons/ui.svg#test"></use></svg>当前测试执行 (${tests.length})</div><div class="dash-tests-list">`;
  if(!tests.length){html+='<div class="dash-empty">当前无运行中的测试</div>'}
  else{
   sortedWorkers.forEach(wid=>{
    const worker=state.workers.find(w=>w.id===wid);
    html+=`<div class="dash-test-group"><div class="dash-test-group-title">${esc(worker?.name||wid)}</div>`;
    byWorker[wid].forEach(t=>{const ext=t.source==='external';html+=`<div class="dash-test-row"><span class="badge-mini ${ext?'ext':'mgd'}">${ext?'外部':'平台'}</span><strong>${esc(t.suite_type||'XTS')} · ${esc((t.devices||[]).join(', ')||'未识别设备')}</strong><span class="dev">${esc(worker?.name||wid)} · ${ext?'外部执行':'平台执行'}</span><span class="dur">${compactDuration(t.elapsed_seconds)}</span></div>`});
    html+='</div>';
   });
  }
  html+='</div>';
  testsEl.innerHTML=html;
 }
 scheduleDashboardChartsResize();
}
function renderDashboardFallback(){
 // 资源表和设备分布始终可用；只有 ECharts 的 24 小时趋势需要降级。
 if(window.dashEchartsStatus!=='failed')return;
 const chart=document.querySelector('#dash-trend-chart');
 if(chart)chart.innerHTML='<div class="dash-empty">历史趋势图暂不可用；实时资源与设备状态仍可正常查看。</div>';
}
