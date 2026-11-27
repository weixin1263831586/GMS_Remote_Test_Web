// ---- Central settings ----
var dailyBriefConfigCache = null;
function dailyBriefSetting(name) {
  return document.querySelector('[data-daily-brief-setting="' + name + '"]');
}

function appendDailyBriefModelOption(select, value, label) {
  var option = document.createElement('option');
  option.value = String(value || '');
  option.textContent = String(label || value || '');
  select.appendChild(option);
}

async function loadDailyBriefAgentProfiles(selectedProfile) {
  var select = dailyBriefSetting('agent_profile');
  if (!select) return;
  var selected = String(selectedProfile || '').trim();
  select.replaceChildren();
  try {
    var payload = await api('/api/redmine-agent/daily-brief/agent-profiles') || {};
    var profiles = Array.isArray(payload.profiles) ? payload.profiles : [];
    var known = new Set();
    appendDailyBriefModelOption(select, '', profiles.length > 1 ? '请选择 kkagent Profile' :
      profiles.length ? '自动选择唯一 kkagent Profile' : '未发现本机 kkagent Profile');
    profiles.forEach(function(item) {
      var profile = String(item || '').trim();
      if (!profile || known.has(profile)) return;
      known.add(profile);
      appendDailyBriefModelOption(select, profile, profile);
    });
    if (!profiles.length) {
      appendDailyBriefModelOption(select, '', '未发现本机 kkagent Profile');
    } else if (selected && !known[selected]) {
      appendDailyBriefModelOption(select, selected, '当前保存的 Profile：' + selected);
    }
    select.value = selected;
  } catch (_) {
    appendDailyBriefModelOption(select, selected, selected || 'Profile 列表读取失败');
    select.value = selected;
  }
}

async function loadDailyBriefModelOptions(selectedModel) {
  var select = dailyBriefSetting('model');
  if (!select) return;
  var selected = String(selectedModel || '').trim();
  select.replaceChildren();
  try {
    var payload = await api('/api/redmine-agent/daily-brief/model-options') || {};
    var models = Array.isArray(payload.models) ? payload.models : [];
    var known = new Set();
    appendDailyBriefModelOption(select, '', '使用 kkagent 默认模型');
    models.forEach(function(item) {
      var model = String((item || {}).model || '').trim();
      if (!model || known.has(model)) return;
      known.add(model);
      var display = String((item || {}).display_name || model).trim() || model;
      var provider = String((item || {}).provider || '').trim();
      appendDailyBriefModelOption(select, model, provider ? display + '（' + provider + '）' : display);
    });
    if (!models.length) {
      appendDailyBriefModelOption(select, '', '未发现已启用的系统模型（使用 kkagent 默认模型）');
    } else if (selected && !known[selected]) {
      appendDailyBriefModelOption(select, selected, '当前保存的模型：' + selected);
    }
    select.value = selected;
  } catch (_) {
    appendDailyBriefModelOption(select, selected, selected || '模型列表读取失败（使用 kkagent 默认模型）');
    select.value = selected;
  }
}

async function loadStatsSettings() {
  var config = await api('/api/redmine-agent/config/stats');
  statsConfig = config;
  document.getElementById('settingStaleDays').value = config.stale_days || 20;
  document.getElementById('settingWindowDays').value = config.window_days || 0;
  document.getElementById('settingCacheTtl').value = config.cache_ttl ?? 600;
  document.getElementById('settingFreshnessDays').value = config.freshness_days || 180;
  document.getElementById('settingRedmineBaseUrl').value = redmineBaseUrl();
}

async function loadCredentialSettings() {
  var credentials = await api('/api/redmine-agent/config/credentials');
  document.getElementById('settingRedmineUser').value = credentials.username || '';
  renderRedmineCredentialStatus(credentials);
}

