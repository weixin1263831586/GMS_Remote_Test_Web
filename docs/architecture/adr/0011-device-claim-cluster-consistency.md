# 0011. 设备归属与集群状态的跨库一致性

- 状态: 已接受
- 日期: 2026-09-16
- 相关: ADR 0001（Controller/Worker 边界）、ADR 0005（USB/IP 固件归属）

## 背景

设备资源模型目前横跨两个 SQLite 数据库：

- **Cluster DB**（`cluster.sqlite3`）：`cluster_jobs`、`cluster_device_reservations`、
  `device_leases`（逻辑状态、审计时间线）。
- **Device Claim DB**（`device_claims.sqlite3`）：`device_claims`（物理 fencing
  权威；同时被本地设备锁 `DeviceLockManager` 与集群租约共享）。

业务代码把两者当成一个原子事务使用，例如 `reserve_devices`：
先在 Cluster 事务里写 reservation，再 `claims.acquire` 提交到 Claim DB。
进程若死在两次提交之间（kill -9 / OOM / 掉电），会留下：

- reservation 不存在但 claim 存在 → 幽灵占用；
- reservation ACTIVE 但 claim 已释放 → 无 fencing 的活跃预约；
- job 状态已提交但 claim 未写 → 运行中任务无物理锁。

Python 的 `try/except → compensate()` 只能处理正常异常，处理不了进程崩溃。

## 决策

**不合并两个数据库**，采用「可对账的中间形态」：启动期幂等 reconciliation。

理由：`DeviceClaimRegistry` 刻意与本地设备锁分离——本地锁与集群租约必须
互相 fencing（见 `test_local_lock_blocks_cluster_lease_and_different_source_for_same_owner`）。
把 claim 表并入 Cluster DB 会让「集群存储重启/清库」波及本地设备锁，
并扩大 Cluster DB 的写竞争面。SQLite 的 WAL 不支持跨库原子提交，ATTACH
进单连接只是把两个库绑在一条连接上，崩溃语义不变，收益有限、风险更大。

因此保留双库，用**启动期对账**把 split-brain 收敛到可自愈状态
（`features/cluster/repository.py: reconcile_claims()`，在 `__init__` 中调用）：

| 漂移 | 修复 |
| --- | --- |
| 活跃 reservation 缺 claim | 按 reservation 设备重取 `reservation:<id>` claim |
| 非活跃 reservation 残留 claim | 释放 claim（`reconciled`） |
| 终态 job 残留 claim | 释放 claim（`reconciled`） |
| 非终态 job 缺 claim | 按 `device_leases` 重取 `job:<id>` claim；重取失败即置 job `failed` |

全部修复在 `BEGIN IMMEDIATE` 内完成、幂等、可重复运行，并返回计数器
（`reservation_claims_reacquired` / `job_claims_reacquired` /
`unfenceable_jobs_failed` …）供可观测性与测试断言。

## 后果

- 崩溃后的幽灵 claim / 无 fencing 任务在下次进程启动时自动收敛。
- 绝不出现「无物理 fencing 的 job 继续跑」：重取失败一律 fail closed 置 failed。
- 需要一次进程重启触发对账；运行期内的瞬时双写窗口仍存在，但影响限于
  「快照短暂不一致」，不产生持久占用。

## 后续（若迁移）

若要彻底消除窗口，需把归属收敛成单一 `ResourceOwnership` 状态机并独立成
服务；届时 `device_claims` 与 `cluster_device_reservations`/`cluster_jobs`
的归属字段统一为一张表。本 ADR 记录该方向为未来工作，不作为当前实现。
