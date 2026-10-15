"""Public review-page login shell; request data stays behind authenticated APIs."""

LOGIN_ASSETS = '''<script src="/static/js/modal.js" defer></script>
<script src="/static/js/pages/device-review-login.js" defer></script>'''

LOGIN_PANEL = '''<div id="review-login-modal" class="modal" aria-hidden="true" aria-labelledby="review-login-title">
<section class="modal-content" tabindex="-1">
<h2 id="review-login-title">登录管理员账号</h2>
<p id="review-login-message">请登录管理员 gms 账号后查看和批准请求。</p>
<form id="review-login-form">
<label for="review-login-username">管理员账号</label>
<input id="review-login-username" name="username" value="gms" autocomplete="username" required>
<label for="review-login-password">密码</label>
<input id="review-login-password" name="password" type="password" autocomplete="current-password" autofocus required>
<p id="review-login-error" role="alert"></p>
<button id="review-login-submit" type="submit">登录并返回审批</button>
<button id="review-login-cancel" type="button">暂不登录</button>
</form></section></div>'''
