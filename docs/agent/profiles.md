# Agent Profile 存储与 Fail-closed 选择（平台管理员 / 部署者视角）

> 本文档面向平台管理员、部署者与仓库维护者，说明 profile 的存储布局、
> 环境变量与多 Controller 主机上的 fail-closed 选择契约。决策背景见
> [docs/architecture/adr/0003-agent-profile-store.md](../architecture/adr/0003-agent-profile-store.md)。
> Agent 使用层面的 profile 排障见 [troubleshooting.md](troubleshooting.md)。

## 单一真源

`agent/gms-remote-test/runtime/gms_agent/profile_store.py` 是 profile 存储的
**唯一实现**：MCP launcher（`mcp_launcher.py`）、包生命周期
（`gms_agent/package_manager.py`）、SDK 与 doctor 全部只经由它读写 profile。
早期 launcher 与 package_manager 各自维护 glob + TOML 解析器的实现已收口，
legacy `<client>.env` 文件已删除——profile 本体只使用 TOML。

## 存储布局

```text
~/.config/gms-agent/profiles/<profile>.toml          profile 本体（0600，数据专用 TOML）
~/.local/state/gms-remote-test/<profile>.token       该 profile 的 Service Token（0600）
```

profile 名必须匹配 `^[A-Za-z0-9_.-]+$`（单一路径段）。`gms-agent install`
的默认 profile 名包含 Controller 身份：
`<client>-<host>-<sha256(server)[:8]>`（如 `codex-build01-3f2a9c1d`）——
同一台主机上同一 client 的不同 Controller 各占一个 profile 文件，不会
相互覆盖；需要可读名字时用 `gms-agent install --profile NAME` 显式指定
（如 `gms-prod` / `gms-dev`）。TOML 为固定形状
（顶层 `profile` / `client` + `[controller]` 的 `url` / `ca_cert` /
可选 `insecure` + `[auth]` 的 `mode = "service-token"` / `token_file`），
写入走 `profile_store.write_profile_toml()`（temp-file 语义外的 0600 保证、
转义都在这一处）。部署与升级工具（`gms-agent` enroll / update / rollback）
必须经 profile_store 写入，不允许绕过它直接写文件。

两个根目录均尊重 XDG：`XDG_CONFIG_HOME`、`XDG_STATE_HOME`。

## Fail-closed 选择契约

`resolve_profile(client)` 的规则（绝不允许 `sorted()` 取第一个——那会静默
把 Agent 路由到错误的 Controller）：

| profile 数量 | 行为 |
| --- | --- |
| 0 | 返回 `None`：不猜测、不使用默认；launcher 报错并给出修复指引 |
| 1 | 直接使用该 profile |
| >1 | 返回 `None`：调用方必须显式指定 profile，未指定则失败并列出候选 |

列举类操作（`list_profiles()` / 按 client 前缀的 `profile_candidates()`）
只做展示与校验，不做隐式选择。`gms-agent profile list|show|use` 是人工
检视与绑定的入口；多 Controller 主机上未显式指定的调用会失败并提示可用
profile 列表——这是有意为之的失败（fail-closed），把歧义交给人决策。

### 生命周期命令的 Controller 解析（fail-closed）

`gms-agent enroll / update / rollback` 用同一条规则确定目标 Controller
（`package_manager.resolve_controller()`），优先级从高到低：

1. 显式 `--server URL`；
2. 环境 `GMS_REMOTE_TEST_SERVER`（或 bootstrap 内嵌 URL）；
3. 显式 `--profile NAME` → 该 profile 记录的 Controller；
4. 全部 profile 恰好指向**唯一** Controller → 自动使用。

0 个或多个不同 Controller → 报错退出（exit 2），要求 `--server` /
`--profile`。旧实现的「按 client 逐个找第一个可用 profile」回退已删除：
codex 侧歧义不允许静默掉到 kimi 的 Controller。

`gms-agent enroll` 在歧义主机上必须 `--profile`（one-shot 配对码只兑换给
明确的 Controller）；唯一 Controller 时 token 写入所有指向它的 profile
（按 TOML 实际内容枚举，手工命名的 profile 同样覆盖）。

## 环境变量与选择顺序

MCP launcher（`mcp_launcher.py`）按以下顺序确定 profile：

1. `$GMS_AGENT_PROFILE` / `$GMS_RT_PROFILE` → 加载具名 profile；
   其 TOML 内的 `client` 字段决定 client（具名 profile 是权威的，即使
   `GMS_AGENT_CLIENT` 也在场也不会被忽略）。
2. 仅 `$GMS_AGENT_CLIENT`（由插件 manifest env block 设置）→ 对该 client
   执行上述 fail-closed 选择。
3. 两者皆无 → 启动失败（exit 2），提示在 MCP 注册 env block 中声明；
   仅当环境里同时提供了 `GMS_REMOTE_TEST_SERVER` 与 `GMS_AUTH_TOKEN_FILE`
   的纯环境注册才放行。

变量速查：

| 变量 | 作用 |
| --- | --- |
| `GMS_AGENT_CLIENT` | 目标 client（`codex` / `kimi` / `kkagent`），由 manifest env block 设置 |
| `GMS_RT_PROFILE` | 显式 profile 名（最高优先级之一，doctor 也识别） |
| `GMS_AGENT_PROFILE` | 显式 profile 名（与 `GMS_RT_PROFILE` 等价的另一入口） |

launcher 加载 profile 后映射出运行时环境：`GMS_REMOTE_TEST_SERVER`、
`GMS_CURL_CA_CERT`（profile `insecure = true` 时映射 `GMS_CURL_INSECURE=1`）、
`GMS_AUTH_TOKEN_FILE`，并**强制** `GMS_AGENT_AUTH_MODE=service-token` 与
`GMS_AGENT_PROCESS=1`（后者是 MCP Server 判定自身处于 Agent 进程的独立
信号，防止伪造 auth-mode 环境变量重新启用密码工具）。

## 相关文档

- [docs/agent/installation.md](installation.md) — profile 的写入时机（install/enroll）
- [docs/agent/security-model.md](security-model.md) — token 与 profile 的对应
- [docs/agent/troubleshooting.md](troubleshooting.md) — profile 歧义处理
- [docs/architecture/adr/0003-agent-profile-store.md](../architecture/adr/0003-agent-profile-store.md) — 决策记录
