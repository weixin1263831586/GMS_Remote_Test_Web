# 固件烧写（Rockchip update.img）

本文档定位：描述平台 Rockchip `update.img` 完整固件烧写的两条链路——本地 USB（Local USB）与 USB/IP 来源端烧写（Source-side Flash），包括 ADB→Loader→MaskROM 状态迁移、USB 所有权状态机、Windows Source Agent + RKDevTool 自动化、失败恢复与安全约束。

## Overview

平台的完整固件烧写只使用 Rockchip `upgrade_tool uf` 整包路径。当前 API 的
`burn_mode` 仅接受 `auto` 与 `uf`；历史上的 USB/IP Fastboot 分区烧写、
`partition`（同会话 DI）、`transport-probe-force` 等实验性 backend 已删除，
不再属于当前架构。

两条烧写链路按设备接入方式分流：

- **Local USB devices**：设备直连 Ubuntu 测试主机，烧写在测试主机本地执行。
- **USB/IP devices**：设备物理插在 Source Host 上、经 USB/IP 转发给 Worker，
  完整烧写必须在设备的物理 Source Host 上执行（见下文所有权状态机）。

Loader、MaskROM 等 USB 重枚举属于 Source 端烧写 backend 的内部细节，
不需要（也无法）由用户手工选择传输 backend。

## Local USB flow

设备直连 Ubuntu 测试主机时：

1. 平台确认设备处于 ADB、Fastboot 或 Rockusb Loader 可识别状态
   （通过 `adb devices` / `fastboot devices` / RockUSB sysfs 探测）。
2. 由测试主机执行 `upgrade_tool uf` 完成 update.img 烧写。
3. Fastboot 状态的设备先回到 Android/ADB，再通过 `adb reboot loader`
   进入 Loader；已处于 Rockusb Loader 的设备直接进入烧写。

GSI 直刷只支持从 ADB/Fastboot 起步；RockUSB Loader 仅属于 `update.img`
固件烧写链路。

## Windows USB/IP flow

USB/IP 设备的完整固件烧写流程：

1. 平台解析设备到 (Source Host, BUSID) 的持久物理路由，并确认
   usbipd-win AutoBind 策略（Windows 来源，4.2+）或用户态 usbipd 导出
   进程（Ubuntu 来源）。
2. 烧写前显式完成所有权交还：暂停目标设备的通用重连 watchdog，目标
   Worker 侧 vhci detach，并 fail-closed 复核端口已释放——只有确认
   Source Host 物理持有设备后才开始烧写。
3. **Windows Source**：由运行在交互桌面会话中的 Windows Source Agent 调度
   RKDevTool 完成（Controller SFTP 上传固件与任务文件，轮询结果）。
4. **Ubuntu/Linux Source**：支持设备共享、分配与重连；在 Linux Source
   完整烧写 backend 实现之前，API 拒绝（HTTP 409）对 Linux 来源 USB/IP
   设备的完整固件烧写，不会误送入 Windows backend。
5. 烧写完成或失败后，平台按原物理路由恢复 USB/IP 导出与 Worker attach。

## ADB → Loader → MaskROM 状态迁移

Rockchip 整包固件烧写（`upgrade_tool uf`）期间，设备会经历
**ADB → RockUSB Loader → MaskROM → Loader → ADB** 的多次 USB 重新枚举。
每次重枚举设备的 USB 身份（VID:PID、Windows 设备标签）都可能改变：

- Rockchip Loader 的 VID 固定为 `2207`，PID 随 SoC 变化（如 RK3572 Loader
  枚举为 `2207:351a / Rockusb Device`）。部署可在 `configs/local/config.json`
  的 `usbip_vid_pids` 中补充需要显式识别的身份；平台同时识别
  `Rockusb Device` 标记，并优先重挂载原 BUSID。
- 每次重枚举都要在 USB/IP 链路上重新 bind/attach。实机验证表明：
  **USB/IP 链路无法维持跨越多次 USB 重枚举的会话**，在 Worker 端直接
  跨重枚举烧写的方案均不可靠，已全部删除（见 [ADR-0005](../architecture/adr/0005-usbip-firmware-ownership.md)）。

因此烧写期间设备反复「消失 / 换身份」是预期行为，平台不尝试在 USB/IP
链路上维持会话，而是把设备所有权交还物理 Source 端。

## USB 所有权状态机

烧写开始前，平台必须把设备所有权显式交还 Source Host
（`features/firmware/usbip_transport.py` 的 `release_usbip_devices_to_source`）：

```text
Worker owns
    │ 1. pause 通用重连 watchdog
    │ 2. Worker 侧 vhci detach
    │ 3. fail-closed 复核端口已消失
    │ 4. 等待 Source 端 PnP 重新认领（约 2 秒）
    ▼
Source host owns
    │ 5. 确认 Source Host 物理持有设备 → SOURCE_OWNED
    ▼
Source-side burn（Windows: Source Agent + RKDevTool）
    │ 烧写结束（成功/失败/异常，finally 对称恢复）
    ▼
resume watchdog + 按原物理路由 reconnect
```

各步骤要点：

