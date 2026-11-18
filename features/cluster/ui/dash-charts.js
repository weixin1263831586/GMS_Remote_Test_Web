// Cluster dashboard charts chunk (ECharts gauges/pie/trend); loaded by page.html in dependency order.
const dashColor={text:'#9299a8',green:'#48c78e',yellow:'#e5b94f',red:'#ef6572',blue:'#4f8cff',purple:'#a78bfa',muted:'#6b7280'};
function scheduleDashboardChartsResize(){
 if(dashResizeFrame)return;
 dashResizeFrame=requestAnimationFrame(()=>{
  dashResizeFrame=0;
  Object.values(dashCharts).forEach(chart=>{if(chart&&!chart.isDisposed?.())chart.resize()});
 });
}

function initDashChartContainers(){
 const resource=document.querySelector('#dash-gauges');
 if(resource&&!resource.dataset.init){
  resource.innerHTML='<div class="dash-panel-title"><svg class="ui-icon" aria-hidden="true"><use href="/static/icons/ui.svg#monitor"></use></svg>主机资源概览<span class="dash-panel-sub">CPU / 内存 / 磁盘实时利用率</span></div><div class="dash-gauges-host-tabs" id="dash-gauges-host-tabs"></div><div id="dash-gauges-chart" class="dash-resource-list"></div><div id="dash-gauges-detail" class="resource-summary"></div>';
  resource.dataset.init='1';
 }
 const devices=document.querySelector('#dash-device-pie');
 if(devices&&!devices.dataset.init){
  devices.innerHTML='<div class="dash-panel-title"><svg class="ui-icon" aria-hidden="true"><use href="/static/icons/ui.svg#device"></use></svg><span id="dash-pie-title">设备状态分布</span><span id="dash-device-legend" class="dash-device-legend"></span></div><div id="dash-pie-hosts" class="device-distribution"></div>';
  devices.dataset.init='1';
 }
 const trend=document.querySelector('#dash-trend');
 if(trend&&!trend.dataset.init){
  trend.innerHTML='<div class="dash-panel-title"><svg class="ui-icon" aria-hidden="true"><use href="/static/icons/ui.svg#trend"></use></svg>资源趋势 · 最近 24 小时<span class="dash-chart-legend"><span class="chart-legend-item"><i class="chart-legend-line" style="--line-color:#5b9af0"></i>CPU %</span><span class="chart-legend-item"><i class="chart-legend-line" style="--line-color:#3acb98"></i>内存 %</span></span></div><div id="dash-trend-tabs" class="dash-trend-tabs"></div><div class="dash-trend-chart-box"><div id="dash-trend-chart" style="width:100%;height:100%"></div></div>';
  trend.dataset.init='1';
 }
}

function getDashChart(key,selector){
 if(dashCharts[key])return dashCharts[key];
 const el=document.querySelector(selector);
 if(!el)return null;
 // The local ECharts bundle is loaded asynchronously. A refresh can finish
 // first, so defer creating the trend chart until its onload re-renders.
 if (!window.echarts || typeof window.echarts.init !== 'function') return null;
 const chart=window.echarts.init(el);
 dashCharts[key]=chart;
 return chart;
}

