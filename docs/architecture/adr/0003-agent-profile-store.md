# ADR-0003: Agent Profile Store 唯一实现与 Fail-closed 选择契约

- 状态：已采纳（Accepted）
- 关联代码：`agent/gms-remote-test/runtime/gms_agent/profile_store.py`、`mcp_launcher.py`、`gms_agent/package_manager.py`

## 背景

Agent Runtime 部署在编译服务器等共享主机上时，同一台 Agent 主机可能同时接入多个 Controller（例如不同网段/环境的测试平台）。早期实现中：

- `mcp_launcher.py` 与 `gms_agent/package_manager.py` 各自维护 profile 根目录、TOML 解析与 profile 选择逻辑（两份 glob、两份解析器），行为会漂移；
- 存在 legacy `<client>.env` profile 文件，与 TOML 并存，解析语义不一致；
- 多 profile 场景下的选择规则不明确，曾出现「按文件名排序取第一个」的隐式行为——这会静默把 Agent 路由到错误的 Controller。

## 决策

1. **TOML-only**：profile 本体只使用 TOML 存储，legacy `<client>.env` 已删除。
2. **唯一实现**：`agent/gms-remote-test/runtime/gms_agent/profile_store.py` 是 profile 存储的唯一实现；MCP launcher、package lifecycle、SDK 与 doctor 全部只经由它读写 profile，不允许各自再维护 PROFILE_ROOT / 解析器。
3. **存储布局**（数据专用 TOML，`0600`，无 shell 语义）：
   - `~/.config/gms-agent/profiles/<profile>.toml` — profile 本体；
   - `~/.local/state/gms-remote-test/<profile>.token` — 该 profile 的 Agent Service Token（`0600`）。
4. **Fail-closed 选择契约**（多 Controller 编译服务器的 Agent 路由保证）：
   - 0 个 profile → `resolve_profile()` 返回 `None`（不猜测、不使用默认）；
   - 1 个 profile → 返回它；
   - 多于 1 个 profile → 返回 `None`，调用方必须显式指定（`GMS_RT_PROFILE` / `GMS_AGENT_PROFILE` / `--profile`）。
   - **绝不 `sorted()` 取第一个**——那是静默把 Agent 路由到错误 Controller 的行为。
5. Profile 列举（`list_profiles()` / 按 client 前缀列举）只做展示与校验，不做隐式选择。

## 理由

- 多 Controller 环境下，错误的路由意味着把命令发到错误的平台实例（错误的设备、错误的凭据作用域），比直接失败严重得多；fail-closed 把歧义暴露给人来决策。
- 单一实现消除两份 glob / 两份解析器之间的漂移，安全属性（0600、路径推导、TOML 字段）只需在一处维护与测试。
- TOML 相比 `.env` 有类型与结构，能承载 `ca_cert`、`token_file`、URL 等字段且不引入 shell 语义。

## 后果

- 多 profile 主机上，未显式指定 profile 的调用会失败并提示可用的 profile 列表；自动化脚本需要通过 `GMS_RT_PROFILE` / `GMS_AGENT_PROFILE` / `--profile` 明确路由。
- 部署与升级工具（`gms-agent` enroll / update / rollback）必须经由 profile_store 写入，不能绕过它直接写文件。
- Service Token 与 profile 一一对应（`<profile>.token`），吊销 / 轮换按 profile 粒度进行。
