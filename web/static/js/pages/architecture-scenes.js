/**
 * Render the six architecture compositions from architecture-scene-data.js.
 * Canonical protocol diagrams remain under docs/architecture, not as duplicate
 * expandable UI content. This renderer uses static local scene descriptions.
 */
(function () {
  'use strict';
  const data = window.ArchitectureSceneData;
  if (!data) return;
  const iconRoot = '/static/icons/ui.svg#';
  const legendNames = {access:'用户 / Agent',control:'Controller / 调度',worker:'Worker / 执行',resource:'设备 / 资源',channel:'设备 / 数据通道',teal:'ADB / 调试代理',external:'外部系统'};
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
      return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[char];
    });
  }
  function icon(name) {
    return '<svg class="arch-scene-icon" aria-hidden="true" focusable="false"><use href="'
      + iconRoot + esc(name) + '"></use></svg>';
  }
  function chipIcon(label) {
    const value=String(label);
    if(/Redmine|工单|缺陷|任务|调度|计划|队列|租约/.test(value))return 'workflow';
    if(/Gerrit|代码|源码|补丁|Patch|Change|版本关联/.test(value))return 'branch';
    if(/CLI|终端|命令|shell|ADB|Fastboot|Tradefed/.test(value))return 'terminal';
    if(/USB|HID|AOAv2|Accessory|触点|热插拔|重枚举|设备透传/.test(value))return 'plug';
    if(/Android|机型|手机|平板|TV|设备|芯片|DUT/.test(value))return 'device';
    if(/权限|授权|审计|健康|校验|安全|风险/.test(value))return 'shield';
    if(/日志|报告|结果|回执|异常|分析|定位/.test(value))return 'chart';
    if(/固件|镜像|分区|CTS|GTS|VTS|STS|套件/.test(value))return 'package';
    if(/知识|搜索|证据|检索|根因/.test(value))return 'search';
    if(/Web|桌面|工作台/.test(value))return 'monitor';
    if(/服务|配置|工具|资源/.test(value))return 'settings';
    if(/Worker|Source/.test(value))return 'server';
    return 'check';
  }
  function makeNode(node) {
    const id=node[0],title=node[1],sub=node[2],symbol=node[3],tone=node[4],
      x=node[5],y=node[6],w=node[7],h=node[8],items=node[9];
    const chips=items.map(function(item) {
      // Display the concise single-line label, but retain the complete term
      // as the hover tooltip / accessible description when a label is shortened.
      const display=Array.isArray(item)?item[0]:item;
      const full=Array.isArray(item)?item[1]:item;
      return '<span class="arch-scene-chip" title="'+esc(full)+'" aria-label="'+esc(full)+'">'
        +icon(chipIcon(display))+'<span>'+esc(display)+'</span></span>';
    }).join('');
    const centerpiece=(id==='controller'||id==='control')
      ?'<span class="arch-scene-controller-emblem" aria-hidden="true"><i></i><i></i><i></i></span>':'';
    return '<article class="arch-scene-node arch-scene-node--'+esc(tone)+'" data-scene-node="'+esc(id)
      +'" style="left:'+x+'px;top:'+y+'px;width:'+w+'px;height:'+h+'px">'
      +centerpiece
      +'<header><span class="arch-scene-node-icon">'+icon(symbol)+'</span><span class="arch-scene-title"><strong>'
      +esc(title)+'</strong><small>'+esc(sub)+'</small></span></header>'
      +'<div class="arch-scene-chips">'+chips+'</div></article>';
  }
  // Every scene is authored as orthogonal M/H/V routes.  Convert their sharp
  // elbow joins to short quadratic arcs, and keep arrowheads off node borders.
  // This changes presentation only: edge direction/ownership stays in scene data.
  function orthogonalPoints(value) {
    const tokens=String(value).match(/[MHVL]|-?\d+(?:\.\d+)?/g)||[];
    const points=[];let x=0,y=0;
    for(let i=0;i<tokens.length;) {
      const command=tokens[i++];
      if(command==='M'||command==='L') {
        x=Number(tokens[i++]);y=Number(tokens[i++]);
      }else if(command==='H')x=Number(tokens[i++]);
      else if(command==='V')y=Number(tokens[i++]);
      else return [];
      if(!Number.isFinite(x)||!Number.isFinite(y))return [];
      if(!points.length||points[points.length-1][0]!==x||points[points.length-1][1]!==y)
        points.push([x,y]);
    }
    return points;
  }
  function roundedRoute(value) {
    const pts=orthogonalPoints(value);
    if(pts.length<2)return value;
    pts[0]=pts[0].slice();pts[pts.length-1]=pts[pts.length-1].slice();
    // The drawn line stops 5px before the actual node edge; SVG marker tips
    // never sit on top of border strokes or clip into adjacent components.
    for(const [which,next,sign] of [[0,1,1],[pts.length-1,pts.length-2,1]]) {
      const dx=pts[next][0]-pts[which][0],dy=pts[next][1]-pts[which][1];
      const len=Math.hypot(dx,dy),inset=Math.min(5,len/4);
      if(len) {
        pts[which][0]+=dx/len*inset*sign;
        pts[which][1]+=dy/len*inset*sign;
      }
    }
    const fmt=n=>Number(n.toFixed(2));
    let d='M'+fmt(pts[0][0])+' '+fmt(pts[0][1]);
    for(let i=1;i<pts.length-1;i++){
      const a=pts[i-1],b=pts[i],c=pts[i+1];
      const ab=Math.hypot(b[0]-a[0],b[1]-a[1]);
      const bc=Math.hypot(c[0]-b[0],c[1]-b[1]);
      if(!ab||!bc)continue;
      const turn=(b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]);
      if(!turn){d+=' L'+fmt(b[0])+' '+fmt(b[1]);continue;}
      const radius=Math.min(20,ab/3,bc/3);
      const x1=b[0]-(b[0]-a[0])/ab*radius,y1=b[1]-(b[1]-a[1])/ab*radius;
      const x2=b[0]+(c[0]-b[0])/bc*radius,y2=b[1]+(c[1]-b[1])/bc*radius;
      d+=' L'+fmt(x1)+' '+fmt(y1)+' Q'+fmt(b[0])+' '+fmt(b[1])+' '+fmt(x2)+' '+fmt(y2);
    }
    d+=' L'+fmt(pts[pts.length-1][0])+' '+fmt(pts[pts.length-1][1]);
    return d;
  }
  function makeEdges(id,graph) {
    const defs=['data','command','report','both'].map(function(type){
      return '<marker id="arch-scene-'+id+'-'+type+'" markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8" refX="7.2" refY="4" orient="auto-start-reverse">'
        +'<path class="arch-scene-marker--'+type+'" d="M0 0 L8 4 L0 8 Z"/></marker>';
    }).join('');
    const paths=graph.edges.map(function(edge){
      const type=edge[1];
      const arrows=type==='both'?' marker-start="url(#arch-scene-'+id+'-both)"':'';
      const route=esc(roundedRoute(edge[0]));
      return '<path class="arch-scene-path arch-scene-path--'+type+'" d="'+route
        +'"'+arrows+' marker-end="url(#arch-scene-'+id+'-'+type+')">'
        +(edge[2]?'<title>'+esc(edge[2])+'</title>':'')+'</path>';
    }).join('');
    const labelBounds=[];
    const labels=graph.edges.filter(function(edge){return edge[2] && edge.length >= 5;}).map(function(edge){
      const width=Math.min(190,Math.max(46,Array.from(String(edge[2])).reduce(function(px,c){
        return px + (c.codePointAt(0)>127?12:7);
      },14)));
      const x=edge[3]-width/2,y=edge[4]-15;
      // Narrow gaps must not produce labels painted over the actual nodes.
      // Edge semantics remain available as a native SVG <title>.
      const blocked=graph.nodes.some(function(node){
        return x<node[5]+node[7] && x+width>node[5]
          && y<node[6]+node[8] && y+21>node[6];
      });
      const overlap=labelBounds.some(function(box){
        return x<box.right+5 && x+width>box.left-5
          && y<box.bottom+3 && y+21>box.top-3;
      });
      if(blocked || overlap)return '';
      labelBounds.push({left:x,right:x+width,top:y,bottom:y+21});
      return '<rect class="arch-scene-edge-pill arch-scene-edge-pill--'+edge[1]
        +'" x="'+x.toFixed(1)+'" y="'+y+'" width="'+width+'" height="21" rx="10.5"/>'
        +'<text class="arch-scene-edge-label" x="'+edge[3]+'" y="'+edge[4]+'">'+esc(edge[2])+'</text>';
    }).join('');
    return '<svg class="arch-scene-wires" xmlns="http://www.w3.org/2000/svg" width="1120" height="'+graph.h
      +'" viewBox="0 0 1120 '+graph.h+'" aria-hidden="true" focusable="false"><defs>'
      +defs+'</defs>'+paths+labels+'</svg>';
  }
  function makeScene(id,graph) {
    const nodes=graph.nodes.map(makeNode).join('');
    const legend=graph.legend.map(function(pair){
      return '<li><i class="arch-scene-swatch arch-scene-swatch--'+esc(pair[0])+'"></i>'
        +esc(pair[1]||legendNames[pair[0]])+'</li>';
    }).join('');
    const boundary=graph.boundary.map(function(item){return '<li>'+esc(item)+'</li>';}).join('');
    return '<div class="arch-scene-layout" data-scene-view="'+id+'">'
      +'<div class="arch-scene-scroll" role="region" aria-label="'+esc(id)+' 拓扑，可横向滚动查看全部节点" tabindex="0">'
      +'<div class="arch-scene-canvas arch-scene-canvas--'+id+'" style="height:'+graph.h+'px">'
      +makeEdges(id,graph)+nodes+'</div></div>'
      +'<aside class="arch-scene-sidebar" aria-label="图例及关键边界">'
      +'<section class="arch-scene-sidecard"><h3>'+icon('layout')+' 图例说明</h3><ul class="arch-scene-legend">'
      +legend+'</ul><div class="arch-scene-edge-key"><i class="arch-scene-line arch-scene-line--data"></i>设备 / 业务数据</div>'
      +'<div class="arch-scene-edge-key"><i class="arch-scene-line arch-scene-line--report"></i>事件 / ACK / 结果</div>'
      +'<div class="arch-scene-edge-key"><i class="arch-scene-line arch-scene-line--command"></i>受控命令 / 授权</div></section>'
      +'<section class="arch-scene-sidecard"><h3>'+icon('shield')+' 关键边界</h3><ul class="arch-scene-boundaries">'
      +boundary+'</ul></section></aside></div>';
  }
  function makeCards(graph) {
    return graph.cards.map(function(card) {
      const type=card[1];
      const rows=card[2].map(function(cols) {
        return '<tr><th scope="row">'+esc(cols[0])+'</th><td>'+cols.slice(1).map(esc).join(' · ')+'</td></tr>';
      }).join('');
      const symbol=type==='risk'?'warning':type==='worker'?'server':'workflow';
      return '<article class="arch-v2-info-panel arch-v2-mini arch-scene-bottom-card arch-scene-bottom-card--'+type+'">'
        +'<h2>'+icon(symbol)+esc(card[0])+'</h2><div class="arch-scene-tablewrap">'
        +'<table><tbody>'+rows+'</tbody></table></div></article>';
    }).join('');
  }
  function mount() {
    Object.keys(data).forEach(function(id){
      const pane=document.querySelector('[data-arch-pane="'+id+'"]');
      if(!pane || pane.dataset.archSceneReady==='true')return;
      const panel=pane.querySelector('.arch-v2-panel');
      const old=panel && panel.querySelector(id==='platform'?'.arch-v2-platform-layout':'.arch-v2-svg-frame');
      if(!old)return;
      const root=document.createElement('div');
      root.innerHTML=makeScene(id,data[id]);
      const sceneEl=root.firstElementChild;
      if(id==='platform'){
        old.replaceWith(sceneEl);
        pane.querySelector('.arch-v2-bottom--platform')?.classList.add('arch-scene-platform-bottom');
      }else{
        // Canonical Mermaid sources remain documented in docs/architecture/.
        // Do not duplicate the same diagram or its responsibilities in the UI.
        old.replaceWith(sceneEl);
        const bottom=pane.querySelector('.arch-v2-flow-bottom');
        if(bottom) bottom.innerHTML=makeCards(data[id]);

      }
      panel.classList.add('arch-scene-panel');
      pane.dataset.archSceneReady='true';
    });
  }
  // Fit the complete diagram and its summary cards into the visible architecture
  // viewport on desktop. Keep ordinary internal horizontal scrolling when the
  // viewport is too narrow; never clip a node simply to eliminate page scrolling.
  function fitScenes() {
    Object.keys(data).forEach(function(id) {
      const pane=document.querySelector('[data-arch-pane="'+id+'"]');
      if(!pane || pane.hidden || pane.dataset.archSceneReady!=='true')return;
      const canvas=pane.querySelector('.arch-scene-canvas');
      const bottom=pane.querySelector('.arch-v2-bottom');
      if(!canvas)return;
      canvas.style.transform='none';
      canvas.style.zoom='1';
      const top=pane.getBoundingClientRect().top;
      const available=window.innerHeight-top-9;
      const bottomHeight=bottom?bottom.getBoundingClientRect().height:0;
      const reserved=bottomHeight+(bottom?9:0)+19;
      const naturalHeight=Number(data[id].h)||canvas.offsetHeight;
      const scale=Math.max(0.58,Math.min(1,(available-reserved)/naturalHeight));
      // CSS zoom changes layout measurements (unlike transform:scale), so
      // the horizontal scrollbar, side cards and lower tables remain aligned.
      canvas.style.zoom=String(Math.round(scale*1000)/1000);
      // Use some of the spare widescreen width without increasing the
      // vertical footprint.  The cap keeps icons/text from becoming distorted
      // and the container retains its native horizontal scroll on small screens.
      const scroll=pane.querySelector('.arch-scene-scroll');
      const room=scroll ? scroll.clientWidth-16 : 0;
      const stretch=Math.max(1,Math.min(1.17,room/(1120*scale)));
      canvas.style.transformOrigin='center top';
      canvas.style.transform='scaleX('+stretch.toFixed(3)+')';
    });
  }
  function mounted() {
    mount();
    requestAnimationFrame(fitScenes);
    // Tab selection is controlled exclusively by the shared workspace-tabs
    // implementation, not by this viewport-fitting layer.
    document.querySelectorAll('[data-arch-pane]').forEach(function(pane){
      const observer=new MutationObserver(function(){
        if(!pane.hidden)requestAnimationFrame(fitScenes);
      });
      observer.observe(pane,{attributes:true,attributeFilter:['hidden']});
    });
    window.addEventListener('resize',fitScenes);
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mounted,{once:true});
  else mounted();
})();
