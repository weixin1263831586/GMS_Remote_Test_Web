# 0019. 对话 Agent 会话持久化与崩溃恢复

- 状态: 已接受
- 日期: 2026-10-17
- 相关: ADR 0010（Actor 与 ResourceOwner 身份分离）、ADR 0006（Agent Service 边界）

## 背景

对话 Agent（`features/assistant/`）的会话状态此前只存在于 Controller 进程内存
（`session_registry._agent_sessions`）。Controller 重启后，`session_id` 与
`cluster_job_id` / `attempt_id` / 执行阶段的关联丢失，浏览器侧会话显示
「过期」，但集群 Job 仍在运行，无人续接监控、retry 与失败分析。

完整的会话 UI 状态（消息正文、步骤、`pending_plan`）体量大且含敏感内容
（`pre_actions[].password`），不适合也不应该整体落盘。

## 决策

### 1. 只持久化最小恢复锚点

`features/assistant/session_store.py` 把每个会话的恢复锚点写入
`data/assistant/agent_sessions.json`（原子替换的单 JSON 文件，量级 =
会话数，每会话 < 2KB）。锚点按字段白名单提取（`session_id`、`client_id`、
`owner_id`、`status`、时间戳、`active_run` 关联 ID、`workspace_context`
标量字段）；`active_run.plan` 明确剥离——计划里可能带 `pre_actions` 密码，
凭据永不落盘。

### 2. 文件权限与写入原子性

- 锚点文件以 `0600` 创建（`os.open(..., 0o600)`），替换前显式
  `chmod(0o600)` 收敛 umask 影响；锚点不含明文凭据，但含
  owner/client/Job 关联，仍按受限权限对待。
- 写入走临时文件 + `fsync` + `os.replace` 原子替换；读路径容忍截断/
  损坏，按空存储处理。
- 进程内 `threading.Lock` 之外，使用 `fcntl.flock` 侧车锁
  （`agent_sessions.json.lock`）把多 Uvicorn Worker 进程的
  「读入 → 更新 → 替换」序列串行化，避免后写覆盖先写。

### 3. 监控任务替换的代际校验

同一会话连续提交新测试计划时，新一轮 `_execute_plan` 会铸造唯一
`monitor_token` 写入 `active_run`，并取消旧监控任务。旧任务在所有终态
写入点（`CancelledError` 清理、异常清理、done/error 状态落定、任务表
移除）先校验自己仍是当前代（`_monitor_run_is_current` /
`asyncio.current_task()` 身份比对），过期则立即退出、不改共享状态，
避免旧任务的取消回调清空新一轮的 `active_run` 与恢复锚点。

### 4. 恢复失败必须可观测

锚点持久化失败不允许静默降级：日志提升为 warning，并在会话上标记
`recovery_anchor_failed`，向诊断明确该会话在 Controller 重启后
不可恢复。

### 5. 单进程语义与跨主机责任边界

`_agent_sessions` 与监控任务表仍是单进程可丢弃 UI 状态；本决策不引入
跨进程会话共享。恢复的边界是：**监控连续性依赖同一个 Controller 进程
（重启后由锚点重建），而测试任务本身的权威状态始终在 Cluster Job 的
持久状态机与测试报告里**。Controller 主机整体故障时，Worker 侧任务
执行与报告采集不受影响，用户从测试页/集群页（而非对话 Agent）仍能
看到任务结果；对话 Agent 只承诺「重启后续接监控到终态」，不承诺
重建对话历史或 retry 计划。

## 后果

- 恢复出的会话没有持久化 plan（防凭据落盘），降级为纯跟踪：等待任务
  终态、关联报告、更新工作区；retry/分析自动跳过。
- 锚点按 `updated_at` 淘汰最旧记录（上限 256），长期运行不无限增长。
- 被清扫/取消的会话同步删除锚点，不残留可恢复入口。
