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
  tabsEl.innerHTML=workers.map(w=>`<button class="dash-trend-btn${w.id===dashGaugesWorker?' active':''}" data-click="selectDashGaugesWorker" data-a0="${esc(w.id)}">${esc(w.name||w.id)}</button>`).join('');
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
