# 0012. 后台编排机器授权与 ATS 执行链身份

- 状态: 已接受
- 补充：人类预授权的定时计划与有界重试派生见 [ADR 0020](0020-scheduled-test-plans-and-bounded-retries.md)；其他执行权限边界继续生效。
- 日期: 2026-09-17
- 相关: ADR 0006（Agent Service 边界）、ADR 0010（Actor/ResourceOwner 身份）

## 背景

ATS（Automation）后台 Worker 按阶段推进一条 run：build → 设备预留 → 烧写 →
测试 → 报告 → 分析。改造前它有两个相反的缺陷：

1. **入站过宽**：`POST /api/automation/runs`、`/preflight` 只要求
   `require_authenticated_user`，零 scope 的 Agent Token 也能创建 run；后台
   Worker 随后直接调用 BuildService / `repository.reserve_devices()`，
   Feature API 的权限边界被 orchestration service 绕开。
2. **执行链断裂**：`HttpAutomationExecutor` 用裸 `requests.Session()` 回环调用
   `POST /api/cluster/firmware/stage`（要求 elevated admin）、
   `/api/test/start`、`/api/cluster/jobs/{id}`、`/api/reports/list|analyze`、
   `/api/cluster/devices/actions` 等非公开端点。生产强制认证下这些调用得到
   401——预检全绿、跑到烧写/测试阶段才失败。

简单给 Worker 一个 admin cookie/token 是错误解法：那会让零 scope Agent →
创建 ATS → privileged Worker → Build/Device/Flash/Test 形成完整权限提升链。

## 决策

1. **入站 human-only**。ATS 的 create/preflight/cancel/retry 端点改用
   `require_human_principal_when_auth_required`（Agent 与机器 principal 一律
   拒绝）。机器能力只用于阶段执行，不允许机器创建/派生新的 run。
2. **能力快照**。创建 run 时按计划编译能力并集（`automation_granted_capabilities`）：
   - build 阶段 → `build.execute` / `build.cancel` / `build.read`
   - 设备预留 → `devices.lease` / `devices.read` / `devices.use_leased`
   - 测试阶段 → `tests.execute` / `tests.cancel`
   - 烧写 → `firmware.stage`（显式能力，独立于 admin elevation）
   创建者必须**已经持有**每个请求的能力（或会话已通过管理员重认证提权，
   见下），并集存入 run 的 `granted_capabilities_json`。之后角色/scope
   变化不会隐式扩大在途 run 的能力。
   **提权即同意**：`POST /runs` 与 `/runs/preflight` 对通过管理员重认证
   （`is_elevated`）的人类会话视为同意全部计划能力——这与固件工作台
   `/api/cluster/firmware/stage` 对人类要求的门槛完全一致（工作台烧写
   只要求提权，不要求角色持有 `firmware.stage`）。编译器只对人类 cookie
   会话应用该豁免：Agent token 与机器 principal 是 Bearer 身份，提权是
   人类会话属性（ADR 0010），永不放大 Bearer 授权。
3. **机器 principal（不模拟浏览器）**。Worker 为每条 run 构造
   `automation:<run_id>` principal（`automation_authority`）：
   `resource_owner_id = created_by`（账号 id，ADR 0010），能力 = 能力快照 +
   固定执行底线（`jobs.read` / `reports.read`）。owner-scoped API 因此恰好
   解析到创建者自己的资源，不多不少。
4. **短 TTL 签名凭证**。回环 HTTP 调用携带 `mint_capability_token` 签发的
   `gmscap_v1_` Bearer（HMAC-SHA256、2 小时 TTL、仅进程内可铸造——没有任何
   HTTP 端点接受铸造请求）。`get_authenticated_user` 按前缀识别，校验失败的
   capability 与无效 Agent token 一样 fail-closed。
5. **服务端点按能力接受机器身份**。设备 actions、cluster job 读取、
   `/api/test/start`、reports list/analyze 等 owner/scope 门继续生效；烧写
   stage 端点用 `machine_has_permission(request, "firmware.stage")` 放行机器
   principal，人类会话仍要求原 elevated admin 二次验证。

## 后果

- 零 scope Agent 无法再借 ATS 越过 Build/Device/Test 权限边界。
- ATS 在生产认证模式下全链路可执行，无需 admin 凭据、不留长期 token。
- orchestration service 不能自动获得比发起者更多的能力（本 ADR 的核心不变式）。
- run 表新增 `granted_capabilities` 列；旧 run 无该列时按能力快照为空处理
  （仅剩固定执行底线，阶段门会如实失败而不是静默越权）。

## 测试

- `features/automation/tests/test_api.py`：Agent token 创建/preflight/cancel/
  retry 全部 403；能力不足的创建者按缺失能力拒绝。
- `features/auth/tests/test_authority.py`：能力快照编译、机器 principal 归属、
  capability token 签名/过期/防篡改。
- `features/automation/tests/test_integrations.py`：executor 回环请求带
  Authorization 能力头；owner-scoped 端点（reports/jobs）按创建者账号解析。
