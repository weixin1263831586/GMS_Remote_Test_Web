'use strict';

(() => {
    const endpoint = location.pathname.replace(/\/review$/, '');
    const details = document.getElementById('details');
    const status = document.getElementById('status');
    const approve = document.getElementById('approve');
    const labels = {pending: '等待批准（上传后 5 分钟内有效）', queued: '已批准，等待执行…', running: '正在传输并安装…', interrupted: '执行中断，请核实设备安装状态后重新提交',
        completed: '安装完成', failed: '安装失败', expired: '请求已过期，请重新上传 APK'};
    let busy = false;
    let timer;
    let failures = 0;
    let stopped = false;
    let hidden = false;

    async function request(method, path) {
        const response = await fetch(path, {method, credentials: 'same-origin',
            headers: {'Accept': 'application/json'}});
        if ([401, 403].includes(response.status)) {
            stopped = true;
            clearTimeout(timer);
            details.replaceChildren();
            const reason = '登录已失效或权限不足，请重新登录管理员 gms 账号。';
            window.DeviceReviewLogin.open(reason);
            throw new Error(reason);
        }
        if (response.status === 404) {
            stopped = true;
            clearTimeout(timer);
            details.replaceChildren();
            const reason = '请求不存在或不属于当前登录账号。请登录发起申请的 Agent 所属平台账号（管理员 gms）；仍不可用时请由原 Agent 重新申请。';
            window.DeviceReviewLogin.open(reason);
            throw new Error(reason);
        }
        const body = await response.json();
        if (!response.ok || body.success === false) throw new Error(body.error || '请求失败，稍后自动重试');
        return body;
    }

    async function refresh() {
        clearTimeout(timer);
        if (stopped) return;
        let poll = true;
        try {
            const {deployment: d} = await request('GET', endpoint);
            details.replaceChildren();
            const entries = [['APK', d.filename], ['包名', d.package || '未知'],
                ['版本', `${d.version_name || ''} (${d.version_code || ''})`],
                ['签名证书 SHA256', (d.signers || []).join(', ')],
                ['设备已安装签名', d.installed_identity?.signers?.join(', ') || '未安装'],
                ['签名匹配', d.installed_signers_match === null ? '未安装' : d.installed_signers_match ? '一致' : '不一致'],
                ['SHA256', d.sha256], ['Worker', d.worker_id],
                ['设备', d.serial], ['安装方式', d.replace_existing ? '覆盖安装，保留应用数据（-r）' : '首次安装'],
                ['有效至', new Date(d.expires_at * 1000).toLocaleString()]];
            entries.forEach(([label, value]) => {
                const term = document.createElement('dt');
                const description = document.createElement('dd');
                term.textContent = label;
                description.textContent = value;
                details.append(term, description);
            });
            status.textContent = labels[d.status] || d.status;
            if (d.result?.error) status.textContent += '：' + d.result.error;
            approve.disabled = busy || d.status !== 'pending';
            failures = 0;
            poll = ['running', 'queued', 'pending'].includes(d.status);
        } catch (error) {
            status.textContent = error.message;
            approve.disabled = true;
            failures += 1;
        } finally {
            if (poll && !stopped) timer = setTimeout(refresh, Math.min(30000, 3000 * 2 ** failures));
        }
    }

    approve.addEventListener('click', async () => {
        if (busy) return;
        busy = true;
        approve.disabled = true;
        let errorMessage = '';
        try {
            await request('POST', endpoint + '/approve');
        } catch (error) {
            errorMessage = error.message;
        } finally {
            busy = false;
            await refresh();
            if (errorMessage) status.textContent = errorMessage;
        }
    });
    window.addEventListener('device-review-authenticated', () => {
        if (hidden) return;
        stopped = false;
        failures = 0;
        refresh();
    });
    window.addEventListener('pagehide', () => { hidden = true; stopped = true; clearTimeout(timer); }, {once: true});
    refresh();
})();
