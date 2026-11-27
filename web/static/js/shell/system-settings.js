// System settings shared by all workspace pages.
let systemSettingsLoad = null;
let systemConfigGeneration = 0;
let systemSettingsGeneration = 0;
let modelCatalogRequest = 0;
let staticRoutesLoaded = false;
function resetSystemSettings() {
    systemSettingsGeneration++;
    systemConfigGeneration++;
    modelCatalogRequest++;
    systemSettingsLoad = null;
    const panel = document.getElementById('sidebar-settings-panel-system');
    if (panel) panel.scrollTop = 0;
    staticRoutesLoaded = false;
    emailSenderConfigLoaded = false;
    const frame = document.getElementById('system-redmine-settings-frame');
    if (frame) frame.removeAttribute('src');
    document.getElementById('system-config-body')?.replaceChildren();
    const input = document.getElementById('gms-assistant-url');
    if (input) { input.value = ''; delete input.dataset.loaded; }
}
function loadSystemSettings() {
    if (!systemSettingsLoad) {
        const frame = document.getElementById('system-redmine-settings-frame');
        if (frame && !frame.getAttribute('src')) frame.src = '/redmine-agent?settings=1';
        systemSettingsLoad = Promise.allSettled([
            loadSystemRuntimeConfig(), loadSystemModelCatalog(), loadExternalServiceConfig(),
        ]);
    }
    return systemSettingsLoad;
}

async function scrollSystemSettingsTo(sectionName) {
    if (!['runtime', 'models', 'redmine', 'external'].includes(sectionName)) return;
    const pending = loadSystemSettings();
    await pending;
    if (pending !== systemSettingsLoad) return;
    const panel = document.getElementById('sidebar-settings-panel-system');
    const section = panel?.querySelector('[data-system-settings-section="' + sectionName + '"]');
    if (!section || panel.hidden || !ModalManager.isOpen('sidebar-visibility-modal')) return;
    panel.scrollTop += section.getBoundingClientRect().top - panel.getBoundingClientRect().top;
}

async function loadSystemModelCatalog(probeProvider = '') {
    const output = document.getElementById('system-model-catalog');
    if (!output) return;
    const request = ++modelCatalogRequest;
    output.textContent = '正在读取统一模型目录…';
    try {
        const query = probeProvider ? '?probe=true&provider=' + encodeURIComponent(probeProvider) : '';
        const result = await apiCall('/api/config/ai' + query, 'GET', null, {silentToast: true});
        if (request !== modelCatalogRequest) return;
        const data = result?.data || result || {};
        const providers = data.status?.providers || [];
        output.replaceChildren();
        const summary = document.createElement('p');
        summary.textContent = '默认 Provider：' + (data.status?.primary_provider || '未配置')
            + '；本地 Provider：' + (data.status?.local_provider || '未配置');
        output.append(summary);
        const engine = document.createElement('p');
        engine.textContent = 'KKAgent：' + (data.status?.kkagent_installed ? '已安装（不代表模型在线）' : '未检测到可执行文件');
        output.append(engine);
        const routes = data.status?.routes || {};
        for (const [label, route] of [['默认文本', routes.default_text], ['本地 RAG', routes.local_rag], ['私有图片', routes.private_vision]]) {
            const row = document.createElement('p');
            row.textContent = label + '：' + (route || '未配置合格路由');
            output.append(row);
        }
        const stateLabels = {disabled:'未启用', incomplete:'配置不完整', credential_missing:'缺少凭据', ready_to_probe:'待检测', available:'检测成功', unavailable:'检测失败'};
        for (const provider of providers) {
            const row = document.createElement('div');
            row.className = 'modal-form-row';
            const label = document.createElement('span');
            label.textContent = [provider.provider, provider.model, stateLabels[provider.state] || provider.state,
                provider.checked && Number.isFinite(provider.latency_ms) ? provider.latency_ms + ' ms' : ''].filter(Boolean).join(' · ');
            const probe = document.createElement('button');
            probe.type = 'button';
            probe.className = 'btn-xxs';
            probe.textContent = '检测';
            probe.disabled = !provider.enabled || !provider.configured || !provider.credential_configured;
            probe.addEventListener('click', () => { void loadSystemModelCatalog(provider.provider); });
            row.append(label, probe);
            output.append(row);
        }
        if (!providers.length) output.append(document.createTextNode('未配置模型。'));
    } catch (error) {
        if (request === modelCatalogRequest) output.textContent = '模型目录读取失败：' + error.message;
    }
}

