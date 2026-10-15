'use strict';

(() => {
    const modalId = 'review-login-modal';
    const form = document.getElementById('review-login-form');
    const username = document.getElementById('review-login-username');
    const password = document.getElementById('review-login-password');
    const message = document.getElementById('review-login-message');
    const error = document.getElementById('review-login-error');
    const submit = document.getElementById('review-login-submit');
    let busy = false;

    function open(reason) {
        message.textContent = reason || '请登录管理员 gms 账号后查看和批准请求。';
        error.textContent = '';
        if (!ModalManager.isOpen(modalId)) ModalManager.open(modalId);
    }

    ModalManager.onClose(modalId, () => { password.value = ''; });
    document.getElementById('review-login-cancel').addEventListener('click', () => ModalManager.close(modalId));
    document.getElementById('review-login-open').addEventListener('click', () => open());
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy || !form.reportValidity()) return;
        busy = true;
        submit.disabled = true;
        error.textContent = '';
        try {
            const body = JSON.stringify({username: username.value.trim(), password: password.value,
                account_switch_target: 'admin'});
            password.value = '';
            const response = await fetch('/api/auth/login', {method: 'POST', credentials: 'same-origin',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'}, body});
            const result = await response.json();
            if (!response.ok || result.success === false || !result.authenticated) {
                throw new Error(result.error || result.detail || '登录失败，请重新输入密码');
            }
            if (result.user?.role !== 'admin') throw new Error('请使用管理员 gms 账号登录');
            ModalManager.close(modalId);
            window.dispatchEvent(new Event('device-review-authenticated'));
        } catch (failure) {
            error.textContent = failure.message;
            if (ModalManager.isOpen(modalId)) password.focus();
        } finally {
            password.value = '';
            busy = false;
            submit.disabled = false;
        }
    });
    window.DeviceReviewLogin = {open};
})();
