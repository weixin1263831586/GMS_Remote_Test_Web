# Agent 安装与 Enrollment（平台侧）

> 本文档面向平台管理员与部署者：如何在 Controller 侧开放 Agent 接入、生成
> enrollment code、在编译服务器上执行安装，以及安装后的验收。
> Agent 安装后的日常使用（自检、跑测试、取证）不在本文范围，见
> [../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md](../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md)。

## 前提

- Controller 已部署并可访问（下文以 `https://CONTROLLER:5001` 为例）。
- 操作者拥有平台管理员账号；生成 enrollment code 需要管理员会话
  （服务端要求 `require_elevated_admin`，即活的提权管理员会话）。
- Agent 主机上已存在目标 AI client（codex / kimi / kkagent）之一，或计划只装
  本地 runtime（`--client none`）。

## 第 1 步：生成一次性 Enrollment Code

在 Web UI 中由管理员铸造一次性配对码（enrollment code）：

- **一次性**：兑换成功即从服务端删除，不可重复使用。
- **5 分钟 TTL**：过期作废（服务端 `ENROLLMENT_TTL_MINUTES = 5`）。
- 配套属性在铸造时确定，兑换方无法自选：scopes（权限范围，
  取自平台 `AGENT_SCOPES` 词表，如 `devices.read`、`tests.execute`、
  `jobs.read` 等）。Web UI 铸造页的默认勾选集合（2026-09-11 反馈后）
  为：`devices.read`、`devices.lease`、`devices.use_leased`、
  `tests.execute`、`tests.cancel`、`jobs.read`、`reports.read`，以及
  只读证据/分析链 `redmine.read`、`artifacts.read_own`、
  `apk.analyze_own`、`sdk.read`——即默认能跑通 Skill 文档化的
  Redmine evidence 工作流；`devices.inventory` 等写操作类仍需手动
  勾选。任务开始前可用 `gms-rt-auth-scopes-check` 自检缺口、
  `gms-rt-redmine-credentials-status` 自检凭据。另包括
  `allowed_workers` / `allowed_devices`（worker/设备 ACL，
  `*` 或逗号分隔列表）、`expires_days`（token 有效期，默认 90 天）、
  `ttl_minutes`（配对码 TTL，默认 5 分钟，可 1–30 定制）。
- 兑换端点 `POST /api/auth/agent-enroll` 不要求会话，但按来源 IP 做持久化
  限速，防止对配对码的匿名爆破。

对应的 API 是 `POST /api/auth/agent-enrollment-codes`（管理员会话调用）。

## 第 2 步：在 Agent 主机执行 curl installer

Controller 暴露一行安装器：

```text
GET /api/agent/install.sh
```

它渲染一份绑定当前 Controller 地址的 bash 脚本，将 install + 配对码 enroll
一步完成。生产环境推荐显式信任 Controller CA：

```bash
export GMS_INSTALL_CA_CERT=/path/to/controller-ca.crt
curl -fsSL --cacert "$GMS_INSTALL_CA_CERT" \
  https://CONTROLLER:5001/api/agent/install.sh | bash -s -- <ENROLLMENT_CODE>
```

说明：

- 受控实验环境若使用自签名证书，可按部署策略使用 installer 支持的
  insecure bootstrap（`GMS_INSTALL_INSECURE=1`）；不要在公网或不可信网络中
  关闭 TLS 校验。
- **运行时同样不得用 insecure（2026-09-11 反馈 S-2）**：bootstrap 用
  `GMS_INSTALL_INSECURE=1` 只是一次性引导手段，安装完成后必须让 profile
  信任 Controller CA——把 CA 证书下发到 Agent 主机（如
  `/etc/gms/controller-ca.pem`），重新 enroll/写 profile 时带上
  `ca_cert` 字段（见 [profiles.md](profiles.md)），或运行时导出
  `GMS_CURL_CA_CERT=/etc/gms/controller-ca.pem` 并移除
  `GMS_CURL_INSECURE=1`。`gms-rt-system-selfcheck` 会对
  `tls_insecure: true` 给出修复提示。
