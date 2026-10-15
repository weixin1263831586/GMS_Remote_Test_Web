'use strict';

(() => {
    const endpoint = location.pathname.replace(/\/review$/, '');
    const details = document.getElementById('details');
    const status = document.getElementById('status');
    const approve = document.getElementById('approve');
    const labels = {pending: '等待批准（上传后 5 分钟内有效）', running: '正在传输并安装…',
        completed: '安装完成', failed: '安装失败', expired: '请求已过期，请重新上传 APK'};
    let busy = false;
    let timer;

    async function request(method, path) {
        const response = await fetch(path, {method, credentials: 'same-origin',
            headers: {'Accept': 'application/json'}});
        const body = await response.json();
        if (!response.ok || body.success === false) throw new Error(body.error || '请求失败，请登录平台后重试');
        return body;
    }

    async function refresh() {
        clearTimeout(timer);
        try {
            const {deployment: d} = await request('GET', endpoint);
            details.replaceChildren();
            const entries = [['APK', d.filename], ['SHA256', d.sha256], ['Worker', d.worker_id],
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
            if (d.status === 'running' || d.status === 'pending') timer = setTimeout(refresh, 3000);
        } catch (error) {
            status.textContent = error.message;
            approve.disabled = true;
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
    window.addEventListener('pagehide', () => clearTimeout(timer), {once: true});
    refresh();
})();