async function loadSystemRuntimeConfig() {
    const modalBody = document.getElementById('system-config-body');
    if (!modalBody) return;
    const generation = ++systemConfigGeneration;
    staticRoutesLoaded = false;
    emailSenderConfigLoaded = false;
    modalBody.textContent = '正在读取运行配置…';

    let config = {};
    try {
        config = await apiCall('/api/config/read', 'GET', null, {background: true, silentToast: true});
    } catch (error) {
        if (generation !== systemConfigGeneration) return;
        modalBody.textContent = '获取配置失败：' + error.message;
        if (error.status === 403) {
            const unlock = document.createElement('button');
            unlock.type = 'button';
            unlock.className = 'btn-xxs';
            unlock.textContent = '验证管理员权限';
            unlock.addEventListener('click', async () => {
                if (await requestElevatedAccess() && generation === systemConfigGeneration) {
                    await loadSystemRuntimeConfig();
                }
            });
            modalBody.append(' ', unlock);
        }
        return;
    }
    if (generation !== systemConfigGeneration) return;
    const usbipVidPids = Array.isArray(config.usbip_vid_pids)
        ? config.usbip_vid_pids.join(', ')
        : String(config.usbip_vid_pid || '');

    // ADR：结构用 HTML、数据用 DOM property —— 模板不插入任何配置数据，
    // 避免 " < > & 破坏属性 / DOM 注入 / 字段消失类 UI 完整性问题。
    modalBody.innerHTML = `
        <div class="config-section config-section-notice">
            部署拓扑（用户/主机/路径/VID:PID）为静态配置：请由管理员在
            <code>configs/local/config.json</code> 修改后重启生效；此处只读展示，
            密码经环境变量（GMS_UBUNTU_PASSWORD 等）注入，不在 Web 端保存。
        </div>
        <form id="system-runtime-config-form" autocomplete="off">
        <div class="config-section config-section-accent-deploy">
            <div class="config-section-title">部署拓扑（只读）</div>
            <div class="modal-form-row">
                <label>测试主机用户:</label>
                <input type="text" id="config-ubuntu-user" class="readonly" readonly />
            </div>
            <div class="modal-form-row">
                <label>测试主机地址:</label>
                <input type="text" id="config-ubuntu-host" class="readonly" readonly />
            </div>
            <div class="modal-form-row">
                <label>设备主机地址:</label>
                <input type="text" id="config-device-host" class="readonly" readonly />
            </div>
            <div class="modal-form-row">
                <label>本地主机地址:</label>
                <input type="text" id="config-local-server" />
            </div>
            <div class="modal-form-row">
                <label>设备VID:PID:</label>
                <input type="text" id="config-usbip-vid-pids" class="readonly" readonly placeholder="例如: 2207:0006, 18d1:4d00" />
            </div>
            <div class="modal-form-row">
                <label>测试脚本路径:</label>
                <input type="text" id="config-script-path" class="readonly" readonly />
            </div>
            <div class="modal-form-row">
                <label>测试套件路径:</label>
                <input type="text" id="config-suites-path" class="readonly" readonly />
            </div>
        </div>

        <div class="config-section config-section-accent-logs">
            <div class="config-section-title">日志显示设置</div>
            <div class="modal-form-row">
                <label>历史日志条数:</label>
                <input type="number" id="config-log-history-limit" min="10" max="2000" />
            </div>
            <div class="modal-form-row">
                <label>实时日志上限:</label>
                <input type="number" id="config-log-max-entries" min="50" max="10000" />
            </div>
            <div class="settings-action-row" id="system-config-footer">
                <small>历史日志：首次加载状态时显示的条数；实时日志：页面每个Tab最多保留的条数。保存后即时生效。</small>
                <button type="submit" class="btn-xxs btn-primary">保存运行配置</button>
            </div>
        </div>
        </form>

        <form id="system-smtp-form" class="config-section config-section-accent-email" autocomplete="off">
            <div class="config-section-title">邮件发送方（系统级 SMTP）</div>
            <div class="modal-form-row">
                <label>启用邮件发送:</label>
                <label style="margin:0;">
                    <input type="checkbox" id="config-email-enabled" style="vertical-align:middle;" />
                    开启后 Redmine 提醒、ATS 通知、运维告警等全部邮件出口生效
                </label>
            </div>
            <div class="modal-form-row">
                <label>SMTP 服务器:</label>
                <input type="text" id="config-email-smtp-host" placeholder="例如: smtphz.qiye.163.com" />
            </div>
            <div class="modal-form-row">
                <label>端口:</label>
                <input type="number" id="config-email-smtp-port" min="1" max="65535" placeholder="465" />
            </div>
            <div class="modal-form-row">
                <label>SMTP 用户名:</label>
                <input type="text" id="config-email-username" autocomplete="off" />
            </div>
            <div class="modal-form-row">
                <label>发件人地址:</label>
                <input type="text" id="config-email-from-addr" placeholder="留空则使用用户名" autocomplete="off" />
            </div>
            <div class="modal-form-row">
                <label>授权码/密码:</label>
                <input type="password" id="config-email-password" placeholder="留空保持不变" autocomplete="new-password" />
            </div>
            <div class="settings-action-row">
                <small id="config-email-status">接收方邮箱在各业务处配置（如 Redmine 用户资料、运维告警 email_to），不在此处。</small>
                <button type="submit" class="btn-xxs btn-primary" disabled>保存 SMTP 配置</button>
            </div>
            <div class="settings-filter-row">
                <button class="btn-xxs" id="email-outbox-refresh-btn" type="button">↻ 刷新历史邮件</button>
                <label class="email-outbox-filter-label" style="margin:0;">通道:
                    <select id="email-outbox-channel" >
                        <option value="">全部</option>
                        <option value="manual">手动发送</option>
                        <option value="automation">自动化通知</option>
                        <option value="ops">运维告警</option>
                        <option value="redmine_reminder">Redmine 提醒</option>
                        <option value="update_radar">GMS更新雷达</option>
                    </select>
                </label>
                <label class="email-outbox-filter-label" style="margin:0;">结果:
                    <select id="email-outbox-sent" >
                        <option value="">全部</option>
                        <option value="true">成功</option>
                        <option value="false">失败</option>
                    </select>
                </label>
            </div>
            <div id="email-outbox-list" class="settings-outbox-list">
                <div class="settings-empty-state">点击「刷新历史邮件」查看本人邮件记录</div>
            </div>
            <small>记录每次出站邮件（含失败尝试）：时间、通道、收件人、主题与结果；正文仅存预览。自动化/运维邮件回链到对应任务/事件。</small>
        </form>

        <!-- 客户端 SSH 凭据管理（增删独立于上方静态配置，即时生效） -->
        <div class="config-section config-section-accent-creds">
            <div class="config-section-title">客户端 SSH 凭据</div>
            <table style="width:100%;border-collapse:collapse;">
                <thead>
                    <tr style="background: var(--darker-bg);">
                        <th style="text-align:left;">设备主机</th>
                        <th style="text-align:left;">用户名</th>
                        <th style="text-align:left;">密码</th>
                        <th style="text-align:center;">操作</th>
                    </tr>
                </thead>
                <tbody id="client-creds-table-body">
                    <tr><td colspan="4" style="text-align:center;">加载中...</td></tr>
                </tbody>
            </table>
            <div class="modal-form-row" style="margin-top:8px;gap:6px;">
                <input type="text" id="client-cred-host" placeholder="user@ip，例如 gms@192.168.1.100" autocomplete="off" style="flex:2;" />
                <input type="password" id="client-cred-password" placeholder="SSH 密码" autocomplete="new-password" style="flex:1.5;" />
                <button class="btn-xxs btn-primary" id="client-cred-add-btn" type="button">添加/更新</button>
            </div>
            <small>填入已有主机的地址+新密码可覆盖更新；密码不会回显明文。</small>
        </div>

        <!-- 静态路由管理（增删独立于上方静态配置，保存后立即应用到本机路由表） -->
        <div class="config-section config-section-accent-routes">
            <div class="config-section-title">静态路由（启动时与保存后自动应用）</div>
            <div class="modal-form-row" style="gap:8px;margin-bottom:6px;">
                <label style="margin:0;">
                    <input type="checkbox" id="config-static-routes-enabled" style="vertical-align:middle;" />
                    启用静态路由
                </label>
            </div>
            <table style="width:100%;border-collapse:collapse;">
                <thead>
                    <tr style="background: var(--darker-bg);">
                        <th style="text-align:left;">目标网段</th>
                        <th style="text-align:left;">网关</th>
                        <th style="text-align:center;">操作</th>
                    </tr>
                </thead>
                <tbody id="static-routes-table-body">
                    <tr><td colspan="3" style="text-align:center;">加载中...</td></tr>
                </tbody>
            </table>
            <div class="modal-form-row" style="margin-top:8px;gap:6px;">
                <input type="text" id="static-route-destination" placeholder="目标网段，例如 10.10.10.0/24 或 10.10.10.29/32" autocomplete="off" style="flex:2;" />
                <input type="text" id="static-route-gateway" placeholder="网关，例如 192.0.2.1" autocomplete="off" style="flex:1.5;" />
                <button class="btn-xxs btn-primary" id="static-route-add-btn" type="button">添加</button>
            </div>
            <div class="settings-action-row">
                <small>程序启动和每次保存路由时自动应用。修改路由表需要 root 或相应的 sudoers 权限。</small>
                <button type="button" id="save-static-routes" class="btn-xxs btn-primary" disabled>保存并应用路由</button>
            </div>
        </div>
    `;

    // 数据通过 DOM property 写入（不进 HTML 字符串）。
    const configValues = {
        'config-ubuntu-user': config.ubuntu_user,
        'config-ubuntu-host': config.ubuntu_host,
        'config-device-host': config.device_host,
        'config-local-server': config.local_server,
        'config-usbip-vid-pids': usbipVidPids,
        'config-script-path': config.script_path,
        'config-suites-path': config.suites_path,
        'config-log-history-limit':
            localStorage.getItem('gms-log-history-limit') || 100,
        'config-log-max-entries':
            localStorage.getItem('gms-log-max-entries') || 1000,
    };
    Object.entries(configValues).forEach(([id, value]) => {
        const input = document.getElementById(id);
        if (input) input.value = value ?? '';
    });

    bindSystemConfigHandlers(modalBody);
    await Promise.allSettled([
        loadClientCredentials(), loadStaticRoutes(), loadEmailSenderConfig(), loadEmailOutbox(),
    ]);
}

