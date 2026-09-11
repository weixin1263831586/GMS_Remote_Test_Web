# Agent 常见问题排查（平台管理员 / 部署者视角）

> 本文档面向平台管理员与部署者，汇总 Agent 部署与接入阶段的常见故障。
> 先用 `gms-agent doctor --json` 获取无凭据的本地一致性报告（见
> [installation.md](installation.md)），再对照本页处置。Agent 使用阶段的
> 设备/测试类问题见
> [../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md](../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md)。

## Enrollment 失败

**现象**：`gms-agent enroll <CODE>` 报配对码无效/已使用/已过期，或兑换
返回 429。

**排查**：

- 配对码是**一次性**且 **5 分钟 TTL**：过期即作废，被兑换过即删除。
  重新由管理员在 Web UI 铸造一个新的。
- 兑换端点按来源 IP 限速；若返回 429，按 `Retry-After` 等待后重试，或
  确认未在短时间内高频重试同一码。
- 生成配对码需要**管理员提权会话**；若管理员未提权，铸造会失败——先在
  Web 端完成提权再生成。
- 生成时指定的 scopes 决定后续能力；若兑换成功但后续命令报 scope 不足，
  属权限配置问题而非 enrollment 失败，需重新铸造带正确 scopes 的码。

**验证**：`gms-agent doctor --json` 中 `clients[].token.present` 应为
`true`。

## TLS 证书问题

**现象**：安装或运行时 curl 报证书校验失败（self-signed / unknown CA）。

**排查**：

- 生产部署应显式信任 Controller CA：
  `export GMS_INSTALL_CA_CERT=/path/to/controller-ca.crt`，安装器与
  bootstrap 均使用它。
- profile 里记录的是 `[controller] ca_cert`；`gms-agent doctor --json` 的
  `clients[].profile.ca_configured` 为 true 时 `ca_present` 必须为 true，
  否则 `actions` 会提示恢复 CA 文件。
- 运行期用 `GMS_CURL_CA_CERT` 指向 CA；仅在受控自签名环境使用
  `GMS_CURL_INSECURE=1`（profile `insecure = true` 会映射它）——不要在
  公网或不可信网络关闭校验。
- 自签名环境安装时可使用 installer 支持的 insecure bootstrap
  （`GMS_INSTALL_INSECURE=1`）；该策略会被写入 profile 并在后续
  update/rollback 重新激活时保持（sticky）。

## Profile 歧义

**现象**：MCP 启动失败，stderr 提示 `multiple <client> profiles found
(names); set GMS_RT_PROFILE or GMS_AGENT_PROFILE explicitly`；或工具调用
一律失败。

**原因**：同一 Agent 主机接入多个 Controller 时，每个 client 有多个
profile，fail-closed 选择契约拒绝猜测（绝不按文件名排序取第一个）。

**处置**（详见 [profiles.md](profiles.md)）：

- 在 MCP 注册的 env block 中显式声明 `GMS_RT_PROFILE`（或
  `GMS_AGENT_PROFILE`）指向目标 profile；`$GMS_AGENT_PROFILE` /
  `$GMS_RT_PROFILE` 加载的具名 profile 是权威的。
- 用 `gms-agent profile list` 查看候选，`gms-agent profile use <name>` 绑定，
  `gms-agent profile show <name>` 检视单个 profile（无需打印凭据）。
- 自动化脚本必须经环境变量/`--profile` 明确路由，不能依赖隐式选择。

**注意**：若提示某个 profile “belongs to <X>, not <Y>”，是 client 不匹配
——具名 profile 的 `client =` 字段与请求的 client 不符，应指向正确的 profile。

## MCP 未注册

**现象**：client 内看不到 `gms_rt_*` 工具；`gms-agent doctor --json` 中
`clients[].mcp.registered` 为 `false`。

**排查**：

- 重新激活/注册：`gms-agent install --client <client>`（或 update 后整包
  重新激活）；排查注册标记——
  kimi 检查 `~/.kimi-code/mcp.json` 的 `mcpServers.gms`，
  kkagent 检查 `~/.kkagent/config.toml` 的 `[mcp_servers.gms]`，
  codex 检查 `~/.codex/config.toml` 的 `[mcp_servers.gms_remote_test]`；
  三者均要求 args 指向 `mcp_launcher.py`。
- **codex 原生插件**：其 MCP 注册由插件 manifest 拥有，不期望独立的
  `[mcp_servers.*]` 块；doctor 会识别已启用的
  `[plugins."gms-remote-test@<channel>"]`（`enabled = true`）条目。
- 修改注册后需重启对应 client，让新的 MCP Server 进程拉起。
- 若 MCP Server 启动即退出（exit 2），通常是 profile/client 未声明——
  见上一节「Profile 歧义」。

## 验收清单

```bash
gms-agent doctor --json         # ok=true 且 clients[].* 全部合格
gms-rt-system-selfcheck --json  # Controller 连通性、认证、设备清单
```

doctor 的 `actions` 数组会逐条给出修复动作，优先按它处置。

## 相关文档

- [docs/agent/installation.md](installation.md) — 安装与 doctor 字段含义
- [docs/agent/profiles.md](profiles.md) — profile 选择契约
- [docs/agent/security-model.md](security-model.md) — 凭据与权限
- [../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md](../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md) — 使用阶段运维手册
