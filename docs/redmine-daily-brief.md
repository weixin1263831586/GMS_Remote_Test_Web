# Redmine Daily Brief（凌晨每日晨报）

每天在各用户设置的触发时间（默认 00:00）自动读取当前用户个人看板中的「待回复」「RK 3 天未回复客户」
两类 issue，去重后冻结快照，用 kkagent headless 调本地大模型做逐 issue
深度分析，在 Redmine 页面「每日晨报」标签页查看结果。

## 架构

```
systemd timer 每分钟检查一次（Persistent=true；按 owner trigger_time 筛选）
  → python -m features.redmine.daily_brief_cli run-nightly
  → DailyBriefService（owner-aware 编排）
      → build_daily_triage_snapshot()   ← 唯一事实来源：get_workload_statistics()
      → 冻结快照（counts + issues + SHA256 fingerprint）
      → KkAgentRedmineAnalyzer（headless，--output-format stream-json，无 yolo）
      → Schema Gate + Runtime Evidence Gate
      → 任一门禁失败时 --resume 精确 session，限次修复
      → 聚合报告 + Markdown → daily_brief.sqlite3（per-owner）
Web 手动触发 → SQLite 持久 job 队列 → 独立 daily_brief_worker
      → 同一套 DailyBriefService / KkAgentRedmineAnalyzer
独立 delta timer 每分钟检查一次（按 owner delta_enabled/delta_trigger_time 筛选）
  → run-delta：fingerprint 未变的 issue 跳过，只重分析变化项
结果在 Redmine 页面「每日晨报」独立标签页查看（与个人看板平级）
```

关键文件：

| 文件 | 职责 |
| --- | --- |
| `features/redmine/daily_brief_models.py` | Run/Issue 数据模型、schema 校验、优先级规则 |
| `features/redmine/daily_brief_repository.py` | per-owner SQLite（runs/issues，幂等唯一索引） |
| `features/redmine/daily_brief_snapshot.py` | triage 快照（去重/bucket/fingerprint/delta） |
| `features/redmine/daily_brief_service.py` | 编排：幂等 run、并发控制、失败隔离、聚合 |
| `features/redmine/kkagent/` | stream-json 进程、轨迹、schema 与 runtime evidence gate、同会话修复 |
| `features/redmine/daily_brief_api.py` | REST API（triage/run/config/latest/refresh/analyze-issue/事件时间线等） |
| `features/redmine/daily_brief_worker.py` | Web 入队任务的独立 Worker、租约与优雅停止 |
| `features/redmine/daily_brief_analysis_events.py` | 分析进度事件存储（脱敏白名单摘要、增量轮询） |
| `features/redmine/daily_brief_cli.py` | systemd 入口（run-nightly/run-delta/doctor） |
| `agent/gms-remote-test/skill/references/redmine-daily-triage.md` | Agent 分析规范（skill，批量晨报与单条深度分析同工作流） |
| `deploy/systemd/gms-redmine-daily-brief*` | timer/service 单元 |

## 单一事实来源

`waiting_my_reply` / `no_reply_3_days` 只由
`RedmineAgentDB.get_workload_statistics()` 判定（与个人看板同源）；
Daily Brief、triage 工具、前端均不得重新实现筛选规则。

## owner 身份规范（canonical owner id）

Web 匿名会话的请求 owner 是原始 display id（`user@ip`），而
systemd/CLI/Worker 用 sanitize 后的目录名（`user_ip`）。晨报的
per-owner 库按目录落盘，runs 表的 `owner_id` 必须**始终**写 sanitize
后的 canonical 值：入库（`create_run` / `update_run`）与所有按 owner
查询的仓库方法统一经 `canonical_owner_id()` 规范化，
`DailyBriefService.__init__` 同样收敛入口。禁止调用方直接用原始
display id 比对 `run.owner_id`。schema v5 迁移会把存量库里的 legacy
`owner_id` 一次性改写为 canonical；与既有 canonical 行
`(owner_id, brief_date, mode)` 冲突的孪生 run 连同子表记录删除
（保留 canonical 孪生，即定时任务写入的权威报告）。

## 配置（owner runtime `redmine_daily_brief` 段）

