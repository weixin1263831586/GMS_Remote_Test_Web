# ADR-0005: USB/IP 完整固件烧写的所有权交还（Source-side Flash）

- 状态：已采纳（Accepted）
- 关联代码：`features/firmware/firmware_api.py`、`features/firmware/usbip_transport.py`、`features/firmware/source_flash.py`、`features/devices/usbip_flash.py`、`features/devices/reconnect.py`、`scripts/windows_source_agent.py`

## 背景

Rockchip 整包固件烧写（`upgrade_tool uf`）期间，设备会经历 **ADB → RockUSB Loader → MaskROM → Loader → ADB** 的多次 USB 重新枚举。当设备经由 USB/IP 从 Source Host 转发到 Worker 时，每次重枚举都要在 USB/IP 链路上重新 bind/attach。实机验证表明：**USB/IP 链路无法维持跨越多次 USB 重枚举的会话**，历次在 Ubuntu Worker 端直接烧写的方案（USB/IP Fastboot 分区烧写、`partition` 同会话 DI、`transport-probe-force`、`uf + watcher`）均不可靠，已全部删除；当前 API 的 `burn_mode` 仅接受 `auto` 与 `uf`。

USB/IP detach 后，设备回到 Windows 源主机，不会出现在 Controller 本机 ADB——因此烧写必须下发到物理 Source 端执行。但 Windows 端没有 CLI 烧写工具（RKDevTool 仅 GUI），且 SSH 会话启动的 GUI 进程没有可见窗口（session 隔离），无法从 Controller 直接自动化。

同时，烧写期间设备会反复「消失 / 换身份」，若 Worker 的通用 USB/IP 重连 watchdog 继续运行，会对同一 BUSID 启动第二个 attach 循环，与烧写互相破坏。

## 决策

**USB/IP 设备的完整固件烧写只在设备的物理 Source Host 上执行，不跨 USB/IP 链路维持多次 USB 重枚举。**

### 所有权交还状态机（ownership handoff）

烧写开始前，平台必须把设备所有权显式交还 Source Host（`features/firmware/usbip_transport.py` 的 `release_usbip_devices_to_source`，即 ADR 注释中的 PREPARE 段）：

1. **暂停通用重连 watchdog**：按 `device_host` + `device_ids` 双键记录 pause（TTL 2 小时，见 `features/devices/reconnect.py` 的 `pause_usbip_reconnect`）。paused 设备仍在正常设备视图中可见，但通用消失监控不得对其启动第二个 attach 循环。该 pause 与手工断开抑制（suppression）是两套独立机制。
2. **目标 Worker 侧 vhci detach**：按 host/busid 结构化匹配 `usbip port` 输出，只拆本次路由涉及的端口。
3. **Fail-closed 复核**：再次查询 `usbip port`，确认目标端口已消失；查询失败或端口仍存在一律视为未释放，中止烧写（HTTP 409）——防止「假 detach」让 Source 端拿不到物理设备。
4. detach 后等待约 2 秒，让 Source 端 PnP 重新认领设备。
5. 确认 Source Host 物理持有设备后，才进入 SOURCE_OWNED 状态并下发烧写。

### Windows Source：Source Agent + RKDevTool

采用「文件队列式 Source Agent」（`scripts/windows_source_agent.py`，交互桌面会话常驻，计划任务自启动，可操作 RKDevTool GUI）：

1. Controller 通过 SFTP 上传固件到 `C:\gms-flash\<task>\`；
2. 投递 `<task>.json` 任务（携带固件路径与目标设备 serial）到 `%USERPROFILE%\gms-flash-queue`；
3. Source Agent 执行 `adb -s <serial> reboot loader`，等待 RKDevTool 明确显示 Loader/Maskrom 后自动化烧写，按天滚动日志判定成败；
4. Controller 轮询 `<task>.result.json`（10s 间隔，5400s 超时）并校验结果；
5. 超时 / 失败不降级重试，由上层决定设备是否进入隔离状态。

### Linux Source：fail closed

Ubuntu/Linux 来源支持设备共享、分配与重连，但在 Linux Source 完整烧写 backend 实现之前，API 对 Linux 来源 USB/IP 设备的完整固件烧写直接拒绝（HTTP 409），**不会误送入 Windows backend**；来源 OS 探测失败同样 fail closed，绝不默默走 Windows。

### 恢复与对称性

烧写结束（成功、失败或异常）后：

- `finally` 中对称恢复 ownership handoff 的 pause：既 resume `device_id` 也 resume `device_host`——只 resume device 会留下 host 级 pause，`schedule_usbip_reconnect` 会因 host pause 拒绝调度，设备最长 2 小时无法自动回到平台管理；
- 需要重连时按原物理路由调度 USB/IP 重连（`usbip_reconnect_after_finish`），重新导出并 attach。

### AutoBind 与重枚举自愈

- Windows 来源要求 usbipd-win 4.2+（AutoBind policy 支持）。烧写前预检并按需创建 `usbipd policy add --effect allow --operation AutoBind --busid <busid>`，保证 Loader / MaskROM 二次枚举后设备被自动重新共享；策略校验失败（版本过低、SSH 账号无管理员权限、策略未生效）fail closed 拒绝烧写。
- Ubuntu 来源的等价物是用户态 `usbipd` 导出进程：按持久分配的序列号过滤导出，设备以相同序列号重新枚举时自动重连。
- Loader 的 PID 会随 SoC 改变（VID 固定 `2207`，如 RK3572 Loader 枚举为 `2207:351a`），平台同时识别 `Rockusb Device` 标记并优先重挂载原 BUSID，降低新增 SoC/PID 导致重连失败的风险。

### 安全约束

固件烧写属于破坏性操作：Web 侧受用户权限、设备 Claim、Worker 状态与操作审计约束；Agent 发起的烧写必须使用 Agent Service Token，并提供与目标设备集合、固件 SHA-256、`wipe_data`、`burn_mode` 精确绑定、只能消费一次的 Approval Token。

## 理由

- **物理所有权是唯一安全锚点**：只有确认 Source 端物理持有设备（detach 复核通过）后烧写才可能成功；任何「假设已释放」的乐观路径都已在实机上失败过。
- **不跨重枚举维持会话**是实测结论而非实现取舍：与其在 USB/IP 链路上继续修补不可靠的重枚举恢复，不如把烧写放到唯一能稳定持有设备的 Source 端。
- **文件队列解耦 GUI 自动化**：避免 SSH 启动 GUI 的 session 隔离问题，且队列文件天然提供任务幂等与结果留痕。

## 后果

- Windows Source 主机需要额外部署 Source Agent（交互桌面会话常驻 + 计划任务自启动），并要求 SSH 地址为 `user@host` 形式以定位队列目录。
- 完整固件烧写仅支持 Windows 来源的 USB/IP 设备；Linux 来源设备的完整烧写在 backend 落地前不可用（API 返回 409）。
- 烧写期间设备对平台不可见（watchdog 已暂停）；必须保证结束路径（含异常）对称恢复 pause 与重连，否则设备最长 2 小时无法自动回归。
- 操作细节与排障见 [docs/usbip/firmware-flashing.md](../../usbip/firmware-flashing.md) 与 [docs/usbip/troubleshooting.md](../../usbip/troubleshooting.md)。
