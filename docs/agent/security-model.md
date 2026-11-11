# Agent 认证与操作边界

平台共用的认证、SSH、上传、审计和 TLS 规则见[安全模型](../security.md)。
本文只说明 Agent 接入时需要额外保持的边界；安装步骤见[安装文档](installation.md)。

## 身份、profile 与权限

Agent 使用 Service Token。服务端保存 Token 哈希及 scopes、Worker/设备 ACL、所有者、
有效期等元数据，原始 Token 仅在签发时返回；客户端保存在
`~/.local/state/gms-remote-test/<profile>.token`，权限 0600。
profile TOML 保存 Controller URL、CA 和 Token 路径，详见 [profile 选择规则](profiles.md)。

一次 enrollment 可以为同一 Controller 的多个 client profile 写入等价凭据；
不同 profile 也可分别 enrollment。吊销针对实际 Token 身份：共享同一 Token 的所有
profile 都会受影响，文件名不同不代表身份隔离。默认 Token 有效期为 90 天，按管理员
铸造时的参数生效。

`agent_service` 权限由 scopes 与 ACL 决定，不能通过角色比较获得 admin 权力。
`allowed_workers` / `allowed_devices` 限定可访问目标；`*` 表示该维度不限制。
设备操作仍需通过设备归属、claim、租约和 fencing 校验。
密码登录、管理员提权和签发操作审批属于人类会话；Agent 无法自提权或自审批。

## Enrollment 与本地验收

管理员在活的提权会话中签发一次性配对码，默认 5 分钟有效，可指定 1–30 分钟。
兑换端点不要求已有登录，但按来源 IP 限速；成功兑换后配对码不可复用。
scopes、ACL 和 Token 有效期由签发时确定，兑换方不能自行扩大。

安装后使用 `gms-agent doctor --json` 检查 profile、Token 权限、MCP 与 Skill，
再用 `gms-rt-system-selfcheck --json` 检查 Controller 连通性和账号能力。
恢复旧认证库或重新建立认证状态时，按实际 Token 是否仍有效决定重新 enrollment。

## 审批与 MCP 安全门

- `gms_rt_run` 只允许目录中标为 `agent_safe_unattended` 的命令；参数必须是字符串数组。
- `gms_rt_shell` 只读白名单；`gms_rt_logcat` 只导出，拒绝清除、写文件和 shell 元字符。
- `gms_rt_shell_exec` 的审批绑定工具、目标和命令哈希；固件审批还绑定对应固件请求。
  审批由人类会话签发，短时、单次使用，客户端布尔值不能代替服务端审批。
- APK 部署和有限应用调试使用各自专用审批流程，见
  [ADR 0017](../architecture/adr/0017-reviewed-apk-deployment.md) 和
  [ADR 0018](../architecture/adr/0018-scoped-debug-sessions.md)。
- 交互终端、scrcpy、原始 shell 和销毁诊断证据的操作保留人工入口。

具体工具参数及审批步骤以包内 Skill 的 `references/agent-workflows.md` 和
`gms_rt_describe` 为准；不要用直连 ADB/SSH 代替平台调用。

## TLS 信任

生产安装先通过可信渠道分发 Controller CA，使用 `--cacert` 和
`GMS_INSTALL_CA_CERT` 从安装脚本开始严格校验；运行期由 profile 加载 CA。
可信实验网络的 TOFU 会在系统不信任 Controller 时获取并保存其 CA，之后严格校验。
TOFU 的首次接触仍依赖可信网络或带外核对；完整安装命令见[安装文档](installation.md)。
