# 平台安全模型总览

> 本文档面向平台维护者、部署者与审计者，自顶向下说明 GMS Remote Test
> Controller 侧的认证、授权、执行边界与秘密管理。Agent 客户端侧的凭据纪律
> 见 [docs/agent/security-model.md](agent/security-model.md)；生产部署的必填
> 配置见 [docs/deployment/production.md](deployment/production.md)。

## 认证：人类 Cookie 会话 vs Agent Service Token 双轨

平台上有两条互不混用的认证轨道，服务端按凭据类型判定 principal（见
`features/auth/access.py` 的 `get_authenticated_user`）：

- **人类会话（cookie）**：浏览器/CLI 登录后在 `AUTH_COOKIE_NAME` cookie 上
  建立会话。`/api/auth/login`、`/api/auth/setup`、`/api/auth/logout`、
  `/api/auth/elevate` 是公开的认证入口边界（见 `tests/architecture/test_route_authorization.py`
  的 allowlist 注释）。参数形式为 `request.state.auth_method == "session"`。
- **Agent Service Token（Bearer）**：Agent/CLI 在 `Authorization: Bearer <token>`
  头携带服务令牌。principal 角色为 `agent_service`，其权力完全来自 scopes
  （见 [AGENT_SCOPES](../features/auth/constants.py)），不是角色阶梯的一级。
  **无效/未知的 Bearer 令牌 fail-closed**：`get_authenticated_user` 置
  `credentials_rejected` 并返回 `None`，绝不回退到 cookie 认证或 dev 匿名，
  由 `require_authenticated_user` 直接 401。

此外还有第三条面向 **Worker** 的机器凭据：Worker 使用按 worker id 独立的
bearer token（`features/cluster/worker_tokens`，配置 `configs/worker_tokens.json`），
生产模式下强制存在（见下「生产启动自检」）。Worker 与 Controller 只通过带
Worker Token 的 HTTP(S) 通信，设备所在主机无需开放入站执行通道
（[ADR-0001](architecture/adr/0001-controller-worker-boundary.md)）。

### Agent 注册与令牌生命周期

1. 管理员在 Web UI 铸造**一次性 enrollment code**（
   `POST /api/auth/agent-enrollment-codes`，TTL 5 分钟、单次使用）。
2. Agent 运行 `gms-rt-agent-enroll <CODE>`，在 `POST /api/auth/agent-enroll`
   用 code 兑换 Service Token。该端点是设计上的**配对码兑换边界**：它接受
   一次性 code 而**不**要求会话（构建服务器此时尚无会话），scopes/ACL/过期
   全部来自服务端的 enrollment 记录，因此泄露的 code 不授予超出管理员批准
   范围的权限；未知/已用/过期 code 一律 403（fail-closed），并按来源 IP
   限流防暴力（`features/auth/agent_api.py`）。
3. 服务端只保存 token 的 SHA-256 哈希；原始 token 仅在创建/兑换时返回一次；
   默认有效期 90 天（`DEFAULT_AGENT_TOKEN_DAYS`），可按 token/profile 粒度吊销。
4. 落盘：`~/.local/state/gms-remote-test/<profile>.token`（权限 `0600`），
   与 profile 一一对应，经 `GMS_AUTH_TOKEN_FILE` 注入运行时。

## Scope / Elevation 模型

授权依赖集中在 `features/auth/access.py`，权限词表在
`features/auth/constants.py` 的 `ROLE_PERMISSIONS`：

- 人类角色：`user`、`device_operator`、`admin`（`admin` 为 `*`）。
- 机器角色：`worker_service`（`worker.register` / `worker.heartbeat` /
  `worker.commands` / `worker.artifacts`）与 `agent_service`（空集合，权力来自 scopes）。
- 常用依赖：`require_permission`、`require_role`、`require_elevated_admin`、
  `require_authenticated_user`、`require_resource_owner`、`require_agent_scope`。
  `*_when_auth_required` 变体在非强制认证（内部/dev）部署下保留匿名可用性。

**Elevation（管理员二次提权）**是挂在人类 cookie 会话上的 step-up，**不改变
账户角色**，仅在该会话上记录 `elevated_until`。`require_permission` /
`require_role("admin")` 认可活跃 elevation；`require_elevated_admin` 要求它。

**Agent 永远不可提权**：`is_elevated` 对 `auth_method == "agent_token"` 直接返回
`False`——避免 agent 令牌继承同请求里残留的浏览器 cookie elevation、击穿 agent
的 scope 隔离。需要提权的高风险操作只能由人在自身会话中完成，或签发一次性
审批令牌（见下）。人类专属界面（VPN、SSH helper、suite 管理等）用
`require_human_principal_when_auth_required` 在服务端拒绝 agent 令牌——
MCP 工具白名单不是安全边界，agent 令牌仍可直连 REST。