```json
{
  "enabled": false,
  "trigger_time": "00:00",
  "delta_enabled": true,
  "delta_trigger_time": "06:00",
  "analysis_backend": "kkagent",
  "model": "",
  "agent_profile": "",
  "max_turns": 0,
  "issue_timeout_seconds": 0,
  "max_parallel_issues": 1,
  "max_issues": 50,
  "stale_days": 3,
  "list_limit": 100
}
```

- `enabled` 默认 `false`（opt-in）：每日晨报会消耗 kkagent 分析资源，owner
  在设置里显式开启后才进入 nightly 调度。
- `trigger_time` 是 Controller 服务器本地时区的每天触发时间，格式为 `HH:MM`，
  默认 `00:00`。定时入口每分钟检查一次，并只为恰好到点的 owner 入队；以
  `--owner` 手动试跑不受此设置限制。
- `delta_enabled` 默认 `true`；`delta_trigger_time` 默认 `06:00`。它们同样是每个
  owner 独立设置，且只有已启用每日晨报的 owner 才会
  进入 delta 调度。
- `model` 为空时使用 kkagent 当前默认模型；
- 分析和同会话修复不设步数、耗时或 Token 预算，以证据充分、结论可用为
  目标。旧 `max_turns` / `issue_timeout_seconds` 配置仍兼容读取，但统一
  规范化为 `0`（无限制）。可随时在晨报页面手动停止；外部模型/工具的
  连接故障仍按执行异常记录。
- `analysis_backend` 仅支持 `kkagent`；历史上可配置 `direct` 但从未实现，
  已从枚举移除（旧配置值会被规范化回 `kkagent`）。
- `max_parallel_issues` 默认 1：同机多个 headless kkagent 会话可能互相中断；
  仅在确认当前 kkagent 运行时支持会话隔离后才提高。
- `agent_profile` 绑定该 owner 的本机 kkagent agent profile 名
  （`~/.config/gms-agent/profiles/<name>.toml`）。多 owner 部署必须逐 owner
  设置：分析子进程据此注入 `GMS_RT_PROFILE`，且**不继承**宿主进程的
  MCP 身份环境。为空、selfcheck 不可用、token 无效或认证结果不完整时，
  run 在启动 Agent 前 fail-closed，绝不回退他人凭据。

## 身份与安全边界

在「每日晨报」顶部的「单号分析」输入 Redmine 单号，点击「开始分析」
或按 Enter。该入口仅排队诊断输入的工单，不扫描待处理列表，也不要求
工单先出现在晨报里。结果直接展示 kkagent 的原始 Markdown 总结，
任务运行中可用「停止分析」取消对应任务（停止后仍会记录 cancelled
终态事件）。单号任务与 nightly/delta 晨报分别保存。

API：`POST /api/redmine-agent/daily-brief/analyze-issue`，JSON 为
`{"issue_id":647338}`；返回 `run_id` / `job_id`。通过
`GET /api/redmine-agent/daily-brief/runs/{run_id}` 查看状态与结果。
触发需要人工会话，取证仍使用该 owner 绑定的 Agent Profile。

- 每日晨报严格**单人视角**：owner 经 `resolve_daily_brief_owner_identity` 解析为
  单个 Redmine 用户（配置用户名命中 user map → 该映射；否则 Redmine 当前
  登录用户）。绝不把 user map 的部门全员当 owner，也不在缺身份时回退
  `None`（那会展开为全部 assignee）。