let dashGaugesWorker='';
function updateDashGauges(){
 const localId=localWorkerId();
 const workers=state.workers.slice().sort((a,b)=>(a.id===localId?-1:b.id===localId?1:String(a.name||a.id).localeCompare(String(b.name||b.id),undefined,{numeric:true})));
 if(dashGaugesWorker&&!workers.some(w=>w.id===dashGaugesWorker))dashGaugesWorker='';
 const selected=dashGaugesWorker?workers.filter(w=>w.id===dashGaugesWorker):workers;
 const tabs=document.querySelector('#dash-gauges-host-tabs');
 if(tabs)tabs.innerHTML=`<button class="dash-trend-btn${!dashGaugesWorker?' active':''}" data-click="selectDashGaugesWorker" data-a0="">全部 Worker (${workers.length})</button>`+workers.map(w=>`<button class="dash-trend-btn${w.id===dashGaugesWorker?' active':''}" data-click="selectDashGaugesWorker" data-a0="${esc(w.id)}">${esc(w.name||w.id)}</button>`).join('');
 const metrics=document.querySelector('#dash-gauges-chart');if(!metrics)return;
 const bar=(value,type)=>{
  const valid=value!==null&&value!==undefined&&value!==''&&Number.isFinite(Number(value));
  if(!valid)return '<span class="dash-empty">未上报</span>';
  const number=Math.max(0,Math.min(100,Number(value))),display=number.toFixed(1).replace(/\.0$/,'');
  const color=number>=85?'#ef6572':number>=70?'#e8b956':type==='cpu'?'#5b9af0':'#3acb98';
  return `<span class="resource-progress"><span class="resource-track"><span class="resource-fill" style="--progress:${number}%;--bar-color:${color}"></span></span><strong>${display}%</strong></span>`;
 };
 const row=worker=>{
  const offline=worker.status==='offline';
  const total=Number(worker.disk_total_gb||0),free=Number(worker.disk_free_gb||0);
  const disk=total>0?Math.max(0,Math.min(100,100*(1-free/total))):null;
  return `<tr><td><span class="resource-host-name" title="${esc(worker.name||worker.id)}">${esc(worker.name||worker.id)}</span><span class="resource-host-sub">${esc(worker.address||worker.hostname||'')} · ${worker.id===localId?'Controller':'Worker'}</span></td><td>${workerBadge(worker)}</td><td>${bar(offline?null:worker.cpu_percent,'cpu')}</td><td>${bar(offline?null:worker.memory_percent,'memory')}</td><td>${bar(offline?null:disk,'disk')}</td></tr>`;
 };
 metrics.innerHTML=selected.length?`<table class="dash-resource-table"><thead><tr><th style="width:24%">Worker / 主机</th><th style="width:12%">状态</th><th>CPU</th><th>内存</th><th>磁盘</th></tr></thead><tbody>${selected.map(row).join('')}</tbody></table>`:'<div class="dash-empty">暂无已注册主机</div>';
 const footer=document.querySelector('#dash-gauges-detail');
 if(footer)footer.textContent=dashGaugesWorker&&selected.length?
 `主机 ${selected[0].id} · 可用内存 ${oneDecimal(selected[0].memory_available_gb)} GB · 剩余磁盘 ${oneDecimal(selected[0].disk_free_gb)} GB · 负载 ${oneDecimal(selected[0].load_1m)}`:
 `共 ${workers.length} 台主机 · 实时数据分别来自各 Worker · 无数据时显示未上报`;
}

const dashStateColors={available:'#48c78e',external_busy:'#e5b94f',allocated:'#a78bfa',fastboot:'#4f8cff',offline:'#ef6572',unknown:'#6b7280'};
const dashStateNames={available:'可用',external_busy:'外部占用',allocated:'已分配',fastboot:'Fastboot',unknown:'未知'};

function updateDashDevicePie(){
 const title=document.querySelector('#dash-pie-title'),body=document.querySelector('#dash-pie-hosts'),legend=document.querySelector('#dash-device-legend');
 if(!body)return;
 const localId=localWorkerId();
 const workers=state.workers.filter(w=>w.status!=='offline').sort((a,b)=>(a.id===localId?-1:b.id===localId?1:String(a.name||a.id).localeCompare(String(b.name||b.id),undefined,{numeric:true})));
 const active=state.devices.filter(d=>d.state!=='offline');
 if(title)title.textContent=`设备状态分布 (${active.length})`;
 const states=Object.keys(dashStateNames);
 const counts=Object.fromEntries(states.map(k=>[k,active.filter(d=>d.state===k).length]));
 if(legend)legend.innerHTML=states.filter(k=>counts[k]>0).map(k=>`<span class="device-legend-item"><i class="legend-dot" style="--dot:${dashStateColors[k]}"></i>${esc(dashStateNames[k])} ${counts[k]}</span>`).join('')||'<span>暂无在线设备</span>';
 body.innerHTML=workers.map(worker=>{
  const devices=active.filter(d=>d.worker_id===worker.id),total=devices.length;
  const present=states.map(key=>({key,count:devices.filter(d=>d.state===key).length})).filter(x=>x.count>0);
  const summary=present.map(({key,count})=>`${dashStateNames[key]} ${count}`).join(' · ')||'暂无设备';
  const bars=total?present.map(({key,count})=>`<span class="dist-segment" style="width:${count*100/total}%;background:${dashStateColors[key]}" title="${esc(dashStateNames[key])}: ${count}"></span>`).join(''):'';
  return `<div class="dist-row"><span class="dist-host" title="${esc(worker.name||worker.id)}">${esc(worker.name||worker.id)}</span><span class="dist-number">${total}</span><span class="dist-track">${bars}</span><span class="dist-summary" title="${esc(summary)}">${esc(summary)}</span></div>`;
 }).join('')||'<div class="dash-empty">暂无在线 Worker</div>';
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
 if(tabsEl)tabsEl.innerHTML=workers.map(w=>`<button class="dash-trend-btn${w===dashTrendWorker?' active':''}" data-click="selectDashTrendWorker" data-a0="${esc(w)}">${esc(w)}</button>`).join('')||'<span class="dash-empty">无数据</span>';
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
