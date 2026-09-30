// Redmine settings; loaded by page.html in dependency order.
// ---- Add User Modal ----
async function populateDepartmentSelect(selectId, selectedId, includeAll) {
  await loadStatsConfig();
  var select = document.getElementById(selectId);
  if (!select) return;
  var html = departmentOptionsHtml(selectedId || '', includeAll);
  select.innerHTML = html || '<option value="">暂无部门</option>';
}
async function showAddUserModal() {
  document.getElementById('addUserId').value = '';
  document.getElementById('addUserName').value = '';
  document.getElementById('addUserEmail').value = '';
  var selected = (currentTab === 'department' && departmentProfileId && departmentProfileId !== 'all') ? departmentProfileId : '';
  await populateDepartmentSelect('addUserDepartment', selected, false);
  showModal('addUserModal');
  document.getElementById('addUserId').focus();
}
function hideAddUserModal() { hideModal('addUserModal'); }
async function submitAddUser() {
  var id = document.getElementById('addUserId').value.trim();
  var name = document.getElementById('addUserName').value.trim();
  var email = document.getElementById('addUserEmail').value.trim();
  var profileId = (document.getElementById('addUserDepartment') || {}).value || '';
  if (!id || !name) { notifyUser('添加用户失败', '请输入用户 ID 和姓名', 'warning'); return; }
  try {
    // 方案 2：成员写入共享组织架构（管理员权限；原 per-owner POST /users 已收编）。
    await api('/api/redmine-agent/org-chart/members', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: Number(id), name: name, email: email, department_id: profileId})
    });
    hideModal('addUserModal');
    statsUserInitialized = false;
    _statsConfigCacheTs = 0;
    await initStatsUserSelect();
    document.getElementById('statsUserSelect').value = name;
    if (currentTab === 'department') loadDepartmentOverdue(true);
    else onStatsUserChange();
  } catch (e) {
    if (e && /403|权限|admin/i.test(e.message)) {
      notifyUser('添加用户失败', '需要管理员权限（组织架构为全组共享，由管理员统一维护）', 'error');
    } else {
      notifyUser('添加用户失败', e.message, 'error');
    }
  }
}

// ---- Add Department Modal ----
function showAddDepartmentModal(targetSelectId) {
  pendingDepartmentTargetSelect = targetSelectId || 'departmentProfileSelect';
  document.getElementById('addDepartmentName').value = '';
  document.getElementById('addDepartmentId').value = '';
  showModal('addDepartmentModal');
  document.getElementById('addDepartmentName').focus();
}
function hideAddDepartmentModal() { hideModal('addDepartmentModal'); }
async function submitAddDepartment() {
  var name = document.getElementById('addDepartmentName').value.trim();
  var id = document.getElementById('addDepartmentId').value.trim();
  if (!name) { notifyUser('添加部门失败', '请输入部门名称', 'warning'); return; }
  try {
    var result = await api('/api/redmine-agent/dashboard/profiles', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, id: id})
    });
    hideAddDepartmentModal();
    _statsConfigCacheTs = 0;
    await loadStatsConfig();
    var profile = result.profile || {};
    if (pendingDepartmentTargetSelect === 'addUserDepartment') {
      await populateDepartmentSelect('addUserDepartment', profile.id || '', false);
    } else {
      departmentProfileId = profile.id || departmentProfileId;
      loadDepartmentOverdue(true);
    }
  } catch (e) { notifyUser('添加部门失败', e.message, 'error'); }
}

// ---- Add Project Modal ----
function showAddProjectModal() {
  document.getElementById('addProjectName').value = '';
  document.getElementById('addProjectId').value = '';
  showModal('addProjectModal');
  document.getElementById('addProjectName').focus();
}
function hideAddProjectModal() { hideModal('addProjectModal'); }
async function submitAddProject() {
  var name = document.getElementById('addProjectName').value.trim();
  var projectId = document.getElementById('addProjectId').value.trim();
  if (!projectId) { notifyUser('添加项目失败', '请输入项目标识', 'warning'); return; }
  try {
    var result = await api('/api/redmine-agent/dashboard/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, project_id: projectId})
    });
    hideAddProjectModal();
    _statsConfigCacheTs = 0;
    await loadStatsConfig();
    projectProfileId = (result.profile || {}).id || projectProfileId;
    loadProjectDashboard(true);
  } catch (e) { notifyUser('添加项目失败', e.message, 'error'); }
}

// ---- Settings Modal ----
function showSettingsModal() {
  showModal('settingsModal');
  // 每次打开都独立刷新晨报模型；统计/凭据设置失败不能让旧下拉选项残留。
  void refreshDailyBriefSettings();
  void initSelfBindingSelect();
  (async function() {
    try {
      await loadStatsConfig();
      document.getElementById('settingStaleDays').value = statsConfig.stale_days || 20;
      document.getElementById('settingWindowDays').value = statsConfig.window_days || 0;
      document.getElementById('settingCacheTtl').value = statsConfig.cache_ttl || 600;
      document.getElementById('settingFreshnessDays').value = statsConfig.freshness_days || 180;
      document.getElementById('settingRedmineBaseUrl').value = redmineBaseUrl();
      // SMTP fields from statsConfig (returned by get_stats_config)
      var cfg = await api('/api/redmine-agent/config/stats');
      var email = (cfg.dashboard || {}).email || {};
      document.getElementById('settingSmtpHost').value = email.smtp_host || '';
      document.getElementById('settingSmtpPort').value = email.smtp_port || 465;
      document.getElementById('settingFromAddr').value = email.from_addr || email.default_from_addr || '';
      document.getElementById('settingSmtpUser').value = email.username || '';
      document.getElementById('settingSmtpPass').value = '';
      // Redmine 凭据状态（已配置则回显用户名，密码不回显）
      try {
        var creds = await api('/api/redmine-agent/config/credentials');
        document.getElementById('settingRedmineUser').value = (creds && creds.username) || '';
        renderRedmineCredentialStatus(creds);
      } catch (_) { renderRedmineCredentialStatus(null); }
      document.getElementById('settingRedminePass').value = '';
      document.getElementById('settingRedmineApiKey').value = '';
    } catch (_) {}
  })();
}
function hideSettingsModal() { hideModal('settingsModal'); }