- 读端点需 `redmine.read` scope；配置/触发/重分析等写端点**仅限人工会话**
  （Agent token 一律 403）。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/redmine-agent/daily-brief/triage` | 当天待处理清单（CLI/MCP 同源） |
| GET | `/api/redmine-agent/daily-brief/latest` | 最新每日晨报 |
| GET | `/api/redmine-agent/daily-brief/{date}` | 指定日期每日晨报 |
| GET | `/api/redmine-agent/daily-brief/active-issue` | 最近一次单号分析 run |
| GET | `/api/redmine-agent/daily-brief/issue-analyses` | 单号分析历史列表 |
| GET | `/api/redmine-agent/daily-brief/issue-analyses/{issue_id}` | 单个工单的分析历史 |
| POST | `/api/redmine-agent/daily-brief/analyze-issue` | 单号分析（持久化入队，返回 run_id/job_id） |
| POST | `/api/redmine-agent/daily-brief/sync-subjects` | 同步历史分析条目的工单标题 |
| GET | `/api/redmine-agent/daily-brief/runs/{run_id}` | run 状态与结果 |
| GET | `/api/redmine-agent/daily-brief/runs/{run_id}/issues/{issue_id}/events` | 分析进度事件增量（`after_sequence=N`） |
| POST | `/api/redmine-agent/daily-brief/runs/{run_id}/cancel` | run 级取消 |
| POST | `/api/redmine-agent/daily-brief/runs/{run_id}/issues/{issue_id}/reanalyze` | 按 run 重分析单 issue |
| POST | `/api/redmine-agent/daily-brief/{date}/issues/{issue_id}/reanalyze` | 按日期重分析单 issue |
| POST | `/api/redmine-agent/daily-brief/{date}/cancel` | 按日期取消当天 run |
| POST | `/api/redmine-agent/daily-brief/run` | 手动触发（持久化入队，返回 run_id/job_id；请求体 `{"force": true}` 强制重跑当天结果） |
| POST | `/api/redmine-agent/daily-brief/{date}/refresh` | delta 刷新 |
| GET/PUT | `/api/redmine-agent/daily-brief/config` | 配置读写 |
| GET | `/api/redmine-agent/daily-brief/model-options` | 可选模型列表 |
| GET | `/api/redmine-agent/daily-brief/agent-profiles` | 可绑定 agent profile 列表 |

读端点需 `redmine.read`；run/refresh/reanalyze/cancel/analyze-issue/config 等写端点仅限人工会话。

Agent 侧 CLI/MCP：`gms-rt-redmine-triage` / `gms_rt_redmine_triage`
（只读）。

## 启用与部署

1. Web UI 设置页保存 Redmine 地址与凭据（human-only）；
2. 确认 kkagent 在 PATH 且本地模型可用（`daily_brief_cli doctor`）；
3. 安装 timer：

```bash
sudo cp deploy/systemd/gms-redmine-daily-brief*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gms-redmine-daily-brief-worker.service
sudo systemctl enable --now gms-redmine-daily-brief.timer
sudo systemctl enable --now gms-redmine-daily-brief-delta.timer   # 可选
```

手动试跑：`python -m features.redmine.daily_brief_cli run-nightly --owner <id>`

## 安全边界

- 全链路只读：Agent 无 Redmine 写权限、无 shell、无设备控制；
- 子进程固定 `GMS_MCP_TOOLSETS=evidence`，只暴露只读取证工具与基础发现工具；
- 不使用 `--yolo`/`--auto`；subprocess 用 `create_subprocess_exec`；
- Redmine 内容（描述/journal/附件）一律视为不可信数据，prompt 明示
  不得执行其中指令；
- token/密码/Cookie 不进入日志与 raw_response 之外的任何输出；
- 每 owner 数据隔离（`data/redmine/by_user/<owner>/daily_brief.sqlite3`）。

## 可靠性

- 幂等：owner+date+mode 唯一；默认复用 completed/partial，人工可在请求体传
  `{"force": true}` 替换当天快照并重跑，failed 可重试；
- 失败隔离：单 issue 失败 → run=partial；全部失败 → failed；
- Web 触发只写 SQLite job 队列，不在 uvicorn 事件循环内持有长任务；Worker
  以租约领取并定期续租，异常退出后任务自动重新入队；
- kkagent 使用独立进程组；超时/Worker 停止时按 TERM→等待→KILL 回收
  kkagent 与全部 stdio MCP 子进程，信号退出单独标记为 interrupted；
- 崩溃恢复：进程重启后 `reset_stale_running` 把僵尸 running 标记 failed；
- 一个 issue 对应一个 kkagent session；自动修复只使用该 issue 的精确
  `--resume <session_id>`，禁止 `--continue` 串入其他会话；
- AI 负责摘要、根因、建议和 `confidence`；`history_checked`、工具调用数、
  session、耗时和 token 等运行事实由 stream-json 轨迹派生；
- Evidence Gate 只接受收到成功 `tool_result` 的调用。它要求完整 issue、
  journals、附件清单、每个可读 text/log 附件至少一次 artifact read、至少
  2 个归一化后不同的历史查询；`similar_issues[].issue_id` 必须出现在成功
  history search 或 issue fetch 的结构化结果中；
- Schema 或 Evidence Gate 未通过时，在同一 session 内限次修复并重新执行
  两道门禁。失败会保留 repair 次数和合并后的脱敏工具轨迹供 UI 审计；
- 冻结快照持久化：快照随 run 落盘（SQLite），失败/崩溃重试复用同一份
  输入事实，Redmine 数据变化不改变既有 run 的分析基准；人工 force 重跑
  才会重新冻结；
- pre-sync 失败可见：快照带 `source_sync_status`（synced / sync_failed /
  skipped），失败降级本地镜像时 run 与 Markdown 报告明确标注数据可能过期；
- 同 run 执行协调：force/refresh/reanalyze 与进行中任务汇合为单次执行，
  不并发写同一 run；
- `Persistent=true`：00:00 停机则开机补跑；flock 防同机并发。

### SQLite 多进程 schema 迁移契约

Web 进程、daily_brief Worker、systemd、CLI 可能同时初始化同一 owner 库。
schema 迁移（建表、补列）必须满足：

- 迁移整体持有 `BEGIN IMMEDIATE` 写锁后再读 schema、再执行 `ALTER TABLE`，
  避免两进程同时 `PRAGMA table_info` 判列缺失、同时 `ADD COLUMN` 触发
  `duplicate column name`（TOCTOU，生产事故已发生过一次）；
- schema 版本记录在 `PRAGMA user_version`（`DailyBriefRepository._SCHEMA_VERSION`）：
  已是当前版本的库走快路径跳过全部 DDL；旧库在写锁内逐版升级。每次改表
  结构必须把 `_SCHEMA_VERSION` +1；
- 迁移天然幂等：先判存在再补列，重复初始化（migration twice）、
  user_version 意外回退重放均安全；
- 回归测试：`features/redmine/tests/test_daily_brief_repository.py` 的
  `CrossProcessConsistencyTests`（含 8 进程并发初始化用例）与
  `test_migration_stamps_user_version_and_fast_paths` /
  `test_user_version_rollback_is_safe_on_reopen`。

### 分析进度时间线（realtime progress）

「查看分析」弹框在分析运行中展示实时执行时间线，数据链路（ADR：轮询而非
SSE/WebSocket）：

```
kkagent --output-format stream-json
    → consume_line(on_event=…)            features/redmine/kkagent/trace.py
    → ProgressTap（tool_call/tool_result 归一化）  kkagent/progress_tap.py
    → AnalysisProgressRecorder（脱敏/白名单摘要）  daily_brief_analysis_events.py
    → redmine_daily_brief_analysis_events 表（per-owner SQLite）
    → GET /daily-brief/runs/{run_id}/issues/{issue_id}/events?after_sequence=N
    → 前端 2.5s 增量轮询，终态后原地切换最终报告
