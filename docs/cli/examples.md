# gms-rt 使用约定与工作流

`gms-rt` 是 Agent Package 的 CLI；定位与发行流程见
[Agent 概览](../agent/overview.md)，完整参数见生成的
[命令参考](command-reference.md)。MCP 场景优先使用 typed tools，
CLI 命令用连字符、MCP 工具用下划线。

Agent 使用 Service Token；多 Controller 环境先明确 `GMS_RT_PROFILE`。
自动化调用附加 `--json --non-interactive`，以 JSON 的 `ok` 和退出码判断结果。
下列命令中的设备、套件、Worker 与 Job ID 都需按当前库存和前序响应替换。

## 工作流：查询上下文 → 列命令 → 跑测试 → 取报告

### 1. 环境与凭据自检（无副作用）

```bash
gms-agent doctor --json
gms-rt-system-selfcheck --json
```

任何一项失败都带修复提示，优先按返回的 hints 处置。

### 2. 发现命令

```bash
gms-rt-system-commands --json              # 全量命令目录（含风险模式与 agent_safe_unattended 标记）
gms-rt-system-command-describe gms-rt-devices-wait  # 单条命令的用法/风险/认证要求
```

### 3. 设备确认

```bash
gms-rt-devices-list                              # 设备清单（status + protocol: adb/fastboot）
gms-rt-devices-wait <devices> --state online --max-wait 300   # 有界等待设备就绪
```

多 worker 部署先用 `gms-rt-cluster-workers` 拿 `worker_id`，再带它调用
设备类命令，避免 serial 歧义（exit 5）。

### 4. 启动测试（长任务用 job 轮询）

```bash
gms-rt-test-start RK3572 CTS android-cts-17_r1
# 用法：gms-rt-test-start <DEVICE> [TYPE] [MODULE/SUITE] [CASE/SUITE] [SUITE] [--wait] [--max-wait SECONDS]
# 长任务（CTS 模块几十分钟）不要带 --wait：直接拿 job_id 后轮询
```

之后轮询 Durable Job（每 20-30s 一次即可）：

```bash
gms-rt-jobs-status <job_id>                    # 权威状态（便宜轮询）
gms-rt-jobs-events <job_id> [after_sequence]   # 增量事件（带 after 游标，避免重读历史）
gms-rt-jobs-wait <job_id> --max-wait SECONDS   # 或有界等待到终态
gms-rt-jobs-list [limit]                       # 便宜的忙碌检查（limit 1-500）
```

要点：

- 设备忙/冲突 → exit `5`：先 `gms-rt-jobs-list` 检查占用，不要盲目重试。
- 只有 exit `6`（网络）适合有界自动重试；exit `4` 表示需人工提权。
- 失败重跑用 retry 模式：`gms-rt-test-start --retry <report_timestamp>`。

### 5. 读取结果与报告

```bash
gms-rt-test-suites-result <suite_path|suite_name>   # 先拿结果索引；summary 优先于全文
gms-rt-reports-list                                 # 已完成的测试报告
```

测试日志可达数百 MB：先用结果索引定位，再按 offset 窗口读取需要的段落。

### 6. 取证（Redmine 证据链，只读）

```bash
gms-rt-redmine-issue-fetch <issue>          # 全量快照（原始 JSON、不截断 journals）
gms-rt-redmine-attachments <issue>          # 附件清单（kind/size/sha256）
gms-rt-artifact-search / gms-rt-artifact-read   # 证据文本检索与窗口读取
```

## 人工会话专用（Agent 禁用）

```bash
gms-rt-auth-login <user> --password-stdin   # 密码登录，仅人工 CLI 会话
gms-rt-auth-elevate                          # 管理员提权（step-up）
gms-rt-approval-create                       # 为 Agent 的高风险操作签发一次性审批令牌
```

Agent 凭据是 Service Token，永远不使用密码登录；破坏性操作（如
`gms-rt-devices-shell` 的变更命令、固件烧录）需人签发的一次性 approval
token（绑定 tool + 设备 + 命令 SHA-256，5 分钟 TTL、单次使用），详见
[../agent/security-model.md](../agent/security-model.md)。

## 相关文档

- [docs/cli/command-reference.md](command-reference.md) — 生成的命令表
- [../../agent/gms-remote-test/skill/references/agent-workflows.md](../../agent/gms-remote-test/skill/references/agent-workflows.md) — 端到端验证过的 playbook（含 MCP 变体）
- [../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md](../../agent/gms-remote-test/docs/AGENT_PLAYBOOK.md) — 测试执行与设备问题要点
- [docs/agent/security-model.md](../agent/security-model.md) — 审批令牌与权限