**路由授权门禁**：`tests/architecture/test_route_authorization.py` 要求
`features/` 下每个 state-changing（POST/PUT/PATCH/DELETE）`/api` 路由要么绑定
`Depends(require_*)`、要么在 handler 内调用权限 helper，否则必须登记到
`MIGRATION_ALLOWLIST`（当前上限 7 条，且**只减不增**）。allowlist 现存条目
是人工认证入口、worker 心跳/注册/命令（per-worker bearer）、Gerrit 签名
webhook 与 `/agent-enroll` 配对码边界等有明确理由的例外。

## 设备 / Worker ACL

Agent Service Token 携带 `allowed_workers` 与 `allowed_devices` 两个 ACL 字段：
`*` 表示不限制，否则为显式白名单（`features/auth/agent_tokens.py`
的 `agent_acl_allows`）。服务端在 `ensure_agent_worker_allowed` /
`ensure_agent_device_allowed` 中按 ACL 过滤：即使 scopes 允许，越界的
worker/设备也不可达。烧录审批消费时同样套用设备 ACL（`agent_api.py`
的 `/approval-tokens/consume`），使仅限特定设备的令牌无法被驱动到别的设备。

物理设备占用由 Controller 的 Device Claim / Lease 保证同一设备不被并发使用
（[ADR-0001](architecture/adr/0001-controller-worker-boundary.md)）；Worker
超时未心跳时 Controller 回收其 Job 与 Device Claim。

## Approval Token（一次性审批令牌）

破坏性/高风险操作（任意设备命令 `gms_rt_shell_exec`、固件烧录
`gms_rt_burn_firmware`）必须携带服务端签发的一次性 approval token
（`features/auth/approval_tokens.py`）：

- **只能由人类会话签发**（cookie 登录，`POST /api/auth/approval-tokens`；
  CLI：`gms-rt-approval-create`）。Agent Service Token 不能自批。
- **精确绑定**：shell 审批绑定 tool + device + SHA256(command)；烧录审批绑定
  完整 operation——规范化设备列表 + 固件 SHA-256 + `wipe_data` + `burn_mode`，
  命令串只由服务端 `derive_burn_command` 从这些字段派生，调用方无法用为
  固件 A 签发的令牌烧固件 B。
- **单次使用 + TTL**：默认 300 秒（`APPROVAL_TOKEN_TTL_SECONDS`，创建时夹取到
  30–600 秒），执行前由 `POST /api/auth/approval-tokens/consume` 校验并消费；
  过期或已用记录在创建时被清理。
- 烧录审批额外要求签发会话处于**活跃的管理员提权**状态。

## SSH 执行边界

**唯一 SSH 执行层**是 `foundation/ssh_executor.py`：业务代码禁止直接调用
`ssh.exec_command()`，命令统一经 `SSHExecutor` / `SSHManager.execute_command`
执行，结果是统一的 `CommandResult`。执行器对 stdout/stderr 双流并发 drain
（先 drain 再 `recv_exit_status`），修复了「大输出下 channel 窗口互锁」的
历史死锁，并带超时、输出截断上限与协作式取消。

**唯一登记的例外**是 `features/system/ssh.py` 的连接健康检查：它以 raw
channel 轮询 `exit_status_ready` 按 deadline 判定连接存活，因为 paramiko 的
`recv_exit_status()` 是不带超时的 `status_event.wait()`，channel `settimeout`
约束不到它（见该模块 155–162 行注释与
[ADR-0004](architecture/adr/0004-ssh-execution-boundary.md)）。

该边界由 `tests/architecture/test_no_regression_rules.py` 静态守护：扫描
`features/`、`foundation/`、`worker_agent/`、`workflows/`、`bootstrap/` 中的
`.exec_command(`，白名单外直接失败。

SSH 连接统一使用严格 Host Key 校验（`foundation/ssh_security.py` 的
`configure_strict_host_keys`，known_hosts 路径可经 `GMS_SSH_KNOWN_HOSTS` 指定）；
生产环境不得关闭 Host Key 检查来「解决」首次连接问题。

## shell=True 架构限制

`shell=True` / `os.system` / `os.popen` **只允许**出现在两个经审计的边界内：

- `foundation/processes.py` — 受控本地进程边界，提供参数数组形式的
  `run_local_command`（首选）与确需 Pipeline/Redirect 时的
  `run_local_shell_command`；
- `features/build/executor.py` — 构建服务器执行器（Build Template 本质是受控
  远程代码执行）。声明为 `trusted_shell_fragment` 的完整命令片段必须同时通过
  `pattern`（以 **fullmatch** 校验，前缀匹配不会放行 `cmd ; malicious`）或
  `choices` 白名单；其余参数一律 shell quote 后再渲染；整数参数走
  `integer` 校验（含 min/max）；`workspace` 必须落在 `workspace_root` 之内。

新增本地命令执行优先使用 argv 数组；只有确实需要 shell 语义的场景才使用受控
边界。该限制由 `tests/architecture/test_shell_execution_boundary.py` 以 AST
静态守护，新出现的 `shell=True` 会直接失败。

## 上传 / 路径白名单

