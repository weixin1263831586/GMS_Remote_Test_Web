// Cluster worker deploy/config chunk; loaded by page.html in dependency order.
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
 try{const d=await api(`/api/cluster/workers/${encodeURIComponent(id)}/config`);const cfg=d.config||{};input.value=cfg.max_jobs??'';input.disabled=false;input.placeholder=window.GMS_CLUSTER_DEFAULT_MAX_JOBS||'';input.focus({preventScroll:true})}
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
function updateDeployCommand(){const id=normalizedWorkerId(document.querySelector('#new-worker-id').value)||'WORKER_ID',host=document.querySelector('#new-worker-host').value.trim()||'USER@HOST',address=host.includes('@')?host.split('@').slice(1).join('@'):'HOST',controller=document.querySelector('#controller-url').value.trim()||location.origin,root=document.querySelector('#suite-root').value.trim()||'~/GMS-Suite';document.querySelector('#deploy-command').textContent=`test -f tools/adbproxy-rs/dist/adbproxy-rs-linux-x86_64-musl.tar.gz || { echo '请先在 Controller 执行 scripts/build_adbproxy_rs.sh'; exit 1; }; test -f tools/gms-worker-native/dist/x86_64/SHA256SUMS || { echo '请先在 Controller 执行 scripts/build_gms_worker_native.sh'; exit 1; }; rsync -azR worker_agent foundation scripts/install_cluster_worker.sh scripts/install_gms_worker_native.sh scripts/install_adbproxy_rs.sh scripts/gms_worker_usbip.sh scripts/run_GSI_Burn.sh scripts/run_GMS_Test_Auto.sh tools/gms-worker-native/dist tools/adbproxy-rs/dist tools/upgrade_tool tools/misc.img tools/scrcpy-linux-x86_64-v3.3.4 tools/GMS-Host-Tools ${host}:~/gms-worker-setup/ && scp "$GMS_GTS_CREDENTIAL_FILE" ${host}:~/gms-worker-gts.json && ssh ${host} 'cd ~/gms-worker-setup && GMS_DEFAULT_MAX_JOBS=${window.GMS_CLUSTER_DEFAULT_MAX_JOBS||''} bash scripts/install_cluster_worker.sh ${id} ${controller} WORKER_TOKEN - ${root} ${address} ~/gms-worker-gts.json'`}
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