1. **暂停通用重连 watchdog**：按 `device_host` + `device_ids` 双键记录
   pause（TTL 2 小时，`features/devices/reconnect.py` 的
   `pause_usbip_reconnect`）。paused 设备仍在正常设备视图中可见，但通用
   消失监控不得对其启动第二个 attach 循环。该 pause 与手工断开抑制
   （suppression）是两套独立机制。
2. **目标 Worker 侧 vhci detach**：按 host/busid 结构化匹配 `usbip port`
   输出，只拆本次路由涉及的端口——同一来源主机上其他设备的 USB/IP 会话
   （如正在跑 CTS 的另一台手机）不会被连带 detach。
3. **Fail-closed 复核**：再次查询 `usbip port`，确认目标端口已消失；
   查询失败或端口仍存在一律视为未释放，中止烧写（HTTP 409）——防止
   「假 detach」让 Source 端拿不到物理设备。
4. detach 后等待约 2 秒，让 Source 端 PnP 重新认领设备。
5. 确认 Source Host 物理持有设备后，才进入 SOURCE_OWNED 状态并下发烧写。

### 为何 target Worker 必须 detach

- **物理所有权是唯一安全锚点**：USB/IP detach 后设备回到 Windows 源主机，
  不会出现在 Controller 本机 ADB。只有确认 Source 端物理持有设备
  （detach 复核通过）后烧写才可能成功；任何「假设已释放」的乐观路径都
  已在实机上失败过。
- **避免双 attach 互相破坏**：烧写期间设备反复消失/换身份，若 Worker 的
  通用重连 watchdog 继续运行，会对同一 BUSID 启动第二个 attach 循环，
  与 Source 端烧写互相抢设备。
- **USB/IP 不能跨重枚举维持会话**：这是实测结论而非实现取舍。与其在
  USB/IP 链路上继续修补不可靠的重枚举恢复，不如把烧写放到唯一能稳定
  持有设备的 Source 端。

## Source Agent + RKDevTool（Windows Source）

Windows 端无 CLI 烧写工具（RKDevTool 仅 GUI），且 SSH 会话启动的 GUI
进程没有可见窗口（session 隔离，实测 MainWindowHandle=0），无法从
Controller 直接自动化。因此采用「文件队列式 Source Agent」
（`scripts/windows_source_agent.py`，交互桌面会话常驻，计划任务自启动，
可操作 RKDevTool GUI）：

```text
Controller (Linux)                    Windows 源主机
---------------                       --------------
1. SFTP 上传固件到        ------->    scripts/windows_source_agent.py
   C:\gms-flash\<task>\              （桌面会话常驻，计划任务自启动，
2. 投递 <task>.json 任务               可操作 RKDevTool GUI）
   到 %USERPROFILE%\gms-flash-queue
3. 轮询 <task>.result.json  <------    RKDevTool GUI 自动化烧写
4. 校验结果                           轮询自身按天滚动日志判定成败
```

流程细节：