- `foundation/uploads.py` 是上传路径的唯一收敛点：`normalize_upload_relative_path`
  拒绝 `.`/`..`、`../` 前缀、绝对路径并统一反斜杠；`safe_upload_target_path`
  用 `os.path.commonpath` 复核目标仍在 base_dir 内；`safe_upload_token` 把不可信
  的 upload/session id 重写成路径安全 token，防止形如 `../../x` 的 id 逃逸
  上传根。上传临时根优先 `GMS_UPLOAD_DIR`，否则 `data_root/uploads`，否则 OS 临时目录。
- 报告压缩包处理在 `features/reports/archive.py` 有前后双重防护：解压前
  `_preflight_system_archive` 校验声明路径无穿越、声明展开大小不超限；解压后
  `_enforce_post_extraction_safety` 拒绝符号链接/特殊文件（FIFO 等）并限制文件
  数量。上限常量在 `foundation/archives.py`：`MAX_ARCHIVE_FILES = 10_000`、
  `MAX_ARCHIVE_EXPANDED_BYTES = 2 GiB`（回归见
  `tests/architecture/test_security_hardening.py`）。

## 秘密管理

**`*_KEY_FILE` 语义是「文件路径」，不是秘密内容本身**——把秘密留在 0600 文件里，
不写进环境变量或仓库。

- `GMS_SECRET_KEY_FILE`：指向 Fernet 主密钥文件（默认
  `data_root/secrets/master.key`）。`GMS_SECRET_KEY` 可作为内联替代注入。密钥文件
  权限必须为 `0600`（含 group/other 位即抛 `RuntimeError`）；文件缺失时自举一个
  部署本地密钥（生产模式明确告警：旧密钥加密的存量密文将无法解密），文件存在但
  损坏/权限错误则硬失败（`foundation/secrets.py`）。`encrypt_secret`/`decrypt_secret`
  用于固件共享密码等落库加密（`password_encrypted` 字段；公开记录不含明文/密文，
  仅 `has_password`）。
- `GMS_SKILL_SIGNING_KEY_FILE`：指向 Ed25519 签名校验密钥，生产模式下必须配置
  （Agent 包分发走向签名信任链，不允许静默降级为仅 SHA 校验）。
- **Release 包不携带秘密**：`make agent-check` 内含
  `scripts/check_source_secrets.py .` 扫描，`scripts/sanitize_release_config.py` /
  `sanitize_tracked_config.py` 负责发布前的配置脱敏。

### 生产启动自检（fail-closed）

`bootstrap/production_security.py` 在 `GMS_ENV=production` 时于接收流量前校验
全部控制面秘密，任一不满足即拒绝启动：`GMS_AUTH_REQUIRED` 与
`GMS_SECURE_COOKIES` 不得关闭；`GMS_BOOTSTRAP_TOKEN` 需 ≥32 字符（保护首次
管理员初始化）；`GMS_METRICS_TOKEN`、`GMS_AUTOMATION_WEBHOOK_TOKEN`（均
≥32 字符）与 `GMS_AUTOMATION_OWNER_ID` 必填；每个 worker token 必填且 ≥32 字符；
`GMS_ALLOWED_ORIGINS` 必须是精确的 HTTPS origin（scheme 为 `https`、无 path）；
主密钥、安全审计与 Agent 包签名密钥均通过校验。

## TLS

Agent 运行时通过 `GMS_CURL_CA_CERT` 指定信任的 CA bundle，用以校验 Controller
的自签名/私有 CA 证书（`agent/gms-remote-test/runtime/gms_agent/client.py`
与 `mcp_launcher.py`）。`GMS_CURL_INSECURE=1` 会跳过校验，**仅限一次性/临时
环境**使用，不得进入常规或生产部署。生产模式下 Controller 要求 HTTPS
（`GMS_ALLOWED_ORIGINS` 与 Worker 的 Controller URL 均强制 HTTPS）。

## 数据面加固

- Worker SQLite 连接启用 `journal_mode=WAL`、`busy_timeout=30000`、
  `foreign_keys=ON`（`worker_agent/runtime.py`）；`foundation/database.py`
  的 `connect_sqlite` 同样启用 WAL 与 busy_timeout。
- 构建命令注入防护、压缩包安全与固件密码静态加密的回归集中在
  `tests/architecture/test_security_hardening.py`。

## 相关文档

- [docs/agent/security-model.md](agent/security-model.md) — Agent 侧凭据与 MCP 安全门
- [architecture/adr/0001-controller-worker-boundary.md](architecture/adr/0001-controller-worker-boundary.md) — Controller/Worker 边界与机器凭据
- [architecture/adr/0003-agent-profile-store.md](architecture/adr/0003-agent-profile-store.md) — profile 与 token 存储
- [architecture/adr/0004-ssh-execution-boundary.md](architecture/adr/0004-ssh-execution-boundary.md) — SSH 执行与 shell 白名单决策
- [docs/deployment/production.md](deployment/production.md) — 生产必填配置
- [docs/deployment/configuration.md](deployment/configuration.md) — 配置项与环境变量