- bootstrap 下载走 Controller Agent Package Registry
  （`/api/agent/packages/gms-remote-test/manifest` + universal 包）：
  同源 URL、拒绝重定向、校验 SHA-256 与 Ed25519 manifest 签名（已固定
  验证公钥时签名缺失/无效即失败），并做防穿越/防 symlink 的安全解包。
- 安装完成后 profile 由 `gms-agent` 经 profile_store 写入（见
  [profiles.md](profiles.md)），Service Token 落盘
  `~/.local/state/gms-remote-test/<profile>.token`（0600），通过
  `GMS_AUTH_TOKEN_FILE` 引用。Agent 不需要、也不应接收平台用户密码。

也可分步执行（先装 runtime + skill + MCP 注册，再单独 enroll）：

```bash
gms-agent install --client auto --server https://CONTROLLER:5001
gms-agent enroll <CODE>
```

默认 profile 名包含 Controller 身份（`<client>-<host>-<sha256(server)[:8]>`），
同一台主机为多个 Controller 安装时各占一个 profile、互不覆盖；需要可读
名字时加 `--profile gms-prod`。多 Controller 主机上 `enroll` / `update`
必须用 `--profile`（或 `--server`）消歧，否则 fail closed（见
[profiles.md](profiles.md) 的 Controller 解析契约）。

## 第 3 步：`gms-agent doctor --json` 验收

```bash
gms-agent doctor --client kkagent --json
# 或 gms-agent doctor --json 检测全部已配置/已安装的 client
```

doctor 输出一份**不含任何凭据内容**的本地一致性报告，逐项核对：

| 检查项 | 报告字段 | 合格标准 |
| --- | --- | --- |
| runtime version | `versions.running` / `versions.installed` / `versions.current_cli` | 三者一致（`versions.consistent: true`） |
| client | `clients[].client` | 覆盖目标 client |
| profile | `clients[].profile` | `selected` 非空（多 profile 时需显式 `--profile`/`GMS_RT_PROFILE`）、`controller` 为合法 URL、`ca_configured`/`ca_present` 与部署一致 |
| TLS CA | `clients[].profile.ca_configured` + `ca_present` | 配置了 CA 时文件必须存在 |
| Service Token | `clients[].token` | `present: true`、`mode_ok: true`（0600）、`owner_ok: true`；报告只含元数据，不打印 token 内容 |
| MCP registration | `clients[].mcp` | `registered: true`（检查对应 client 配置中的 `mcp_launcher.py` 注册标记） |
| Skill | `clients[].skill_present` / `skill_path` | `SKILL.md` 存在于 `skill_path` |

整体结论看顶层 `ok`；任何失败项都会在 `actions` 数组给出对应修复动作
（如 re-enroll、修复 token 权限、reconcile MCP 注册、安装 Skill）。
Connectivity（Controller 连通性）建议用 Agent 侧自检验证：

```bash
gms-rt-system-selfcheck --json    # 凭据、认证身份与 scopes、server 健康、设备清单
```

## 常用维护入口

```bash
gms-agent status      # 安装版本、检测到的 client、profile 与认证状态
gms-agent update      # 从 Registry 检查新版本、校验后整包升级并重新激活
gms-agent rollback <version>
```

## 相关文档

- [docs/agent/overview.md](overview.md) — 生成链与定位
- [docs/agent/security-model.md](security-model.md) — token 落盘与权限模型
- [docs/agent/profiles.md](profiles.md) — profile 布局
- [docs/agent/troubleshooting.md](troubleshooting.md) — enrollment/TLS/profile 常见故障
- [../../agent/gms-remote-test/docs/README.md](../../agent/gms-remote-test/docs/README.md) — 安装后使用
- 根 README「一键安装与 Enrollment」「包完整性与更新链」两节
