# Agent Runtime 概览（平台管理员 / 部署者视角）

> 本文档面向 GMS Remote Test 平台的管理员、部署者与仓库维护者，说明 Agent Runtime
> 的定位、源码到分发产物的生成链，以及多 Controller 接入的整体结构。
> 安装后如何**使用** Agent（日常命令、MCP 工具、运维手册），见
> [../../agent/gms-remote-test/docs/README.md](../../agent/gms-remote-test/docs/README.md)
> 与 [../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md](../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md)。

## 定位

Agent Runtime 是面向 Codex、Kimi、kkagent 等 MCP Client 的统一 Agent Package，
部署在编译服务器等共享主机上，通过 Agent Service Token 访问 Controller 提供的
设备、测试、报告与证据能力。它包含：

```text
gms-rt CLI（gms-remote-test.sh，shell CLI 使用 gms-rt-* 连字符命令名）
MCP Server / Launcher（mcp_server.py / mcp_launcher.py，MCP 工具名为 gms_rt_*）
gms-agent 安装与升级 CLI
Python SDK（gms_agent/）
SKILL.md / references / agent metadata
Codex / Kimi / kkagent 插件 manifest
Package tests
```

Agent 与 Worker Agent 的边界：Agent 通过 Controller 的 HTTP API 驱动测试与取证，
不直接执行 Tradefed；实际测试执行节点见 README 的「Worker Agent」一节。

## 生成链：canonical source → sync → generated plugin → dist

`agent/gms-remote-test/` 是唯一的手工维护源码根（canonical source），
`agent/gms-remote-test/package.yaml` 是唯一版本源。任何修改都只发生在源码根：

```text
agent/gms-remote-test/            手工维护的源码根（CLI、MCP adapter、SDK、Skill、manifest）
        │  python tools/sync_agent_package.py
        ▼
plugins/gms-remote-test/          生成结果，禁止手改
        │  python tools/build_agent_package.py
        ▼
dist 分发包（由 features/system/agent_package_builder.py 与该工具产出，
            发布走 python tools/release_agent.py --version X.Y.Z）
```

规则：

- **只改源码根** `agent/gms-remote-test/`，然后运行
  `python tools/sync_agent_package.py` 重新生成 `plugins/gms-remote-test/`。
  直接手改 `plugins/` 会导致与源码树漂移，CI 的 Agent Package Gate 会校验
  生成树、版本契约和打包结果与源码同步，`tools/release_agent.py --check`
  可在本地做同样的检查。
- 版本提升走 `python tools/release_agent.py --version X.Y.Z`（会改写所有
  版本声明并重新 sync）；`tools/audit_gms_agent_contract.py` 强制六方版本契约。
- 分发包布局（canonical DISTRIBUTION layout）：`runtime/ → scripts/`、
  `skill/ → skills/gms-remote-test/`、`manifests/* → 插件根 manifest`。

## 部署拓扑与多 Controller

- 一台编译服务器上的 Agent Runtime 可同时接入多个 Controller：每个
  client×Controller 组合对应一个 profile（见 [profiles.md](profiles.md)）。
  多 profile 主机的选择是 fail-closed 的，绝不按文件名排序取第一个。
- Agent 以 Agent Service Token（`GMS_AUTH_TOKEN_FILE` 指向 0600 文件）认证，
  不持有、也不应接收平台用户密码；安全模型见 [security-model.md](security-model.md)。
- 平台侧开放与安装流程见 [installation.md](installation.md)；分发包的完整性
  （SHA-256 + Ed25519 manifest 签名、同源下载、禁重定向、安全解包）由
  bootstrap 与 `GMS_SKILL_SIGNING_KEY_FILE` 配置的签名密钥保证，详见 README
  「包完整性与更新链」一节。

## 相关文档

- [docs/agent/installation.md](installation.md) — 平台侧开放与安装验收
- [docs/agent/security-model.md](security-model.md) — Service Token、scope、审批令牌
- [docs/agent/profiles.md](profiles.md) — profile 存储与 fail-closed 选择
- [docs/agent/troubleshooting.md](troubleshooting.md) — 常见问题
- [../../agent/gms-remote-test/docs/README.md](../../agent/gms-remote-test/docs/README.md) — 安装后使用文档
- [../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md](../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md) — Agent 运维手册
- [docs/architecture/adr/0003-agent-profile-store.md](../architecture/adr/0003-agent-profile-store.md) — Profile Store 决策记录
- 根 README「Agent Runtime 与 MCP」「包完整性与更新链」两节