// 历史邮件（outbox ledger）：本人视角；管理员如需全量走 GET /api/email/outbox?admin_view=true。
const EMAIL_OUTBOX_CHANNEL_LABELS = {
    manual: '手动',
    automation: '自动化',
    ops: '运维告警',
    redmine_reminder: 'Redmine',
    update_radar: '更新雷达',
};

let emailOutboxRequest = 0;
async function loadEmailOutbox() {
    const listEl = document.getElementById('email-outbox-list');
    if (!listEl) return;
    const request = ++emailOutboxRequest;
    const channel = document.getElementById('email-outbox-channel')?.value || '';
    const sent = document.getElementById('email-outbox-sent')?.value || '';
    listEl.innerHTML = '<div class="settings-empty-state">加载中...</div>';
    try {
        const params = new URLSearchParams({limit: '30'});
        if (channel) params.set('channel', channel);
        if (sent) params.set('sent', sent);
        const result = await apiCall(`/api/email/outbox?${params.toString()}`, 'GET', null, {background: true, silentToast: true});
        if (request !== emailOutboxRequest || !listEl.isConnected) return;
        const data = result?.data || {};
        const items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) {
            listEl.innerHTML = '<div class="settings-empty-state">暂无发件记录（筛选条件下无匹配）</div>';
            return;
        }
        const rows = items.map((item) => {
            const state = item.sent
                ? '<span style="color:var(--success-color,#4caf50);">✓ 成功</span>'
                : `<span style="color:var(--error-color,#f44336);" title="${escapeJsAttr(item.error || '')}">✗ 失败</span>`;
            const channelLabel = EMAIL_OUTBOX_CHANNEL_LABELS[item.channel] || escapeJsAttr(item.channel || '未知');
            const ref = item.ref_type && item.ref_id
                ? `<span title="${escapeJsAttr(item.ref_type + ':' + item.ref_id)}" style="color:var(--text-muted,#888);"> ↳${escapeJsAttr(String(item.ref_id).slice(0, 14))}</span>`
                : '';
            const toText = (item.to_addrs || []).join(', ');
            const preview = escapeHtml(String(item.subject || '')) +
                (item.body_preview ? `　<span style="color:var(--text-muted,#888);">${escapeJsAttr(item.body_preview.slice(0, 60))}…</span>` : '');
            return `<tr>
                <td style="white-space:nowrap;">${escapeJsAttr(item.created_at || '')}</td>
                <td style="white-space:nowrap;">${channelLabel}${ref}</td>
                <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeJsAttr(toText)}">${escapeJsAttr(toText)}</td>
                <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeJsAttr(item.subject || '')}">${preview}</td>
                <td style="white-space:nowrap;">${state}</td>
            </tr>`;
        }).join('');
        listEl.innerHTML = `<table style="width:100%;border-collapse:collapse;">
            <thead><tr style="background:var(--darker-bg,#222);">
                <th style="text-align:left;">时间(UTC)</th>
                <th style="text-align:left;">通道</th>
                <th style="text-align:left;">收件人</th>
                <th style="text-align:left;">主题/预览</th>
                <th style="text-align:left;">结果</th>
            </tr></thead><tbody>${rows}</tbody></table>
            <div class="history-mail-count" style="padding:4px 6px;color:var(--text-muted,#888);">共 ${Number(data.total) || 0} 条，显示最近 ${items.length} 条</div>`;
    } catch (error) {
        if (request !== emailOutboxRequest || !listEl.isConnected) return;
        listEl.innerHTML = `<div class="settings-empty-state settings-empty-error">加载历史邮件失败: ${escapeJsAttr(error.message)}</div>`;
    }
}

