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

function showSettingsModal() {
  if (window.parent !== window && typeof window.parent.openSidebarVisibilityModal === 'function') {
    window.parent.openSidebarVisibilityModal('system', 'redmine');
  } else {
    window.location.assign('/redmine-agent?settings=1');
  }
}
