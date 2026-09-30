        // ==================== 多主机终端工作区 ====================
        const terminalWorkspace = {layout:'single', panes:[], instances:new Map(), mountingPanes:new Map(), paneGenerations:new Map(), maximized:null, generation:0, clusterState:null, pendingAdbTarget:null};

        function refreshTerminalWorkspaceHostSelectors() {
            terminalWorkspace.panes.forEach((pane, index) => {
                const select = document.querySelector(
                    `[data-terminal-pane="${index}"] select[aria-label="主机"]`
                );
                if (!select) return;
                select.innerHTML = hostWorkspaceHostOptions(
                    pane.hostId,
                    terminalWorkspace.panes,
                    index
                );
            });
        }
        function snapshotTerminalClusterState() {
            return {
                layout: terminalWorkspace.layout,
                // ADB Shell targets are one-shot sessions. Persist only host
                // selections so a mode change or reload cannot reopen/claim a
                // previously selected device automatically.
                panes: terminalWorkspace.panes.map(pane => ({hostId: pane.hostId})),
                maximized: terminalWorkspace.maximized,
            };
        }
        function useSingleTerminalWorkspaceState() {
            const preferred=workspaceHostForWorker(workspaceLocalWorkerId())
                ||desktopHosts.find(host=>host.id==='default')||desktopHosts[0];
            terminalWorkspace.layout='single';
            terminalWorkspace.maximized=null;
            terminalWorkspace.panes=[{hostId:preferred?.id||'default'}];
        }
        function normalizeTerminalWorkspace() {
            const count = HOST_WORKSPACE_COUNTS[terminalWorkspace.layout] || 1;
            const fallback = currentHost?.id || desktopHosts[0]?.id || 'default';
            while (terminalWorkspace.panes.length < count) terminalWorkspace.panes.push({hostId:''});
            const assigned = new Set();
            terminalWorkspace.panes = terminalWorkspace.panes.slice(0, count).map(p => {
                let hostId = p?.hostId;
                const adbSerial = String(p?.serialNo || '').trim();
                const adbWorkerId = String(p?.workerId || '').trim();
                if (p?.mode === 'adb' && adbSerial && adbWorkerId) {
                    if (hostId) assigned.add(hostId);
                    return {hostId, mode:'adb', serialNo:adbSerial, workerId:adbWorkerId};
                }
                if (!desktopHosts.some(h => h.id === hostId) || assigned.has(hostId)) {
                    hostId = (!assigned.has(fallback) && desktopHosts.some(h => h.id === fallback && !h.offline)
                        ? fallback
                        : desktopHosts.find(h => !h.offline && !assigned.has(h.id))?.id) || '';
                }
                if (hostId) assigned.add(hostId);
                return {hostId};
            });
        }
        function saveTerminalWorkspace() {
            if(hostWorkspaceClusterEnabled)terminalWorkspace.clusterState=snapshotTerminalClusterState();
            const stateToSave=terminalWorkspace.clusterState||snapshotTerminalClusterState();
            localStorage.setItem('gms_terminal_workspace',JSON.stringify({layout:stateToSave.layout,panes:stateToSave.panes}));
        }
        function initializeTerminalWorkspaceState() {
            if (window.terminalWorkspaceInitialized) return;
            try {
                const saved=JSON.parse(localStorage.getItem('gms_terminal_workspace')||'{}');
                if(HOST_WORKSPACE_COUNTS[saved.layout])terminalWorkspace.layout=saved.layout;
                if(Array.isArray(saved.panes))terminalWorkspace.panes=saved.panes;
            } catch(_) {}
            normalizeTerminalWorkspace();
            terminalWorkspace.clusterState=snapshotTerminalClusterState();
            if(!hostWorkspaceClusterEnabled)useSingleTerminalWorkspaceState();
            window.terminalWorkspaceInitialized=true;
        }
        async function ensureTerminalWorkspaceInitialized() {
            if (!window.desktopHostsInitialized) {
                const hostsReady = initDesktopHosts();
                const context = window.GmsWorkspace?.get?.() || {};
                const canUseLocalBootstrap = context.scope_mode !== 'cluster'
                    && isLocalWorkspaceWorker(context.worker_id || workspaceLocalWorkerId())
                    && Boolean(workspaceHostForWorker(context.worker_id));
                if (!canUseLocalBootstrap) {
                    await hostsReady;
                } else {
                    hostsReady.catch(error =>
                        debugLog('[Terminal] Background host refresh failed:', error));
                }
                window.desktopHostsInitialized = true;
            }
            initializeTerminalWorkspaceState();
            const preferred=workspaceHostForWorker();
            if(preferred&&terminalWorkspace.layout==='single'&&terminalWorkspace.panes.length){
                const currentPane=terminalWorkspace.panes[0],preferredWorkerId=preferred.worker_id||workspaceLocalWorkerId();
                terminalWorkspace.panes[0]=currentPane?.mode==='adb'&&currentPane.workerId===preferredWorkerId
                    ? {...currentPane,hostId:preferred.id}:{hostId:preferred.id};
            }
            renderTerminalWorkspace();
        }
        function disposeTerminalWorkspaceInstance(index) {
            const instance=terminalWorkspace.instances.get(index);if(!instance)return;
            instance.disposed=true;clearTerminalWorkspaceStartupTimer(instance);instance.resizeObserver?.disconnect();
            if(instance.socket){instance.socket.onclose=null;instance.socket.close();}
            if(instance.terminal){const oldTerminal=instance.terminal;setTimeout(()=>{try{oldTerminal.dispose();}catch(_){}},50);}
            terminalWorkspace.instances.delete(index);
        }
        function disposeTerminalWorkspace() {
            terminalWorkspace.generation+=1;
            [...terminalWorkspace.instances.keys()].forEach(disposeTerminalWorkspaceInstance);
            terminalWorkspace.mountingPanes.clear();
            terminalWorkspace.paneGenerations.clear();
        }
        function renderTerminalWorkspace() {
            const grid=document.getElementById('terminal-workspace-grid'); if(!grid)return;
            normalizeTerminalWorkspace(); saveTerminalWorkspace();
            // Keep existing terminal sessions alive when the layout hasn't
            // changed. Matches renderHostWorkspace: switching away no longer
            // disposes instances, so revisiting should reuse them.
            const signature=terminalWorkspaceRenderSignature();
            if(terminalWorkspace.renderedSignature===signature && grid.children.length) {
                // A hidden-page mount stops after xterm finishes loading. On
                // return, resume panes that never produced a live instance;
                // refreshTerminalWorkspacePane invalidates any older pending
                // mount so duplicate WebSockets cannot be created.
                if(currentPage==='terminal') terminalWorkspace.panes.forEach((pane,index)=>{
                    const status=document.getElementById(`terminal-workspace-status-${index}`)?.textContent;
                    if(!terminalWorkspace.instances.has(index)&&!terminalWorkspace.mountingPanes.has(index)&&(status==='准备中'||status==='正在加载终端…')) {
                        refreshTerminalWorkspacePane(index);
                    }
                });
                setHostWorkspaceSurfaceReady('terminal',true);
                return;
            }
            // Workspace-context/host-directory updates may legitimately alter
            // only the host metadata while an ADB session is opening. A full
            // render would dispose the live WebSocket before terminal_connect
            // is sent. Keep the mounted one-shot ADB target alive when its
            // device identity is unchanged; a real device/layout change still
            // takes the normal teardown path below.
            const activeAdbInstance = terminalWorkspace.instances.get(0);
            const activeAdbPane = terminalWorkspace.panes[0];
            const pendingAdb = terminalWorkspace.pendingAdbTarget;
            if (grid.children.length && terminalWorkspace.layout === 'single'
                    && pendingAdb?.serialNo === activeAdbPane?.serialNo
                    && pendingAdb?.workerId === activeAdbPane?.workerId
                    && (!activeAdbInstance || (
                        activeAdbInstance.mode === 'adb'
                        && activeAdbInstance.serialNo === pendingAdb.serialNo
                        && activeAdbInstance.workerId === pendingAdb.workerId
                    ) || terminalWorkspace.mountingPanes.has(0))
                    && activeAdbPane?.mode === 'adb') {
                // xterm loading happens before the instance is registered.
                // Keep the already-mounted ADB DOM/socket lifecycle intact
                // during that gap as well.
                terminalWorkspace.renderedSignature=signature;
                refreshTerminalWorkspaceHostSelectors();
                setHostWorkspaceSurfaceReady('terminal',true);
                return;
            }
            if (grid.children.length && terminalWorkspace.layout === 'single'
                    && activeAdbInstance?.mode === 'adb'
                    && !activeAdbInstance.disposed
                    && activeAdbPane?.mode === 'adb'
                    && activeAdbPane.serialNo === activeAdbInstance.serialNo
                    && activeAdbPane.workerId === activeAdbInstance.workerId) {
                terminalWorkspace.renderedSignature=signature;
                refreshTerminalWorkspaceHostSelectors();
                setHostWorkspaceSurfaceReady('terminal',true);
                return;
            }
            terminalWorkspace.renderedSignature=signature;
            disposeTerminalWorkspace();
            grid.className=`host-workspace-grid layout-${terminalWorkspace.layout}${terminalWorkspace.maximized!==null?' pane-maximized':''}`;
            document.querySelectorAll('[data-terminal-layout]').forEach(b=>b.classList.toggle('active',b.dataset.terminalLayout===terminalWorkspace.layout));
            grid.innerHTML=terminalWorkspace.panes.map((p,i)=>`<section class="host-workspace-pane${terminalWorkspace.maximized===i?' maximized':''}" data-terminal-pane="${i}">
                <div class="host-workspace-pane-header"><span data-multi-host-control data-terminal-pane-mode-label style="font-size:11px;">🐧 终端</span>
                <select data-multi-host-control aria-label="主机" data-change="changeTerminalWorkspaceHost" data-a0="${i}" data-r1="value">${hostWorkspaceHostOptions(p.hostId,terminalWorkspace.panes,i)}</select>
                <span class="host-workspace-pane-status" id="terminal-workspace-status-${i}">准备中</span>
                <button data-multi-host-control data-terminal-pane-host-mode class="btn-xs" data-click="restoreHostTerminalWorkspacePane" data-a0="${i}" title="关闭当前 ADB Shell，返回这台主机的 SSH 终端" style="${p.mode==='adb'?'':'display:none'}">↩ 主机终端</button>
                <button class="btn-xs host-workspace-refresh-btn" data-click="refreshTerminalWorkspacePane" data-a0="${i}" title="重新连接" aria-label="刷新主机终端">🔄</button>
                <button data-multi-host-control class="btn-xs" data-click="maximizeTerminalWorkspacePane" data-a0="${i}">${terminalWorkspace.maximized===i?'▦':'⛶'}</button></div>
                <div class="host-workspace-pane-body" id="terminal-workspace-body-${i}"><div class="host-workspace-empty">${p.mode==='adb'?'正在打开 ADB Shell…':'正在加载终端…'}</div></div></section>`).join('');
            const singleHostModeButton=document.getElementById('terminal-workspace-host-mode-btn');
            if(singleHostModeButton)singleHostModeButton.style.display=terminalWorkspace.panes[0]?.mode==='adb'?'':'none';
            const generation=terminalWorkspace.generation;
            const autoMountIndex=terminalWorkspace.maximized??0;
            terminalWorkspace.panes.forEach((p,i)=>{
                const host=desktopHosts.find(item=>item.id===p.hostId);
                if(i!==autoMountIndex&&host&&!host.offline){
                    const body=document.getElementById(`terminal-workspace-body-${i}`);
                    if(body)body.innerHTML=`<div class="host-workspace-empty"><button class="btn-xs" data-click="refreshTerminalWorkspacePane" data-a0="${i}">连接此终端</button></div>`;
                    terminalWorkspaceStatus(i,'按需连接');
                    return;
                }
                mountTerminalWorkspacePane(i,p,generation);
            });
            setHostWorkspaceSurfaceReady('terminal',true);
        }
        function terminalWorkspaceStatus(i,text,ok=false){
            const color=ok?'var(--success-color)':'var(--text-secondary)';
            const e=document.getElementById(`terminal-workspace-status-${i}`);
            if(e){e.textContent=text;e.style.color=color;}
            const singleStatus=document.getElementById('terminal-workspace-status-single');
            if(singleStatus&&i===0){singleStatus.textContent=text;singleStatus.style.color=color;}
        }
        async function mountTerminalWorkspacePane(index,pane,generation,paneGeneration=terminalWorkspace.paneGenerations.get(index)||0) {
            const body=document.getElementById(`terminal-workspace-body-${index}`),host=desktopHosts.find(h=>h.id===pane.hostId); if(!body)return;
            if(!host){body.innerHTML='<div class="host-workspace-empty">暂无更多可用主机</div>';terminalWorkspaceStatus(index,'空闲');return;}
            if(host.offline){body.innerHTML='<div class="host-workspace-empty">主机已离线</div>';terminalWorkspaceStatus(index,'离线');return;}
            const terminalMode=pane.mode==='adb'?'adb':'ssh',serialNo=String(pane.serialNo||'').trim();
            if(terminalMode==='adb'&&!serialNo){body.innerHTML='<div class="host-workspace-empty">设备序列号无效</div>';terminalWorkspaceStatus(index,'连接失败');return;}
            const mountToken={generation,paneGeneration};
            const pendingMount=terminalWorkspace.mountingPanes.get(index);
            if(pendingMount&&pendingMount.generation===generation&&pendingMount.paneGeneration===paneGeneration)return;
            terminalWorkspace.mountingPanes.set(index,mountToken);
            terminalWorkspaceStatus(index,terminalMode==='adb'?'正在打开 ADB Shell…':'正在加载终端…');
            try{await loadXTermScripts();}catch(error){if(terminalWorkspace.mountingPanes.get(index)===mountToken)terminalWorkspace.mountingPanes.delete(index);console.error('[Terminal workspace] xterm.js load failed:',error);if(generation!==terminalWorkspace.generation||paneGeneration!==(terminalWorkspace.paneGenerations.get(index)||0)||currentPage!=='terminal'||!body.isConnected)return;body.innerHTML='<div class="host-workspace-empty">xterm.js 加载失败</div>';terminalWorkspaceStatus(index,'加载失败');return;}
            if(terminalWorkspace.mountingPanes.get(index)===mountToken)terminalWorkspace.mountingPanes.delete(index);
            if(generation!==terminalWorkspace.generation||paneGeneration!==(terminalWorkspace.paneGenerations.get(index)||0)||currentPage!=='terminal'||!body.isConnected)return;
            // terminal_ 前缀 WS 在认证部署下要求已提权 session（服务端握手
            // 直接 403）。switchPage 的门卫覆盖不了恢复/复用路径（提权过期
            // 后切回终端页），这里在建立 WS 前兜底确认，拒绝时给出可见
            // 状态而不是哑失败。
            if(!await ensureTerminalElevation(false,'打开主机终端','主机终端')){
                if(generation===terminalWorkspace.generation&&paneGeneration===(terminalWorkspace.paneGenerations.get(index)||0)&&currentPage==='terminal'&&body.isConnected){
                    body.innerHTML='<div class="host-workspace-empty">需要管理员认证后才能打开主机终端</div>';
                    terminalWorkspaceStatus(index,'等待管理员认证');
                }
                return;
            }
            const el=document.createElement('div');el.className='host-workspace-terminal';body.replaceChildren(el);
            const term=new Terminal({cursorBlink:true,fontSize:13,fontFamily:'Consolas, "Courier New", monospace',theme:createTerminalTheme(),scrollback:2000,termName:'xterm-256color'});
            const fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open(el);fit.fit();
            const workerId=pane.workerId||host.worker_id||(host.id==='default'?workspaceLocalWorkerId():'');
            const instance={type:'terminal',mode:terminalMode,paneIndex:index,hostId:host.id,workerId,serialNo,terminal:term,fit,socket:null,resizeObserver:null,disposed:false,initialData:'',shellReady:false,startupFailed:false,startupFailureStatus:'',startupTimer:null,lastResizeCols:0,lastResizeRows:0};terminalWorkspace.instances.set(index,instance);
            const parts=String(host.connection||'').split('@'),user=parts.shift()||'',address=parts.join('@');
            body.addEventListener('dragover',event=>{event.preventDefault();body.style.outline='2px dashed var(--primary-color)';});
            body.addEventListener('dragleave',()=>{body.style.outline='';});
            body.addEventListener('drop',async event=>{event.preventDefault();body.style.outline='';const file=event.dataTransfer?.files?.[0];if(!file)return;if(!await ensureTerminalElevation(false,'上传文件到主机','主机文件上传'))return;term.writeln(`\r\n\x1b[33m📤 正在上传 ${file.name}...\x1b[0m`);const form=new FormData();form.append('file',file);form.append('worker_id',workerId);try{const result=await apiCall('/api/terminal/push','POST',form);term.writeln(`\r\n\x1b[32m✅ 已上传到 ${result.remote_path}\x1b[0m`);}catch(error){term.writeln(`\r\n\x1b[31m❌ 上传失败: ${error.message}\x1b[0m`);}finally{if(socket.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:'terminal_input',input:'\r'}));term.focus();}});
            const protocol=location.protocol==='https:'?'wss:':'ws:',socket=new WebSocket(`${protocol}//${location.host}/api/system/websocket/terminal_workspace_${index}_${Date.now()}_${Math.random().toString(36).slice(2)}`);instance.socket=socket;
            // ADB 启动时宿主 Shell 会依次回显 clear 和 adb shell 命令。
            // 状态栏已经提供连接反馈，终端画布等设备提示符就绪后再一次性显示，
            // 避免“加载文字 -> 清屏 -> 宿主输出 -> 再清屏”的闪烁。
            if(terminalMode!=='adb')term.writeln(`\x1b[33m⏳ 正在连接 ${user}@${address}...\x1b[0m`);
            term.onData(input=>{if(!instance.startupFailed&&socket.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:'terminal_input',input}));});
            let suppressPasteUntil=0;
            const sendInput=input=>{if(instance.startupFailed||socket.readyState!==WebSocket.OPEN)return false;socket.send(JSON.stringify({type:'terminal_input',input}));return true;};
            const pasteText=text=>sendInput(String(text||'').replace(/\r\n/g,'\n').replace(/\r/g,'\n'));
            const pasteClipboard=async()=>{try{if(navigator.clipboard?.readText)pasteText(await navigator.clipboard.readText());}catch(error){console.debug('[Terminal workspace] clipboard read failed',error);}finally{term.focus();}};
            term.attachCustomKeyEventHandler(event=>{if(event.type!=='keydown')return true;const key=event.key.length===1?event.key.toLowerCase():event.key,isCtrl=event.ctrlKey&&!event.altKey&&!event.metaKey;if(isCtrl&&key==='c'){if(term.hasSelection()){const text=term.getSelection();navigator.clipboard?.writeText(text);term.clearSelection();term.focus();}else sendInput('\x03');return false;}if(isCtrl&&key==='v'){suppressPasteUntil=Date.now()+500;pasteClipboard();return false;}if(isCtrl&&/^[a-z]$/.test(key)){sendInput(String.fromCharCode(key.charCodeAt(0)-96));return false;}return true;});
            el.addEventListener('click',()=>term.focus());
            el.addEventListener('paste',event=>{if(Date.now()<suppressPasteUntil){event.preventDefault();event.stopPropagation();return;}const text=event.clipboardData?.getData('text/plain');if(text){event.preventDefault();event.stopPropagation();pasteText(text);term.focus();}},true);
            el.addEventListener('contextmenu',event=>{if(navigator.clipboard?.readText){event.preventDefault();event.stopPropagation();pasteClipboard();}});
            socket.onopen=()=>{if(!instance.disposed)socket.send(JSON.stringify({type:'terminal_connect',mode:terminalMode,worker_id:workerId,...(terminalMode==='adb'?{serial_no:serialNo}:{})}));};
            socket.onmessage=e=>{if(instance.disposed)return;try{const m=JSON.parse(e.data);if(m.type==='terminal_data')writeTerminalWorkspaceData(instance,m.data||'');else if(m.type==='terminal_connected'){if(terminalMode==='adb'){terminalWorkspaceStatus(index,'正在进入 ADB Shell…');clearTerminalWorkspaceStartupTimer(instance);if(!instance.shellReady)instance.startupTimer=setTimeout(()=>failTerminalWorkspaceStartup(instance,'ADB Shell 启动超时'),15000);}else{terminalWorkspaceStatus(index,`${user}@${address}`,true);}setTimeout(()=>resizeHostWorkspaceTerminal(instance),0);}else if(m.type==='terminal_error'||m.type==='error'){clearTerminalWorkspaceStartupTimer(instance);const error=m.error||m.message||'连接失败';if(terminalMode==='adb'&&!m.elevation_required&&!m.credential_required){failTerminalWorkspaceStartup(instance,'ADB Shell 连接失败',error);return;}term.writeln(`\r\n\x1b[31m${error}\x1b[0m`);if(m.elevation_required){terminalWorkspaceStatus(index,'等待管理员认证');recoverTerminalElevation(instance,'重新连接主机终端',()=>refreshTerminalWorkspacePane(index));}else if(m.credential_required&&m.device_host&&typeof showDevicePasswordModal==='function'){terminalWorkspaceStatus(index,'等待 SSH 凭据');showDevicePasswordModal(m.device_host,'terminal',()=>refreshTerminalWorkspacePane(index));}else{terminalWorkspaceStatus(index,'连接失败');}}}catch(_){}};
            socket.onclose=()=>{if(!instance.disposed){clearTerminalWorkspaceStartupTimer(instance);if(instance.startupFailed){terminalWorkspaceStatus(index,instance.startupFailureStatus||'ADB Shell 启动失败');return;}term.writeln('\r\n\x1b[31m⚠️ 连接已断开\x1b[0m');terminalWorkspaceStatus(index,'已断开');}};
            socket.onerror=()=>{if(!instance.disposed)terminalWorkspaceStatus(index,instance.startupFailed?(instance.startupFailureStatus||'ADB Shell 启动失败'):'连接错误');};instance.resizeObserver=new ResizeObserver(()=>resizeHostWorkspaceTerminal(instance));instance.resizeObserver.observe(body);
        }
        function clearTerminalWorkspaceStartupTimer(instance){
            if(instance?.startupTimer){clearTimeout(instance.startupTimer);instance.startupTimer=null;}
        }
        function terminalWorkspaceAdbStartupDetail(instance){
            const plain=String(instance?.initialData||'')
                .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g,'')
                .replace(/\x1b\[[0-?]*[ -\/]*[@-~]/g,'');
            const adbCommand=/\badb\s+-s\s+(?:"[^"]*"|'[^']*'|[^\s]+)\s+shell(?:\s|$)/.exec(plain);
            if(!adbCommand)return '';
            const lines=plain.slice(adbCommand.index+adbCommand[0].length).split(/\r\n|\n|\r/);
            const hostPrompt=lines.findIndex(line=>/^[^\s@]+@[^\s:]+:[^\r\n]*[$#]\s*$/.test(line));
            return lines.slice(0,hostPrompt<0?lines.length:hostPrompt)
                .map(line=>line.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g,'').trim())
                .filter(Boolean).join('\r\n').slice(-4096);
        }
        function terminalWorkspaceFailureStatus(reason,detail){
            const summary=String(detail||'').replace(/[\x00-\x1f\x7f]/g,' ').replace(/\s+/g,' ').trim();
            return summary?`${reason}：${summary.slice(0,180)}`:reason;
        }
        function failTerminalWorkspaceStartup(instance,reason,detail=''){
            if(!instance||instance.disposed||instance.shellReady||instance.startupFailed)return;
            clearTerminalWorkspaceStartupTimer(instance);
            instance.startupFailed=true;
            const failureDetail=detail||terminalWorkspaceAdbStartupDetail(instance);
            instance.startupFailureStatus=terminalWorkspaceFailureStatus(reason,failureDetail);
            instance.initialData='';
            instance.terminal.write(`\x1b[31m❌ ${reason}\x1b[0m${failureDetail?`\r\n\r\n${failureDetail}`:''}`);
            terminalWorkspaceStatus(instance.paneIndex,instance.startupFailureStatus);
            if(instance.socket?.readyState===WebSocket.OPEN)instance.socket.close();
        }
        function writeTerminalWorkspaceData(instance,data){
            if(instance.shellReady){instance.terminal.write(data);return;}
            if(instance.startupFailed)return;
            instance.initialData=(instance.initialData+String(data||'')).slice(-32768);
            const plain=instance.initialData
                .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g,'')
                .replace(/\x1b\[[0-?]*[ -\/]*[@-~]/g,'');
            const lines=plain.split(/\r\n|\n|\r/);
            if(instance.mode==='adb'){
                // The backend enters ADB through an interactive host shell.
                // Do not paint its welcome text, clear sequence, command echo,
                // and intermediate prompts. Reveal only once Android is ready.
                const adbCommand=/\badb\s+-s\s+(?:"[^"]*"|'[^']*'|[^\s]+)\s+shell(?:\s|$)/.exec(plain);
                if(!adbCommand)return;
                const adbLines=plain.slice(adbCommand.index+adbCommand[0].length).split(/\r\n|\n|\r/);
                const devicePromptIndex=adbLines.findIndex(line=>/^(?:[\w.-]+@)?[^\s:]+:\/[^\r\n]*[$#]\s*$/.test(line));
                const hostPromptIndex=adbLines.findIndex(line=>/^[^\s@]+@[^\s:]+:[^\r\n]*[$#]\s*$/.test(line));
                if(hostPromptIndex>=0&&(devicePromptIndex<0||hostPromptIndex<devicePromptIndex)){
                    failTerminalWorkspaceStartup(instance,'ADB Shell 启动失败',terminalWorkspaceAdbStartupDetail(instance));
                    return;
                }
                if(devicePromptIndex<0)return;
                const prompt=adbLines[devicePromptIndex];
                clearTerminalWorkspaceStartupTimer(instance);
                instance.shellReady=true;
                instance.initialData='';
                instance.terminal.write(`\x1b[32m✅ ADB Shell · ${instance.serialNo}\x1b[0m\r\n\r\n${prompt}`);
                terminalWorkspaceStatus(instance.paneIndex,`ADB · ${instance.serialNo}`,true);
                return;
            }
            const prompt=[...lines].reverse().find(line=>/@[^\s:]+:[^\r\n]*[$#]\s*$/.test(line));
            if(prompt){instance.shellReady=true;instance.initialData='';instance.terminal.write(prompt);}
        }
        function terminalWorkspaceRenderSignature(){return JSON.stringify({layout:terminalWorkspace.layout,panes:terminalWorkspace.panes,maximized:terminalWorkspace.maximized});}
        function setTerminalWorkspaceLayout(v){if(HOST_WORKSPACE_COUNTS[v]&&(hostWorkspaceClusterEnabled||v==='single')){terminalWorkspace.layout=v;terminalWorkspace.maximized=null;renderTerminalWorkspace();}}
        function changeTerminalWorkspaceHost(i,v){
            const pane=terminalWorkspace.panes[i],host=desktopHosts.find(item=>item.id===v),select=document.querySelector(`[data-terminal-pane="${i}"] select[aria-label="主机"]`);
            if(!pane||!host||host.offline){if(select&&pane)select.value=pane.hostId;return;}
            if(terminalWorkspace.panes.some((item,paneIndex)=>paneIndex!==i&&item.hostId===v)){
                if(select)select.value=pane.hostId;
                showToast('该主机已在其他终端窗格中连接','warning');
                return;
            }
            if(pane.hostId===v)return;
            terminalWorkspace.panes[i]={hostId:v};
            if(terminalWorkspace.layout==='single'){
                currentHost=host;
                updateWorkspaceForHost(host,'terminal');
            }
            saveTerminalWorkspace();
            terminalWorkspace.renderedSignature=terminalWorkspaceRenderSignature();
            terminalWorkspace.panes.forEach((item,paneIndex)=>{
                const paneSelect=document.querySelector(`[data-terminal-pane="${paneIndex}"] select[aria-label="主机"]`);
                if(paneSelect)paneSelect.innerHTML=hostWorkspaceHostOptions(item.hostId,terminalWorkspace.panes,paneIndex);
            });
            refreshTerminalWorkspacePane(i);
        }
        function refreshTerminalWorkspacePane(i){
            const pane=terminalWorkspace.panes[i],body=document.getElementById(`terminal-workspace-body-${i}`);if(!pane||!body)return;
            disposeTerminalWorkspaceInstance(i);
            const paneGeneration=(terminalWorkspace.paneGenerations.get(i)||0)+1;terminalWorkspace.paneGenerations.set(i,paneGeneration);
            // clone-replace 清除上一次 mount 残留的 dragover/drop 等事件监听器，
            // 避免刷新 N 次后拖放文件触发 N 次上传。
            body.replaceWith(body.cloneNode(false));
            terminalWorkspaceStatus(i,'正在刷新…');
            mountTerminalWorkspacePane(i,pane,terminalWorkspace.generation,paneGeneration);
        }
        function restoreHostTerminalWorkspacePane(i){
            const pane=terminalWorkspace.panes[i];if(!pane||pane.mode!=='adb')return;
            terminalWorkspace.panes[i]={hostId:pane.hostId};
            saveTerminalWorkspace();
            // Only reconnect the selected pane. Other terminals in a multi-pane
            // layout must keep their PTY/WebSocket sessions alive.
            terminalWorkspace.renderedSignature=terminalWorkspaceRenderSignature();
            const paneRoot=document.querySelector(`[data-terminal-pane="${i}"]`);
            const hostModeButton=paneRoot?.querySelector('[data-terminal-pane-host-mode]');
            if(hostModeButton)hostModeButton.style.display='none';
            const singleHostModeButton=document.getElementById('terminal-workspace-host-mode-btn');
            if(singleHostModeButton&&i===0)singleHostModeButton.style.display='none';
            refreshTerminalWorkspacePane(i);
        }
        function maximizeTerminalWorkspacePane(i){terminalWorkspace.maximized=terminalWorkspace.maximized===i?null:i;renderTerminalWorkspace();}
        function applyHostWorkspaceScopeMode(clusterEnabled) {
            const nextClusterEnabled = Boolean(clusterEnabled);
            // An ADB pane is a user-requested, one-shot target. Scope
            // initialization can race with openDeviceShell and otherwise
            // restore the saved SSH pane over it, disposing its WebSocket
            // before terminal_connect is sent.
            const activeAdbPane = terminalWorkspace.panes.find(pane =>
                pane?.mode === 'adb' && pane.serialNo && pane.workerId
            );
            // Workspace context also changes when a user selects a Worker.
            // Restoring the saved layout on every context event rolls the
            // first host selection back to the previous pane; only restore
            // layouts when the single/cluster scope itself actually changes.
            if (hostWorkspaceScopeModeInitialized && nextClusterEnabled === hostWorkspaceClusterEnabled) {
                return;
            }
            hostWorkspaceScopeModeInitialized = true;
            setHostWorkspaceSurfaceReady('desktop', false);
            setHostWorkspaceSurfaceReady('terminal', false);
            if (!nextClusterEnabled && hostWorkspaceClusterEnabled) {
                hostWorkspace.clusterState = snapshotHostClusterState();
                terminalWorkspace.clusterState = snapshotTerminalClusterState();
            }
            hostWorkspaceClusterEnabled = nextClusterEnabled;
            if (nextClusterEnabled) {
                if (hostWorkspace.clusterState) {
                    hostWorkspace.layout = hostWorkspace.clusterState.layout;
                    hostWorkspace.panes = hostWorkspace.clusterState.panes.map(pane => ({...pane}));
                    hostWorkspace.maximized = hostWorkspace.clusterState.maximized;
                }
                if (terminalWorkspace.clusterState) {
                    terminalWorkspace.layout = terminalWorkspace.clusterState.layout;
                    terminalWorkspace.panes = terminalWorkspace.clusterState.panes.map(pane => ({...pane}));
                    terminalWorkspace.maximized = terminalWorkspace.clusterState.maximized;
                }
            } else {
                useSingleHostWorkspaceState();
                useSingleTerminalWorkspaceState();
            }
            if (activeAdbPane && (nextClusterEnabled || isLocalWorkspaceWorker(activeAdbPane.workerId))) {
                const targetHost = workspaceHostForWorker(activeAdbPane.workerId);
                terminalWorkspace.layout = 'single';
                terminalWorkspace.maximized = null;
                terminalWorkspace.panes = [{
                    ...activeAdbPane,
                    // Preserve the pane identity while its WebSocket is
                    // mounting; changing hostId here changes the render
                    // signature and tears down the pending ADB connection.
                    ...(targetHost && !terminalWorkspace.instances.has(0)
                        && !terminalWorkspace.mountingPanes.has(0)
                        ? {hostId: targetHost.id} : {}),
                }];
            }
            if (window.hostWorkspaceInitialized) renderHostWorkspace();
            if (window.terminalWorkspaceInitialized) renderTerminalWorkspace();
        }
        Object.assign(window,{setTerminalWorkspaceLayout,changeTerminalWorkspaceHost,refreshTerminalWorkspacePane,restoreHostTerminalWorkspacePane,maximizeTerminalWorkspacePane,applyHostWorkspaceScopeMode});

