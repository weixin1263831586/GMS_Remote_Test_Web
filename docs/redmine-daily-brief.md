# Redmine AI Daily Brief（凌晨 AI 晨报）

每天 00:00 自动读取当前用户个人看板中的「待回复」「RK 3 天未回复客户」
两类 issue，去重后冻结快照，用 kkagent headless 调本地大模型做逐 issue
深度分析，09:00 上班在个人看板直接查看 AI 晨报。

## 架构

```
systemd timer 00:00 (Persistent=true)
  → python -m features.redmine.daily_brief_cli run-nightly
  → DailyBriefService（owner-aware 编排）
      → build_daily_triage_snapshot()   ← 唯一事实来源：get_workload_statistics()
      → 冻结快照（counts + issues + SHA256 fingerprint）
      → KkAgentRedmineAnalyzer（headless，--output-format json，无 yolo）
      → 聚合报告 + Markdown → daily_brief.sqlite3（per-owner）
08:40 timer → run-delta：fingerprint 未变的 issue 跳过，只重分析变化项
09:00 → 个人看板顶部「AI 晨报」卡片
```

关键文件：

| 文件 | 职责 |
| --- | --- |
| `features/redmine/daily_brief_models.py` | Run/Issue 数据模型、schema 校验、优先级规则 |
| `features/redmine/daily_brief_repository.py` | per-owner SQLite（runs/issues，幂等唯一索引） |
| `features/redmine/daily_brief_snapshot.py` | triage 快照（去重/bucket/fingerprint/delta） |
| `features/redmine/daily_brief_service.py` | 编排：幂等 run、并发控制、失败隔离、聚合 |
| `features/redmine/kkagent_analyzer.py` | headless kkagent 分析器 + prompt v1 |
| `features/redmine/daily_brief_api.py` | REST API（triage/run/config/latest/refresh） |
| `features/redmine/daily_brief_cli.py` | systemd 入口（run-nightly/run-delta/doctor） |
| `agent/gms-remote-test/skill/references/redmine-daily-triage.md` | Agent 分析规范（skill） |
| `deploy/systemd/gms-redmine-daily-brief*` | timer/service 单元 |

## 单一事实来源

`waiting_my_reply` / `no_reply_3_days` 只由
`RedmineAgentDB.get_workload_statistics()` 判定（与个人看板同源）；
Daily Brief、triage 工具、前端均不得重新实现筛选规则。

## 配置（owner runtime `redmine_daily_brief` 段）

```json
{
  "enabled": true,
  "analysis_backend": "kkagent",
  "model": "",
  "agent_profile": "",
  "max_turns": 12,
  "issue_timeout_seconds": 600,
  "max_parallel_issues": 2,
  "max_issues": 50,
  "stale_days": 3,
  "list_limit": 100
}
```

- `model` 为空时使用 kkagent 当前默认模型；
- `analysis_backend` 可选 `direct`（保留 fallback 能力，默认 kkagent）。
- `agent_profile` 绑定该 owner 的本机 kkagent agent profile 名
  （`~/.config/gms-agent/profiles/<name>.toml`）。多 owner 部署必须逐 owner
  设置：分析子进程据此注入 `GMS_RT_PROFILE`，且**不继承**宿主进程的
  MCP 身份环境。为空则不注入身份——分析仍可运行，但 MCP 取证工具会以
  未配置身份失败（fail-closed，绝不回退他人凭据）。

## 身份与安全边界

- 晨报严格**单人视角**：owner 经 `resolve_daily_brief_owner_identity` 解析为
  单个 Redmine 用户（配置用户名命中 user map → 该映射；否则 Redmine 当前
  登录用户）。绝不把 user map 的部门全员当 owner，也不在缺身份时回退
  `None`（那会展开为全部 assignee）。
- 读端点需 `redmine.read` scope；配置/触发/重分析等写端点**仅限人工会话**
  （Agent token 一律 403）。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/redmine-agent/daily-brief/triage` | 当天待处理清单（CLI/MCP 同源） |
| GET | `/api/redmine-agent/daily-brief/latest` | 最新晨报 |
| GET | `/api/redmine-agent/daily-brief/{date}` | 指定日期晨报 |
| POST | `/api/redmine-agent/daily-brief/run` | 手动触发（后台执行，返回 run_id） |
| POST | `/api/redmine-agent/daily-brief/{date}/refresh` | delta 刷新 |
| POST | `/api/redmine-agent/daily-brief/{date}/issues/{id}/reanalyze` | 单 issue 重分析 |
| GET/PUT | `/api/redmine-agent/daily-brief/config` | 配置读写 |

Agent 侧 CLI/MCP：`gms-rt-redmine-triage` / `gms_rt_redmine_triage`
（只读）。

## 启用与部署

1. Web UI 设置页保存 Redmine 地址与凭据（human-only）；
2. 确认 kkagent 在 PATH 且本地模型可用（`daily_brief_cli doctor`）；
3. 安装 timer：

```bash
sudo cp deploy/systemd/gms-redmine-daily-brief*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gms-redmine-daily-brief.timer
sudo systemctl enable --now gms-redmine-daily-brief-delta.timer   # 可选
```

手动试跑：`python -m features.redmine.daily_brief_cli run-nightly --owner <id>`

## 安全边界

- 全链路只读：Agent 无 Redmine 写权限、无 shell、无设备控制；
- 不使用 `--yolo`/`--auto`；subprocess 用 `create_subprocess_exec`；
- Redmine 内容（描述/journal/附件）一律视为不可信数据，prompt 明示
  不得执行其中指令；
- token/密码/Cookie 不进入日志与 raw_response 之外的任何输出；
- 每 owner 数据隔离（`data/redmine/by_user/<owner>/daily_brief.sqlite3`）。

## 可靠性

- 幂等：owner+date+mode 唯一；completed/partial 复用，failed 可重试；
- 失败隔离：单 issue 失败 → run=partial；全部失败 → failed；
- 崩溃恢复：进程重启后 `reset_stale_running` 把僵尸 running 标记 failed；
- `Persistent=true`：00:00 停机则开机补跑；flock 防同机并发。

## 已知限制（第一阶段）

- 08:40 delta、历史晨报趋势、persistent issue 连续天数统计已具备数据
  基础，UI 聚合视图后续迭代；
- `analysis_backend=direct`（UniversalAIAnalyzer fallback）尚未接线，
  当前仅 kkagent。