// ---- 个人身份绑定（方案 2：全局组织架构共享，个人绑定/别名 per-owner）----
async function initSelfBindingSelect() {
  var select = document.getElementById('settingSelfBinding');
  if (!select) return;
  try {
    var users = await api('/api/redmine-agent/users');
    var binding = await api('/api/redmine-agent/me/binding').catch(function() { return {member: null}; });
    var boundId = binding && binding.member ? String(binding.member.id || '') : '';
    var items = (users.items || []).slice().sort(function(a, b) {
      return (a.name || '').localeCompare(b.name || '', 'zh-Hans-CN-u-co-pinyin');
    });
    select.innerHTML = '<option value="">未绑定（按姓名/邮箱自动识别）</option>'
      + items.map(function(item) {
        var dept = item.department ? ' · ' + item.department : '';
        return '<option value="' + esc(item.id) + '">#' + esc(item.id) + ' ' + esc(item.name) + dept + '</option>';
      }).join('');
    select.value = boundId;
    if (select.value !== boundId) select.value = '';
  } catch (_) { select.innerHTML = '<option value="">（组织架构不可用）</option>'; }
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
    notifyUser(value ? '已绑定身份' : '已解除绑定', value ? '个人看板与每日晨报将按绑定的组织成员识别' : '恢复按姓名/邮箱自动识别', 'success');
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
  } catch (_) {
    // The individual selectors render a visible fallback option on failure.
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

async function saveSettings() {
  var stale = parseInt(document.getElementById('settingStaleDays').value) || 20;
  var window_ = parseInt(document.getElementById('settingWindowDays').value) || 60;
  var cacheTtl = parseInt(document.getElementById('settingCacheTtl').value) || 600;
  var freshnessDays = parseInt(document.getElementById('settingFreshnessDays').value) || 180;
  var redmineBase = document.getElementById('settingRedmineBaseUrl').value.trim();
  try {
    // Save stats config
    var result = await api('/api/redmine-agent/config/stats', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({base_url: redmineBase, stale_days: stale, window_days: window_, cache_ttl: cacheTtl, freshness_days: freshnessDays})
    });
    if (result) { statsConfig = Object.assign({}, statsConfig, result); _statsConfigCacheTs = Date.now(); }
    // Save SMTP config
    var smtpHost = document.getElementById('settingSmtpHost').value.trim();
    var smtpPort = parseInt(document.getElementById('settingSmtpPort').value) || 465;
    var fromAddr = document.getElementById('settingFromAddr').value.trim();
    var smtpUser = document.getElementById('settingSmtpUser').value.trim();
    var smtpPass = document.getElementById('settingSmtpPass').value;
    await api('/api/redmine-agent/config/email', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({smtp_host: smtpHost, smtp_port: smtpPort, from_addr: fromAddr, username: smtpUser, password: smtpPass})
    });
    // Redmine 凭据（仅当填写了密码才保存，避免误清空）
    var redmineUser = document.getElementById('settingRedmineUser').value.trim();
    var redminePass = document.getElementById('settingRedminePass').value;
    var redmineApiKey = document.getElementById('settingRedmineApiKey').value;
    if (redminePass) {
      await api('/api/redmine-agent/config/credentials', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: redmineUser, password: redminePass})
      });
    }
    if (redmineApiKey) {
      await api('/api/redmine-agent/config/credentials', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({api_key: redmineApiKey})
      });
    }
    try { renderRedmineCredentialStatus(await api('/api/redmine-agent/config/credentials')); } catch (_) {}
    var briefConfig = Object.assign({}, dailyBriefConfigCache || {});
    briefConfig.enabled = dailyBriefSetting('enabled').checked;
    briefConfig.trigger_time = dailyBriefSetting('trigger_time').value || '00:00';
    briefConfig.delta_enabled = dailyBriefSetting('delta_enabled').checked;
    briefConfig.delta_trigger_time = dailyBriefSetting('delta_trigger_time').value || '06:00';
    briefConfig.agent_profile = dailyBriefSetting('agent_profile').value.trim();
    briefConfig.model = dailyBriefSetting('model').value.trim();
    briefConfig.max_parallel_issues = parseInt(dailyBriefSetting('max_parallel_issues').value) || 1;
    briefConfig.max_turns = 0;
    briefConfig.issue_timeout_seconds = 0;
    dailyBriefConfigCache = await api('/api/redmine-agent/daily-brief/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(briefConfig)
    });
    _statsConfigCacheTs = 0; // force reload
    hideSettingsModal();
    refreshCurrentTab();
  } catch (e) { notifyUser('保存设置失败', e.message, 'error'); }
}