async function loadEmailSenderSummary() {
  var summary = document.getElementById('emailSenderSummary');
  try {
    var sender = await api('/api/email/smtp');
    summary.textContent = sender.smtp_host
      ? '发送方：' + sender.smtp_host + ':' + (sender.smtp_port || 465)
        + '（' + (sender.from_addr || sender.username || '-') + '）'
      : '发送方尚未配置，请联系管理员在「导航设置与使用帮助 → 系统配置」填写。';
  } catch (_) {
    summary.textContent = '发送方配置读取失败。';
  }
}

async function loadSettingsSection(id, load) {
  var section = document.getElementById(id);
  try {
    await load();
    section.disabled = false;
  } catch (error) {
    section.querySelector('[role="status"]').textContent = '读取失败：' + error.message;
  }
}

// ---- 个人身份绑定（方案 2：全局组织架构共享，个人绑定/别名 per-owner）----
async function initSelfBindingSelect() {
  var select = document.getElementById('settingSelfBinding');
  if (!select) return;
  try {
    var users = await api('/api/redmine-agent/users');
    var binding = await api('/api/redmine-agent/me/binding');
    var boundId = binding && binding.member ? String(binding.member.id || '') : '';
    var items = (users.items || []).slice().sort(function(a, b) {
      return (a.name || '').localeCompare(b.name || '', 'zh-Hans-CN-u-co-pinyin');
    });
    select.innerHTML = '<option value="">未绑定（按姓名/邮箱自动识别）</option>'
      + items.map(function(item) {
        var dept = item.department ? ' · ' + esc(item.department) : '';
        return '<option value="' + esc(item.id) + '">#' + esc(item.id) + ' ' + esc(item.name) + dept + '</option>';
      }).join('');
    select.value = boundId;
    if (select.value !== boundId) select.value = '';
  } catch (error) {
    select.replaceChildren();
    throw error;
  }
}

async function submitSelfBinding() {
  var select = document.getElementById('settingSelfBinding');
  if (!select) return;
  var value = select.value || null;
  try {
    await api('/api/redmine-agent/me/binding', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({member_id: value}),
    });
    notifySettingsSaved(value ? '已绑定身份' : '已解除绑定');
  } catch (e) { notifyUser('绑定失败', e.message, 'error'); }
}

async function refreshDailyBriefSettings() {
  try {
    dailyBriefConfigCache = await api('/api/redmine-agent/daily-brief/config') || {};
    dailyBriefSetting('enabled').checked = dailyBriefConfigCache.enabled === true;
    dailyBriefSetting('trigger_time').value = dailyBriefConfigCache.trigger_time || '00:00';
    dailyBriefSetting('delta_enabled').checked = dailyBriefConfigCache.delta_enabled !== false;
    dailyBriefSetting('delta_trigger_time').value = dailyBriefConfigCache.delta_trigger_time || '06:00';
    await Promise.all([
      loadDailyBriefAgentProfiles(dailyBriefConfigCache.agent_profile || ''),
      loadDailyBriefModelOptions(dailyBriefConfigCache.model || ''),
    ]);
    dailyBriefSetting('max_parallel_issues').value = dailyBriefConfigCache.max_parallel_issues || 1;
  } catch (error) {
    throw new Error('每日晨报配置读取失败：' + error.message);
  }
}

function renderRedmineCredentialStatus(status) {
  var el = document.getElementById('settingRedmineCredentialStatus');
  if (!el) return;
  if (!status) {
    el.textContent = '无法读取当前账号的 Redmine 凭据状态。';
    return;
  }
  if (status.configured) {
    el.textContent = status.api_key_configured
      ? '当前账号：Redmine 地址与 API Key 已配置。'
      : '当前账号：Redmine 地址与账号密码已配置。';
    return;
  }
  if (status.credential_error === 'stored_secret_unreadable') {
    el.textContent = '已保存 Redmine 凭据，但当前部署主密钥无法解密；请恢复原 master.key，或重新保存凭据。';
    return;
  }
  var missing = [];
  if (!status.base_url_configured) missing.push('地址');
  if (!status.password_configured && !status.api_key_configured) missing.push('账号密码或 API Key');
  el.textContent = '当前账号尚未配置：' + (missing.join('、') || 'Redmine 凭据') + '。';
}