// 邮件发送方（系统级 SMTP）：读全局配置回填；密码永不回显，留空表示保持不变。
// 配置弹框打开后这几个请求是并行的。保存发生在回填完成之前时，
// 空主机和默认未勾选会被当成用户修改，把系统发信关掉。
let emailSenderConfigLoaded = false;

async function loadEmailSenderConfig() {
    emailSenderConfigLoaded = false;
    const form = document.getElementById('system-smtp-form');
    const status = form.querySelector('#config-email-status');
    try {
        const result = await apiCall('/api/email/smtp', 'GET', null, {background: true, silentToast: true});
        if (!form.isConnected) return;
        const data = result?.data || result || {};
        const setValue = (id, value) => {
            const input = document.getElementById(id);
            if (input) input.value = value ?? '';
        };
        setValue('config-email-smtp-host', data.smtp_host);
        setValue('config-email-smtp-port', data.smtp_port || 465);
        setValue('config-email-username', data.username);
        setValue('config-email-from-addr', data.from_addr);
        const enabledInput = document.getElementById('config-email-enabled');
        if (enabledInput) enabledInput.checked = data.enabled !== false;
        if (status) {
            const state = data.enabled === false ? '【已关闭】' : '';
            status.textContent = data.password_set
                ? `${state}当前已配置授权码（不回显；留空保持不变）。接收方邮箱在各业务处配置（如 Redmine 用户资料、运维告警 email_to）。`
                : `${state}尚未配置授权码。接收方邮箱在各业务处配置（如 Redmine 用户资料、运维告警 email_to）。`;
        }
        emailSenderConfigLoaded = true;
        form.querySelector('[type="submit"]').disabled = false;
    } catch (error) {
        if (status) status.textContent = '读取邮件发送方配置失败: ' + error.message;
    }
}

