# gms-rt CLI 介绍与设计原则

> 本文档介绍 `gms-rt` shell CLI 的定位与设计原则，并指出完整的命令参考表
> 的权威来源。安装后 Agent 的运维手册见
> [../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md](../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md)。

## 定位

`gms-rt` 是 GMS Remote Test Agent Package 自带的 shell CLI
（源码 `agent/gms-remote-test/runtime/gms-remote-test.sh`），覆盖设备清单、
CTS/GTS/VTS/STS 测试启动与 Durable Job 查询、报告与 Artifact、Redmine
证据、APK/JADX 分析、SDK Source、ADB 只读 Shell/Logcat/Screencap、固件
烧录等 Controller 能力。它与 MCP Server（`gms_rt_*` typed tools）共享同一
套 CLI 目录与安全目录：MCP 的通用 runner `gms_rt_run` 只放行 CLI 标记为
`agent_safe_unattended` 的命令。

## 设计原则

- **JSON envelope**：所有命令支持 `--json`，输出统一的 JSON 信封
  （`ok`、`data`、`diagnostics`）；MCP adapter 会向每个子进程注入
  `--json --non-interactive`，使工具输出稳定可解析。以 `ok` 与退出码为
  权威判断，不要解析进度文本。
- **统一退出码语义**：`0` 成功、`2` 用法错误、`3` 未认证、`4` 权限/需提权、
  `5` 冲突/设备忙、`6` 网络/超时、`7` 操作失败。只有 `6`（网络）适合有界
  自动重试；`4`/`5` 需先检查提权、锁与占用。
- **命名约定**：shell 命令用连字符并带显式动作（`gms-rt-devices-list`），
  MCP 工具用下划线（`gms_rt_devices`）；因此 `gms-rt-devices` 这类裸命令
  不是 CLI 契约的一部分。
- **认证分层**：Agent 用 Service Token（`GMS_AUTH_TOKEN_FILE`，0600）；
  密码登录 `gms-rt-auth-login` 与提权 `gms-rt-auth-elevate` 属于人工会话。
- **Fail-closed profile**：多 Controller 主机上必须经 `GMS_RT_PROFILE` /
  `GMS_AGENT_PROFILE` 显式路由（见
  [../agent/profiles.md](../agent/profiles.md)）。
- **命令自描述**：`gms-rt-system-commands --json` 列出全部命令（含风险
  模式与 agent 安全标记），`gms-rt-system-command-describe <name>` 查看单条
  用法；`gms-rt-system-help` / `gms-rt-system-docs` 提供概览与 API 文档。

## 命令参考

完整的命令表（含用法、风险模式、认证/提权要求）由生成器维护在
[command-reference.md](command-reference.md)，请勿在本页手工复制；
运行期以 `gms-rt-system-commands --json` 的实时输出为准。典型工作流示例见
[examples.md](examples.md)。

## 相关文档

- [docs/cli/examples.md](examples.md) — 典型工作流
- [docs/cli/command-reference.md](command-reference.md) — 生成的命令表
- [docs/agent/overview.md](../agent/overview.md) — Agent Runtime 定位
- [../../agent/gms-remote-test/skill/references/agent-integration.md](../../agent/gms-remote-test/skill/references/agent-integration.md) — CLI↔MCP 映射与执行规则
- [../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md](../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md) — 运维手册
