# ADR-0006: Agent Service 边界 —— Service Token / Approval Token / Enrollment

- 状态：已采纳（Accepted）
- 关联代码：`features/auth/agent_tokens.py`、`features/auth/approval_tokens.py`、`features/auth/agent_api.py`、`features/auth/access.py`、`features/auth/constants.py`、`features/firmware/firmware_api.py`（burn 授权门）、`features/test_execution/execution_api.py`、`features/cluster/device_actions_api.py`、`agent/gms-remote-test/runtime/mcp_server.py`、`tests/architecture/test_review_markers.py`

## 背景

Agent（Codex / Kimi / kkagent）需要操作平台，但绝不能持有人类用户的 Web
登录密码。历史上 Agent 工具链没有独立凭据：要么复用人类会话，要么对
Agent 客户端整体关闭鉴权，无法满足：

- Agent 凭据可独立发放、限定 scope、独立吊销；
- 高危动作（固件烧写、设备 shell 写操作）即使凭据泄漏也无法单独完成，
  需要人类的一次性显式批准；
- 配对过程一次性完成且配对码不可重放。

## 决策

1. **Agent Service Token**：Agent 专用 Bearer 凭据（`role=agent_service`），
   绑定显式 scope 集合；scope 复用既有人类角色的权限模型，Agent 默认无
   任何隐式权限。人类会话 Cookie 对 Agent API 不生效，反之 Agent Token
   不得用于人类会话端点。`foundation` 与 `features/auth` 的 Bearer 解析
   统一走 `features/auth/access.py`。
2. **One-shot Approval Token**：高危操作（`gms_rt_shell_exec`、
   `gms_rt_burn_firmware` 等）要求服务端签发的一次性批准令牌，绑定
   `tool + device + SHA256(command)`、带 TTL、单次消费。客户端自声明
   `authorized=true` 不是安全边界。人类管理员会话仍走原有的提权
   （step-up elevation）路径，不受 Approval Token 影响。
3. **One-shot Enrollment Code**：管理员在 Web UI 生成一次性配对码
   （5 分钟 TTL、单次使用），`gms-agent enroll <CODE>` 用它换取
   Agent Service Token；token 落盘 0600
   （`~/.local/state/gms-remote-test/<profile>.token`）。Agent 从不接触
   Web 登录密码；配对码泄漏窗口被 TTL + 单次消费限制。
4. **Burn 授权门**：`POST /api/burn/firmware` 允许两类主体——持有效
   提权的人类管理员会话，或带一次性 Approval Token 的
   `agent_service` 主体；其余一律 403。
5. **MCP 进程边界**：`mcp_launcher.py` 强制 `GMS_AGENT_AUTH_MODE=service-token`
   并加盖 `GMS_AGENT_PROCESS=1`；MCP server 在无该标记时拒绝注册密码/
   提权类工具。密码类工具只存在于人类登录链路。
6. 本 ADR 是 `tests/architecture/test_review_markers.py` 的引用锚点：
   源码注释中关于 Agent 安全边界的说明应引用本 ADR（`ADR 0006`），不得
   引用仓库中不存在的评审文档编号。

## 理由

- 三类凭据（Service Token / Approval Token / Enrollment Code）各自解决
  一个正交问题：长期身份、单次高危授权、一次性配对；合并任何两个都会
  重新扩大泄漏面。
- scope 模型复用人类权限模型，避免维护第二套权限语义。
- 服务端绑定与单次消费是对「客户端自声明授权」的结构性否定；该错误
  模式在历史上真实出现过，必须由服务端状态而非客户端参数决定授权。

## 后果

- 新增 Agent 可用能力必须显式声明 scope，并在高危场景接入 Approval
  Token 校验；否则架构评审与安全测试（`features/auth/tests/`）不通过。
- Approval Token 校验失败必须在消耗前完成（或失败即作废），实现需保证
  原子性；`features/auth/approval_tokens.py` 是唯一实现。
- Agent 端 profile/token 存储与多 Controller 语义见
  `docs/architecture/adr/0003-agent-profile-store.md`。