async function saveEmailSenderConfig(event) {
    event.preventDefault();
    if (!emailSenderConfigLoaded) return;
    const form = event.currentTarget;
    const button = form.querySelector('[type="submit"]');
    const password = form.querySelector('#config-email-password');
    const payload = {
        smtp_host: form.querySelector('#config-email-smtp-host').value.trim(),
        smtp_port: Number(form.querySelector('#config-email-smtp-port').value) || 465,
        username: form.querySelector('#config-email-username').value.trim(),
        from_addr: form.querySelector('#config-email-from-addr').value.trim(),
        password: password.value || '***',
        enabled: form.querySelector('#config-email-enabled').checked,
    };
    button.disabled = true;
    try {
        await apiCall('/api/email/smtp', 'POST', payload, {silentToast: true});
        password.value = '';
        showToast('SMTP 配置已保存', 'success');
    } catch (error) {
        showToast('保存 SMTP 配置失败：' + error.message, 'error');
    } finally {
        button.disabled = false;
    }
}

// 配置模态内动态渲染控件的事件绑定（CSP 收紧前置：去掉 inline handler）。
function bindSystemConfigHandlers(scope) {
    const outboxRefresh = scope.querySelector('#email-outbox-refresh-btn');
    if (outboxRefresh) {
        outboxRefresh.addEventListener('click', loadEmailOutbox);
    }
    const outboxChannel = scope.querySelector('#email-outbox-channel');
    if (outboxChannel) outboxChannel.addEventListener('change', loadEmailOutbox);
    const outboxSent = scope.querySelector('#email-outbox-sent');
    if (outboxSent) outboxSent.addEventListener('change', loadEmailOutbox);
    const form = scope.querySelector('#system-runtime-config-form');
    if (form) {
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            saveConfig();
        });
    }
    scope.querySelector('#system-smtp-form').addEventListener('submit', saveEmailSenderConfig);
    scope.querySelector('#save-static-routes').addEventListener('click', saveStaticRoutes);
    const addCredBtn = scope.querySelector('#client-cred-add-btn');
    if (addCredBtn) addCredBtn.addEventListener('click', addClientCredential);
    const addRouteBtn = scope.querySelector('#static-route-add-btn');
    if (addRouteBtn) addRouteBtn.addEventListener('click', addStaticRouteRow);
}