function notifySettingsSaved(title) {
  notifyUser(title, '', 'success');
  if (window.parent !== window) {
    window.parent.postMessage({type: 'redmine-settings-saved'}, window.location.origin);
  }
}

function bindSettingsForm(id, save) {
  var form = document.getElementById(id);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    var button = form.querySelector('[type="submit"]');
    button.disabled = true;
    try {
      await save();
    } catch (error) {
      notifyUser('保存设置失败', error.message, 'error');
    } finally {
      button.disabled = false;
    }
  });
}

function writeSettings(url, method, payload) {
  return api(url, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
}

bindSettingsForm('redmine-stats-form', async () => {
  await writeSettings('/api/redmine-agent/config/stats', 'POST', {
    base_url: document.getElementById('settingRedmineBaseUrl').value.trim(),
    stale_days: Number(document.getElementById('settingStaleDays').value || 20),
    window_days: Number(document.getElementById('settingWindowDays').value || 0),
    cache_ttl: Number(document.getElementById('settingCacheTtl').value || 0),
    freshness_days: Number(document.getElementById('settingFreshnessDays').value || 180),
  });
  notifySettingsSaved('Redmine 统计与地址已保存');
});

bindSettingsForm('redmine-credentials-form', async () => {
  var username = document.getElementById('settingRedmineUser').value.trim();
  var password = document.getElementById('settingRedminePass');
  var apiKey = document.getElementById('settingRedmineApiKey');
  if (!password.value && !apiKey.value.trim()) return;
  if (password.value && !username) throw new Error('请输入 Redmine 账号');
  await writeSettings('/api/redmine-agent/config/credentials', 'POST',
    {username, password: password.value, api_key: apiKey.value.trim()});
  password.value = '';
  apiKey.value = '';
  notifySettingsSaved('Redmine 凭据已保存');
  try {
    renderRedmineCredentialStatus(await api('/api/redmine-agent/config/credentials'));
  } catch (_) { renderRedmineCredentialStatus(null); }
});

bindSettingsForm('redmine-brief-form', async () => {
  dailyBriefConfigCache = await writeSettings('/api/redmine-agent/daily-brief/config', 'PUT', {
    ...dailyBriefConfigCache,
    enabled: dailyBriefSetting('enabled').checked,
    trigger_time: dailyBriefSetting('trigger_time').value,
    delta_enabled: dailyBriefSetting('delta_enabled').checked,
    delta_trigger_time: dailyBriefSetting('delta_trigger_time').value,
    agent_profile: dailyBriefSetting('agent_profile').value.trim(),
    model: dailyBriefSetting('model').value.trim(),
    max_parallel_issues: Number(dailyBriefSetting('max_parallel_issues').value || 1),
  });
  notifySettingsSaved('每日晨报配置已保存');
});

void loadSettingsSection('redmine-stats-fields', loadStatsSettings);
void loadSettingsSection('redmine-credential-fields', loadCredentialSettings);
void loadSettingsSection('redmine-brief-fields', refreshDailyBriefSettings);
void loadSettingsSection('redmine-binding-fields', initSelfBindingSelect);
void loadEmailSenderSummary();
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && window.parent !== window) window.parent.closeSidebarVisibilityModal?.();
});

const settingsFrame = window.frameElement;
if (settingsFrame?.id === 'system-redmine-settings-frame') {
  document.documentElement.classList.add('embedded-system-settings');
  const form = document.getElementById('redmine-system-settings');
  function syncSettingsFrameHeight() {
    const bounds = form.getBoundingClientRect();
    if (bounds.width > 0 && bounds.height > 0) {
      const height = Math.ceil(bounds.bottom + window.scrollY);
      if (settingsFrame.style.height !== height + 'px') settingsFrame.style.height = height + 'px';
    }
  }
  new ResizeObserver(syncSettingsFrameHeight).observe(form);
  window.addEventListener('load', syncSettingsFrameHeight, {once:true});
  syncSettingsFrameHeight();
}
