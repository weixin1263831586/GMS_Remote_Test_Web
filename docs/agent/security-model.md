# Agent 安全模型（平台管理员 / 部署者视角）

> 本文档面向平台管理员、部署者与仓库维护者，说明 Agent Service Token、
> scope/elevation、设备 ACL、审批令牌与 MCP 安全门的设计。Agent 安装后的
> 使用层面安全纪律见
> [../../agent/gms-remote-test/docs/README.md](../../agent/gms-remote-test/docs/README.md)
> 的「Security boundary」一节。

## 核心原则

- **Agent 不碰平台登录密码。** Agent 唯一的凭据是 Agent Service Token；
  密码登录（`gms-rt-auth-login`）与管理员提权（`gms-rt-auth-elevate`）属于
  人工 CLI 会话。MCP Server 在 service-token 模式下
  （`GMS_AGENT_AUTH_MODE=service-token`，由安装器/lancer 强制）甚至不注册
  这两个密码类工具。
- **服务端是安全边界。** 不存在也不接受客户端传入的 `authorized=true`
  之类布尔值作为授权依据；越权行为由服务端拒绝。

## Agent Service Token

- 由管理员铸造（直接创建，或经一次性 enrollment code 兑换），默认有效期
  90 天，可随时吊销（按 token / 按 profile 粒度）。
- 客户端只保存 token 的 SHA-256 哈希对应的记录；原始 token 仅在创建/兑换
  时返回一次。
- 落盘：`~/.local/state/gms-remote-test/<profile>.token`，权限 `0600`，
  属主必须是运行 Agent 的用户；profile 与 token 一一对应
  （`<profile>.toml` 的 `[auth] token_file` 指向它），通过
  `GMS_AUTH_TOKEN_FILE` 注入运行时。
- 身份模型：当前为一台主机一个 token、由各 client profile 共享（审计通过
  `GMS_AGENT_CLIENT` 元数据区分 client）；如需隔离可为不同 profile 分别
  enroll。

## Scopes 与 elevation

- Agent principal 的角色是 `agent_service`，其权力**完全**来自 scopes
  （平台权限词表 `AGENT_SCOPES`），不是角色阶梯的一级；基于角色的 admin
  门（`require_role`）永远不匹配 agent principal。可用 scopes 包括：
  `devices.read`、`devices.lease`、`devices.use_leased`、`devices.inventory`、
  `tests.execute`、`tests.cancel`、`jobs.read`、`reports.read`、
  `redmine.read`、`artifacts.read_own`、`apk.analyze_own`、`sdk.read`。
- **elevation（管理员提权）对 Agent 不可达**：提权是人工会话的 step-up
  操作；Agent token 永远无法自审批、自提权（服务端强制）。需要提权的操作
  只能由人在自己的会话里完成或显式签发审批（见下）。

## 设备 / Worker ACL

Agent token 携带 `allowed_workers` 与 `allowed_devices` 两个字段：
`*` 表示不限制，否则为逗号分隔的显式白名单。服务端在执行设备/worker 相关
操作时按 ACL 过滤，即使 scopes 允许，越界的 worker/设备也不可达。
吊销/轮换按 token（即按 profile）粒度进行。

## Approval Token（一次性审批令牌）

破坏性/高风险操作（MCP typed tool `gms_rt_shell_exec` 的任意设备命令、
`gms_rt_burn_firmware` 烧录）必须携带服务端签发的一次性 approval token：

- 只能由**人工会话**（cookie 登录）通过 `POST /api/auth/approval-tokens`
  （CLI：`gms-rt-approval-create`）签发；Agent Service Token 不能自批
  （服务端按认证方式拒绝）。
- 精确绑定：shell 审批绑定 tool + device + SHA256(command)；烧录审批绑定
  固件 SHA-256 + wipe_data + burn_mode + 规范化设备列表。5 分钟 TTL、
  单次使用，执行前由服务端 validate-and-consume。
- 烧录审批额外要求签发会话处于活的**管理员提权**状态。

## Typed MCP tools 与只读 runner

- 通用 runner `gms_rt_run` 只执行 CLI 目录标记为 `agent_safe_unattended`
  的（只读）命令；mutating/提权命令与交互式会话
  （`terminal-open`、`terminal-push`、`devices-scrcpy`）被直接拒绝。
- 高风险操作只有专用 typed tool 通道：`gms_rt_shell_exec`（每次调用都要
  新的 approval token）、`gms_rt_burn_firmware`（approval + 签发会话提权）。
- `gms_rt_logcat` 恒为 dump 模式，拒绝 `-c/-f` 与 shell 元字符——清除日志
  缓冲属于销毁诊断证据，只能由人经 CLI 执行。
- `gms_rt_shell` 的只读 allowlist（`getprop`、`dumpsys` 等）无需额外授权。

## 凭据落盘一览

```text
~/.config/gms-agent/profiles/<profile>.toml          profile（Controller URL、CA、token 路径）0600
~/.local/state/gms-remote-test/<profile>.token       Agent Service Token，0600
~/.local/state/gms-remote-test/session.cookies       人工 CLI 会话 cookie（仅人用）
```

平台侧永不向 Agent 暴露 Web 登录密码；文档与工具链也不应为 Agent 语境
提供、建议或重实现密码登录。

## 相关文档

- [docs/agent/installation.md](installation.md) — enrollment 与 token 落盘流程
- [docs/agent/profiles.md](profiles.md) — profile 与 token 的一一对应
- [../../agent/gms-remote-test/docs/README.md](../../agent/gms-remote-test/docs/README.md) — MCP 层安全门与工具清单
- [../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md](../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md) — 凭据与会话处置
- [docs/architecture/adr/0003-agent-profile-store.md](../architecture/adr/0003-agent-profile-store.md) — profile/token 存储决策