async function loadExternalServiceConfig() {
    const input = document.getElementById('gms-assistant-url');
    if (!input) return;
    delete input.dataset.loaded;
    const generation = systemSettingsGeneration;
    try {
        const result = await apiCall('/api/config/external-services', 'GET', null, {background: true, silentToast: true});
        if (generation !== systemSettingsGeneration) return;
        const data = result?.data || result || {};
        input.value = String(data.gms_assistant_url || '').trim();
        input.dataset.loaded = 'true';
    } catch (error) {
        if (generation === systemSettingsGeneration) showToast('读取 GMS助手配置失败: ' + error.message, 'error');
    }
}

async function saveGmsAssistantConfig() {
    const input = document.getElementById('gms-assistant-url');
    if (!input || input.dataset.loaded !== 'true') return;
    const url = String(input.value || '').trim();
    if (url && !/^https?:\/\/[^\s/$.?#].[^\s]*$/i.test(url)) {
        showToast('请输入完整的 http(s) 地址', 'warning');
        return;
    }
    try {
        await apiCall('/api/config/external-services', 'POST', {gms_assistant_url: url});
        showToast('GMS助手配置已保存', 'success');
        const frame = document.getElementById('gms-assistant-frame');
        const dataSrc = frame?.getAttribute('data-src');
        if (frame && dataSrc) {
            window.setLazyFrameSource?.(frame, dataSrc);
        }
    } catch (error) {
        showToast('保存 GMS助手配置失败: ' + error.message, 'error');
    }
}

// 客户端 SSH 凭据管理。
async function loadClientCredentials() {
    const tbody = document.getElementById('client-creds-table-body');
    if (!tbody) return;
    let credentials = [];
    try {
        const result = await apiCall('/api/config/client-ssh-credentials', 'GET', null, {background: true, silentToast: true});
        credentials = (result && (result.credentials || result.data?.credentials)) || [];
    } catch (error) {
        renderClientCredentialsMessage(tbody, `加载失败: ${error.message}`, 'var(--error-color)');
        return;
    }

    if (!credentials.length) {
        renderClientCredentialsMessage(tbody, '暂无凭据。在下方添加设备主机 SSH 凭据。', 'var(--text-secondary)');
        return;
    }

    tbody.replaceChildren();
    credentials.forEach(cred => {
        const host = cred.device_host || `${cred.username || ''}@${cred.host || ''}`.replace(/^@$/, '');
        const masked = cred.has_password ? '••••••' : '未设置';
        const row = document.createElement('tr');
        row.style.borderBottom = '1px solid var(--border-color)';

        appendClientCredentialCell(row, host || '-');
        appendClientCredentialCell(row, cred.username || '-');
        appendClientCredentialCell(row, masked, 'var(--text-secondary)');

        const actionCell = appendClientCredentialCell(row, '');
        actionCell.style.textAlign = 'center';
        const deleteButton = document.createElement('button');
        deleteButton.className = 'btn-xxs';
        deleteButton.type = 'button';
        deleteButton.textContent = '删除';
        deleteButton.addEventListener('click', () => deleteClientCredential(host));
        actionCell.appendChild(deleteButton);

        tbody.appendChild(row);
    });
}

function renderClientCredentialsMessage(tbody, message, color) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 4;
    cell.style.textAlign = 'center';
    if (color) cell.style.color = color;
    cell.textContent = message;
    row.appendChild(cell);
    tbody.replaceChildren(row);
}