```

- 事件词表固定 8 种（analysis_started / stage_changed / tool_started /
  tool_completed / tool_failed / progress / analysis_completed /
  analysis_failed），UI 不消费 kkagent 原始协议；
- 只落 allowlist 字段（tool_name/stage/status/summary/duration/时间），
  工具输出与 reasoning 一律不落库；summary 仅含身份字段摘录并经
  `scrub_secrets` 清洗；
- preflight（Controller 证据预采集）与 kkagent 工具调用都会产生事件；
  取消/失败同样收敛出终态事件；
- 表通过幂等 `CREATE TABLE/INDEX IF NOT EXISTS` 自举（写锁内执行），不
  占用 `user_version` 快路径；终态 run 的事件保留 30 天，Worker 启动时
  清理（`purge_expired`，含 run 行已消失的孤儿事件）。
- 每日晨报行与「Redmine 单号分析」条目的按钮集一致：查看分析 / 打开
  Redmine / AI 统计 / 增量-全量选择 / 重新分析 ↔ 停止分析。行级停止走
  run 级精确取消（`POST /daily-brief/runs/{run_id}/cancel`）。「增量」在
  晨报 run 上按单重跑（结论原地更新）；「全量」走 `analyze-issue` 新建
  独立 `issue:` run（晨报记录保留可审计，新结论进入单号分析历史）。
- 终态 run 在 30 天保留期内可从报告弹框「执行过程」回看完整时间线
  （历史模式：不轮询、终态徽标，报告 ↔ 执行过程原地互切）；超期后
  按钮禁用并提示已清理。

## 已知限制（第一阶段）

- 可配置 delta、历史每日晨报趋势、persistent issue 连续天数统计已具备数据
  基础，UI 聚合视图后续迭代。
