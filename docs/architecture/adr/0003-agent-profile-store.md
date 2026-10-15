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
6. **Direct CLI 等价折叠**：Codex/Kimi/kkagent 由同一次安装和 enrollment
   产生的 profiles，若 Controller URL、CA/insecure 策略以及 token 内容完全
   相同，裸 `gms-rt-*` 可将它们折叠为 profile-neutral 的 `direct` 上下文。
   这不代表按顺序选择某个 profile；任一 Controller、TLS 策略或 token 不同
   都必须 fail closed。`GMS_RT_HUMAN_SESSION=1` 可在唯一 Controller/TLS
   上显式改用独立的人类 cookie 会话。
7. **交互终端显式选择**：人工 TTY 下允许展示编号菜单，由用户选择确切
   profile，再交给同一个 profile_store 校验。`gms-agent profile env` 只输出
   引用安全的 `export GMS_RT_PROFILE=...`，供当前终端显式复用，不写默认
   profile、不修改 shell 启动文件或其他客户端注册。MCP、JSON 和非交互
   执行仍不弹菜单、不猜选。帮助、版本、命令目录不解析业务凭据；更新
   只走包生命周期的 Controller/TLS/完整性校验，不要求业务 Token。
8. **人工命令按用途解析凭据**：交互终端中的登录、登出、提权和审批
   命令直接解析 Controller/TLS，使用独立人工 Cookie，不先选择 Agent
   身份。多个客户端配置指向相同 Controller/TLS 时，忽略服务 Token 的
   差异；不同 Controller/TLS 仍须人工选择。显式 profile 只为这些人工
   命令提供已绑定的传输配置，不允许发送到其他 Controller。人工 Cookie
   使用 `human-<Controller URL SHA256 前16位>.cookies`，不覆盖 Agent Token。
   审批命令复用有效人工会话，失效时交互登录后继续签发，不执行设备命令。
   JSON、非交互和 MCP 不自动切换人工身份或提示密码；MCP 不继承父终端
   的 `GMS_RT_HUMAN_SESSION`。人工脚本仍须显式选择人工模式。

## 理由

- 多 Controller 环境下，错误的路由意味着把命令发到错误的平台实例（错误的设备、错误的凭据作用域），比直接失败严重得多；fail-closed 把歧义暴露给人来决策。
- 单一实现消除两份 glob / 两份解析器之间的漂移，安全属性（0600、路径推导、TOML 字段）只需在一处维护与测试。
- TOML 相比 `.env` 有类型与结构，能承载 `ca_cert`、`token_file`、URL 等字段且不引入 shell 语义。

## 后果

- 多 profile 主机上，Agent launcher 与自动化脚本仍须通过
  `GMS_RT_PROFILE` / `GMS_AGENT_PROFILE` / `--profile` 明确路由。仅直接
  shell CLI 可使用上述等价折叠；存在任何身份或 Controller 歧义时会失败。
- 部署与升级工具（`gms-agent` enroll / update / rollback）必须经由 profile_store 写入，不能绕过它直接写文件。
- Service Token 与 profile 一一对应（`<profile>.token`），吊销 / 轮换按 profile 粒度进行。
