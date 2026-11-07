# ADR 0030：无人值守运维底座（kill-switch、告警与磁盘卫生）

- Status: Accepted
- Date: 2026-11-06
- Related: [ADR 0002](0002-feature-foundation-boundary.md)、[ADR 0007](0007-owner-config-in-configs-secrets.md)、[ADR 0012](0012-automation-machine-authority.md)、[ADR 0021](0021-ats-execution-options-and-evidence-retention.md)

## Context

自动化链路（编译→烧写→AOA 恢复→测试→分析→报告）已可长跑，但无人值守运行
缺少运维底座：停机只能改配置重启、告警只到 run 级通知（无 worker/磁盘类运维
告警）、临时文件随时间累积可能写满磁盘导致烧写静默失败。

## Decision

1. 新建 `features/ops` 特性作为唯一运维台账：`data/ops/ops.sqlite3` 存
   `ops_events`（去重键 + 抑制窗口 + ack 台账，ack 只改状态不删行）与
   `ops_flags`（全局运维标志）。迁移遵循进程安全规则（`BEGIN IMMEDIATE`
   + 幂等语句）。
2. 全局停机开关（kill-switch）是 `ops_flags` 中的 `advancement_paused`
   标志，经 `POST /api/ops/kill-switch`（admin）切换，每次切换写一条
   审计事件。语义是**停推进、不停维护**：`advance_runs_sync` 命令通道
   不再认领新 run、不触发计划调度，正在执行的步骤自然完成；超时清理、
   日志保留、告警 sweep 照常运行。停机不得制造僵尸 run。
3. 告警规则是纯函数（`features.ops.rules.evaluate`），输入维护快照
   （Worker 目录、最近 run、磁盘水位、Worker 心跳），输出事件描述；
   落库与去重在 `features/ops`，通知只对未抑制事件发送。
4. 通知通道：email 与 webhook。webhook 支持企业微信/钉钉/飞书机器人
   报文格式与 generic JSON，HMAC-SHA256 签名头
   `X-GMS-Ops-Signature`，签名密钥存 `configs/secrets/`（ADR 0007），
   配置只引用文件路径。通知与告警全程不抛出——运维链路故障不得打断
   自动化流水线（fail-open：kill-switch 检查失败视为未暂停并记录日志）。
5. 磁盘卫生只清理**可重建**文件：`.part` 下载残片、过期上传临时目录
   （`foundation.uploads.upload_temp_root()`）、以及仅限管理员显式触发的
   过期暂存固件（`data/automation/artifact-stage/`）。报告、cluster
   artifacts（ADR 0021 owner 级保留策略）与 APK 上传会话（firmware 自管
   生命周期）永不触碰。无人值守小时清扫不包含暂存固件——它可能仍被
   排队回放引用。
6. 自动化页新增"运维"页签：kill-switch、事件台账（ack）、磁盘概览与
   清理入口；读操作轮询，写操作仅由真实用户动作触发（遵循前端初始化
   不发变更请求的规则）。

## Consequences

运维事件是单点台账，后续修复闭环（fail↔patch 关联、结果驱动决策）可
直接消费 `ops_events` 作为升级通知的输入。kill-switch 持久化在
`ops_flags`（SQLite），Web/Worker/CLI 多进程按轮询读取同一全局开关，
不持久化排队意图——暂停期间 Gerrit webhook 仍可入队，恢复后按原顺序
推进；每个认领边界都重查标志，暂停后不再认领新 run。磁盘卫生是启发式
mtime 清扫：stage 清理前核对运行状态，非终态 run 引用的镜像跳过
（fail-closed）；活跃上传会话按子树最新 mtime 判定，仅 rmdir 空目录。
告警投递状态持久化在 `ops_events.notify_state`，失败按指数退避重试。