function appendClientCredentialCell(row, text, color) {
    const cell = document.createElement('td');
    if (color) cell.style.color = color;
    cell.textContent = text;
    row.appendChild(cell);
    return cell;
}

async function addClientCredential() {
    const hostInput = document.getElementById('client-cred-host');
    const pwdInput = document.getElementById('client-cred-password');
    const deviceHost = (hostInput?.value || '').trim();
    const password = pwdInput?.value || '';

    if (!/^[^@\s<>"'`]+@[^@\s<>"'`]+$/.test(deviceHost)) {
        showToast('请输入 user@ip 格式的设备主机', 'warning');
        return;
    }
    if (!password) {
        showToast('请输入密码', 'warning');
        return;
    }

    try {
        await apiCall('/api/config/client-ssh-credentials', 'POST', { device_host: deviceHost, password });
        showToast('凭据已保存', 'success');
        if (hostInput) hostInput.value = '';
        if (pwdInput) pwdInput.value = '';
        await loadClientCredentials();
    } catch (error) {
        showToast('保存凭据失败: ' + error.message, 'error');
    }
}

async function deleteClientCredential(deviceHost) {
    if (!deviceHost) return;
    if (!await showConfirmDialog(
        '删除 SSH 凭据',
        `确认删除凭据：${deviceHost}？`
    )) return;
    try {
        await apiCall('/api/config/client-ssh-credentials', 'DELETE', { device_host: deviceHost });
        showToast('凭据已删除', 'success');
        await loadClientCredentials();
    } catch (error) {
        showToast('删除凭据失败: ' + error.message, 'error');
    }
}

// ==================== 静态路由管理 ====================
// 配置保存在运行时配置存储（static_routes 段），保存后立即应用到本机路由表。

function _readStaticRouteRows() {
    const tbody = document.getElementById('static-routes-table-body');
    if (!tbody) return [];
    return Array.from(tbody.querySelectorAll('tr[data-destination]')).map(row => ({
        destination: row.dataset.destination || '',
        gateway: row.dataset.gateway || '',
    }));
}

async function loadStaticRoutes() {
    const tbody = document.getElementById('static-routes-table-body');
    if (!tbody) return;
    const enabledCheckbox = document.getElementById('config-static-routes-enabled');
    let data = {};
    try {
        const result = await apiCall('/api/config/static-routes', 'GET', null, {background: true, silentToast: true});
        data = (result && (result.data || result)) || {};
    } catch (error) {
        if (tbody.isConnected) renderStaticRoutesMessage(`加载失败: ${error.message}`, 'var(--error-color)');
        return;
    }
    if (!tbody.isConnected || tbody !== document.getElementById('static-routes-table-body')) return;
    staticRoutesLoaded = true;
    document.getElementById('save-static-routes').disabled = false;
    if (enabledCheckbox) enabledCheckbox.checked = !!data.enabled;
    renderStaticRoutesTable(Array.isArray(data.routes) ? data.routes : []);
}

function renderStaticRoutesMessage(message, color) {
    const tbody = document.getElementById('static-routes-table-body');
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="3" style="text-align:center;${color ? `color:${color};` : ''}">${escapeHtml(message)}</td></tr>`;
}

function renderStaticRoutesTable(routes) {
    const tbody = document.getElementById('static-routes-table-body');
    if (!tbody) return;
    if (!routes.length) {
        renderStaticRoutesMessage('暂无静态路由。在下方添加目标网段和网关。', 'var(--text-secondary)');
        return;
    }
    tbody.innerHTML = routes.map(route => `
        <tr data-destination="${escapeHtml(route.destination)}" data-gateway="${escapeHtml(route.gateway)}" style="border-bottom: 1px solid var(--border-color);">
            <td class="ui-mono">${escapeHtml(route.destination)}</td>
            <td class="ui-mono">${escapeHtml(route.gateway)}</td>
            <td style="text-align:center;">
                <button class="btn-xxs" type="button" data-action="delete-static-route">删除</button>
            </td>
        </tr>
    `).join('');
    tbody.querySelectorAll('[data-action="delete-static-route"]').forEach((btn) => {
        btn.addEventListener('click', () => deleteStaticRouteRow(btn));
    });
}

function addStaticRouteRow() {
    const destInput = document.getElementById('static-route-destination');
    const gwInput = document.getElementById('static-route-gateway');
    const destination = (destInput?.value || '').trim();
    const gateway = (gwInput?.value || '').trim();

    if (!/^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/.test(destination)) {
        showToast('目标网段格式应为 a.b.c.d/掩码，例如 10.10.10.0/24', 'warning');
        return;
    }
    if (!/^(\d{1,3}\.){3}\d{1,3}$/.test(gateway)) {
        showToast('网关格式应为 IP 地址，例如 192.0.2.1', 'warning');
        return;
    }

    const routes = _readStaticRouteRows();
    if (routes.some(r => r.destination === destination && r.gateway === gateway)) {
        showToast('该路由已存在', 'warning');
        return;
    }
    routes.push({destination, gateway});
    renderStaticRoutesTable(routes);
    if (destInput) destInput.value = '';
    if (gwInput) gwInput.value = '';
}

function deleteStaticRouteRow(button) {
    const row = button?.closest('tr[data-destination]');
    if (row) row.remove();
}

async function saveStaticRoutes() {
    if (!staticRoutesLoaded) {
        showToast('静态路由尚未读取成功，本次未修改路由配置', 'warning');
        return;
    }
    const enabledCheckbox = document.getElementById('config-static-routes-enabled');
    const payload = {
        enabled: !!(enabledCheckbox && enabledCheckbox.checked),
        routes: _readStaticRouteRows(),
    };
    const button = document.getElementById('save-static-routes');
    button.disabled = true;
    try {
        const result = await apiCall('/api/config/static-routes', 'POST', payload, {silentToast: true});
        const data = (result && (result.data || result)) || {};
        if (data.success) {
            showToast('静态路由已保存并应用', 'success');
        } else {
            showToast(data.error || '路由已保存，但应用失败（需要 root/sudoers 免密权限）', 'warning');
        }
        if (button.isConnected) renderStaticRoutesTable(payload.routes);
    } catch (error) {
        showToast('保存静态路由失败: ' + error.message, 'error');
    } finally {
        button.disabled = false;
    }
}

async function saveConfig() {
    // /api/config/update 只接受运行时字段（client_hosts / local_server）。
    // 静态部署拓扑（用户/主机/路径/VID:PID）默认在 configs/local/config.json
    // （支持 ${ENV} 占位符），SSH 密码存 configs/secrets/runtime_credentials.json；
    // Web 端不随 /api/config/update 提交。
    const localServer = document.getElementById('config-local-server').value;
    const historyLimit = document.getElementById('config-log-history-limit').value || '100';
    const maxEntries = document.getElementById('config-log-max-entries').value || '1000';
    const button = document.querySelector('#system-config-footer button');
    button.disabled = true;
    try {
        await saveLocalServer(localServer);
        localStorage.setItem('gms-log-history-limit', historyLimit);
        localStorage.setItem('gms-log-max-entries', maxEntries);
        showToast('运行配置已保存', 'success');
    } catch (error) {
        showToast('保存运行配置失败：' + error.message, 'error');
    } finally {
        button.disabled = false;
    }
}
