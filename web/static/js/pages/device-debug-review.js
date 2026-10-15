'use strict';

(() => {
    const endpoint = location.pathname.replace(/\/review$/, '');
    const details = document.getElementById('details');
    const status = document.getElementById('status');
    const history = document.getElementById('operations');
    const approve = document.getElementById('approve');
    const revoke = document.getElementById('revoke');
    const labels = {pending: '等待批准（申请后 5 分钟内有效）', approving: '正在批准…',
        active: '调试授权有效', expired: '调试授权已到期，请重新申请', revoked: '调试授权已撤销'};
    const actions = {install: '覆盖安装同包名、同签名 APK', launch: '启动应用', stop: '停止应用',
        tap: '应用内点击', swipe: '应用内滑动', text: '应用内输入文本', keyevent: '应用内导航按键'};
    let busy = false;
    let timer;

    async function request(method, path) {
        const response = await fetch(path, {method, credentials: 'same-origin', headers: {'Accept': 'application/json'}});
        const body = await response.json();
        if (!response.ok || body.success === false) throw new Error(body.error || '请求失败，请登录平台后重试');
        return body;
    }

    async function refresh() {
        clearTimeout(timer);
        try {
            const {session: s, operations} = await request('GET', endpoint);
            details.replaceChildren();
            const entries = [['Agent', s.actor_id], ['Worker', s.worker_id], ['设备', s.serial], ['应用', s.package],
                ['签名 SHA256', s.signers.join(', ')], ['首次 APK SHA256', s.baseline_sha256],
                ['授权范围', s.operations.map(a => actions[a] || a).join('、')],
                ['调试时长', `${s.duration_minutes} 分钟（从批准时开始）`],
                [s.status === 'pending' ? '审批截止' : '有效至', new Date(s.expires_at * 1000).toLocaleString()]];
            for (const [label, value] of entries) {
                const term = document.createElement('dt');
                const description = document.createElement('dd');
                term.textContent = label;
                description.textContent = value;
                details.append(term, description);
            }
            status.textContent = labels[s.status] || s.status;
            if (s.blocked_reason) status.textContent += '，暂不可执行：' + s.blocked_reason;
            if (s.busy) status.textContent += '，正在执行操作';
            approve.disabled = busy || s.status !== 'pending';
            revoke.disabled = busy || !['pending', 'approving', 'active'].includes(s.status);
            history.replaceChildren();
            for (const operation of operations || []) {
                const item = document.createElement('li');
                item.textContent = `${new Date(operation.started_at * 1000).toLocaleTimeString()} ${actions[operation.action] || operation.action}：${operation.status}`;
                if (operation.result?.error) item.textContent += '，' + operation.result.error;
                history.append(item);
            }
            if (['pending', 'approving', 'active'].includes(s.status)) timer = setTimeout(refresh, 3000);
        } catch (error) {
            status.textContent = error.message;
            approve.disabled = true;
            revoke.disabled = true;
        }
    }

    for (const [button, action] of [[approve, 'approve'], [revoke, 'revoke']]) {
        button.addEventListener('click', async () => {
            if (busy) return;
            busy = true;
            approve.disabled = true;
            revoke.disabled = true;
            let message = '';
            try { await request('POST', endpoint + '/' + action); }
            catch (error) { message = error.message; }
            finally { busy = false; await refresh(); if (message) status.textContent = message; }
        });
    }
    window.addEventListener('pagehide', () => clearTimeout(timer), {once: true});
    refresh();
})();