1. Controller 通过 SSH/SFTP 连接 Windows 源主机（严格主机密钥校验，
   凭据来自平台存储的设备主机密码），SFTP 上传固件到
   `C:\gms-flash\<task>\`（task_id 形如 `flash-<device>-<时间戳>`）。
2. 投递 `<task>.json` 任务（携带固件路径与目标设备 serial）到
   `%USERPROFILE%\gms-flash-queue`（即 `C:\Users\<user>\gms-flash-queue`，
   队列目录跟随 SSH 登录用户）。Windows 源主机地址必须为 `user@host`
   格式，否则拒绝投递（fail closed，HTTP 409）——硬编码个人账户目录会
   让烧写任务投递到错误的主目录。
3. Source Agent 执行 `adb -s <serial> reboot loader`，等待 RKDevTool
   明确显示 Loader/Maskrom 后自动化烧写，按天滚动日志判定成败。
4. Controller 轮询 `<task>.result.json`（10 秒间隔，5400 秒超时）并校验
   结果（`status == "SUCCESS"` 视为成功，附带日志尾部与错误信息）。
5. 超时 / 失败不降级重试，由上层决定设备是否进入隔离状态。

文件队列解耦 GUI 自动化：避免 SSH 启动 GUI 的 session 隔离问题，且队列
文件天然提供任务幂等与结果留痕。

### AutoBind

Windows 来源要求 usbipd-win 4.2+（AutoBind policy 支持）。烧写前预检并
按需创建 AutoBind 策略，保证 Loader / MaskROM 二次枚举后设备被自动重新
共享；策略校验失败（版本过低、SSH 账号无管理员权限、策略未生效）fail
closed 拒绝烧写。

需要手工排查时，可在管理员 PowerShell 中执行：

```powershell
usbipd --version
usbipd policy list
usbipd policy add --effect allow --operation AutoBind --busid 1-1
```

策略只应授予平台已分配的专用 Android USB 端口；不要为键盘、摄像头或整类
不受控 USB 设备创建宽泛规则。策略命令及其从 4.2.0 开始提供的说明参见
[usbipd-win AutoBind policies](https://github.com/dorssel/usbipd-win/wiki/New-design%3A-policies)。

Ubuntu/Linux 来源的等价物是用户态 `usbipd` 导出进程：按持久分配的序列号
过滤导出，设备以相同序列号重新枚举时自动重连。

## 失败恢复

烧写结束（成功、失败或异常）后：

- `finally` 中对称恢复 ownership handoff 的 pause：既 resume `device_id`
  也 resume `device_host`——只 resume device 会留下 host 级 pause，
  `schedule_usbip_reconnect` 会因 host pause 拒绝调度，设备最长 2 小时
  无法自动回到平台管理。
- 需要重连时按原物理路由调度 USB/IP 重连（重新导出并 attach）。
- 烧写失败不降级重试（不在失败后自动再次下发烧写）；超时或失败的设备由
  上层决定是否进入隔离状态。烧写中途中断的设备通常停留在 Loader 或
  MaskROM 状态，恢复路径见 [troubleshooting.md](troubleshooting.md)。

## reconnect watchdog

平台为 USB/IP 设备维护通用重连 watchdog：

- 持久设备从在线视图中消失时，按其持久来源主机调度后台重连
  （重新 bind/attach），Worker 重启后也会做 reconciliation。
- 烧写模式切换（进入 RockUSB Loader）时允许 transport-only 重连：
  此时设备不提供 ADB/Fastboot，但 RockUSB Loader 是合法目标状态。
- 固件烧写 claim 期间，ownership handoff 会按 host + device 双键暂停
  watchdog（TTL 2 小时）；paused 设备仍在设备视图中可见，只是不被通用
  消失监控接管。手工断开抑制（suppression）是独立机制，两者不混用。
- 如果烧写异常路径没有对称恢复 pause，设备最长 2 小时无法自动回到平台
  管理（等待 TTL 过期后 watchdog 才会重新接管）。

## Linux 限制（fail closed）

Ubuntu/Linux 来源支持设备共享、分配与重连，但在 Linux Source 完整烧写
backend 实现之前：

- API 对 Linux 来源 USB/IP 设备的完整固件烧写直接拒绝（HTTP 409），
  **不会误送入 Windows backend**。
- 来源 OS 探测失败同样 fail closed，绝不默默走 Windows backend。

物理路由解析要求设备到 (Source Host, BUSID) 的持久分配记录存在；缺少
记录时烧写请求直接报错，需要断开后从设备管理页重新选择该 USB 设备并
连接以重建持久分配。

## Troubleshooting（烧写相关速查）

| 现象 | 处理 |
|---|---|
| `USB/IP 所有权交还源主机失败`（HTTP 409） | 查看 error 中的具体 stage：端口查询失败或目标端口仍存在（假 detach）。确认 Worker 侧 `usbip port` 可用、`sudo -n usbip detach` 可执行后重试。 |
| `USB/IP固件烧写缺少设备到物理BUSID的持久分配记录` | 断开该设备后从设备管理页重新选择该 USB 设备并连接，重建持久分配。 |
| `Windows 源主机地址必须为 user@host 格式` | 设备来源主机地址补齐 SSH 用户名（如 `gms@192.0.2.10`）。 |
| `未找到 Windows 源主机 <host> 的 SSH 凭据` | 在平台中录入该 Windows 来源主机的 SSH 密码。 |
| Source Agent 烧写超时（5400s） | 检查 Windows 源主机上 Source Agent 是否在交互桌面会话中运行、`%USERPROFILE%\gms-flash-queue` 下任务与结果文件、Agent 按天滚动日志；确认 RKDevTool 可正常启动且无人值守。 |
| AutoBind 策略校验失败 | 管理员 PowerShell 执行 `usbipd --version`（需 4.2+）与 `usbipd policy list` 核对；SSH 账号需具有执行策略命令的管理员权限。 |

更完整的排障清单见 [troubleshooting.md](troubleshooting.md)。

## 安全考虑

- 固件烧写属于破坏性操作。Web 侧受用户权限、设备 Claim、Worker 状态与
  操作审计约束。
- Agent 调用必须使用 Agent Service Token，并提供与目标设备集合、固件
  SHA-256、`wipe_data`、`burn_mode` 精确绑定的一次性 approval token；
  令牌只能消费一次，不能跨固件、设备或参数复用。
- Controller 到 Windows 源主机的 SSH 使用严格主机密钥校验（known_hosts +
  Reject 未知主机），避免烧写链路被中间人劫持。
- 烧写任务中的设备 serial 经字符白名单校验后才会进入 Windows 侧命令
  插值，杜绝 shell 元字符逃逸。
- Windows 来源主机 SSH 账号必须具有执行策略命令的管理员权限；usbipd-win
  AutoBind 策略只授予专用 Android USB 端口，不创建宽泛规则。
- 不要为烧写链路放宽主机密钥校验或在来源主机上持久保存明文凭据。

## 相关文档

- [docs/architecture/adr/0005-usbip-firmware-ownership.md](../architecture/adr/0005-usbip-firmware-ownership.md)
- [docs/usbip/overview.md](overview.md)
- [docs/usbip/troubleshooting.md](troubleshooting.md)
- [docs/deployment/](../deployment/)
