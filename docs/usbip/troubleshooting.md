# USB/IP 常见问题排查

本文档定位：汇总 USB/IP 接入与固件烧写链路的常见故障（attach 失败、假 detach、reconnect 抖动、烧写中断恢复等）与排查路径。命令均为平台实际使用或在实机上可直接执行的命令。

## attach 失败

**现象**：Worker 侧 `usbip attach` 后设备没有出现在本地 USB 列表，或
`lsusb` 看不到对应设备。

排查路径：

- 确认 Worker 侧 `vhci_hcd` 已加载（平台 attach 前会检查/确保驱动加载）。
- 确认来源主机 3240 端口可达（平台在接入时会做网络可达性检查；
  `usbipd list` 在 Windows 上需要 PTY 才返回完整设备表，手工排查时建议
  在交互会话中执行）。
- usbipd-win 已知行为：在驱动切换后可能接受 attach（命令返回 0）却在
  vhci 完成 USB 枚举前立即释放会话。实机经验是同一 BUSID 后续再次
  attach 即可稳定；平台只对这种“命令成功但端口未稳定”的目标重试，
  确定性命令失败不会反复执行。
- Worker 侧只清理本次要 attach 的 (host, busid) vhci 端口；同一来源主机
  上其他设备的 USB/IP 会话不会被连带 detach。如果排障时手工 detach，
  不要拆掉其他设备的端口。
- Windows 来源要求 usbipd-win 4.2+（AutoBind policy 支持），来源主机
  SSH 账号需具有执行策略命令的管理员权限。

## 假 detach

**现象**：Worker 侧执行 `usbip detach` 后端口“看起来”已释放，但 Source
端拿不到物理设备，烧写或直连操作失败。

这是实机验证过的真实故障模式，平台的处理是 **fail closed**：

- detach 后再次查询 `usbip port` 复核目标端口已消失；查询失败或端口仍
  存在一律视为未释放，烧写以 HTTP 409 中止。
- 固件烧写只有在确认 Source Host 物理持有设备后才进入 SOURCE_OWNED
  状态下发烧写。

手工排查：

```bash
usbip port                      # 确认目标 host/busid 端口已消失
sudo -n usbip detach -p <port>  # 平台使用的 detach 形式
```

- 若 `usbip port` 输出无法解析，平台按未释放处理——不要在端口状态
  未知时强行下发源端烧写。
- detach 后等待约 2 秒让 Source 端 PnP 重新认领设备；Source 端仍看不到
  设备时，检查 Windows 设备管理器或 Linux `lsusb`。

## reconnect 抖动

**现象**：设备在模式切换（ADB ↔ Fastboot ↔ RockUSB Loader）期间反复
消失/出现，或重连循环频繁触发。

背景与排查：

- 设备重枚举后 USB 身份可能变化：Rockchip Loader 的 VID 固定为 `2207`，
  PID 随 SoC 变化（如 `2207:351a`）。平台同时识别 `Rockusb Device` 标记
  并优先重挂载原 BUSID。若新增 SoC/PID 导致重连失败，在
  `configs/local/config.json` 的 `usbip_vid_pids` 中补充该身份。
- 设备在 ADB/Fastboot/Fastbootd/Loader 之间切换时，平台按来源主机和
  BUSID 自动重新 bind/attach；重枚举后的短暂不可见是预期行为，watchdog
  会在设备从持久视图中消失后调度重连。
- 烧写模式切换允许 transport-only 重连：此时设备处于 RockUSB Loader、
  不提供 ADB/Fastboot 是合法状态，不代表重连失败。
- 烧写 claim 期间 watchdog 被按 host + device 双键暂停（TTL 2 小时），
  paused 设备仍可见但不会被通用消失监控接管；这是防止“第二个 attach
  循环与烧写互相破坏”的设计，不是故障。若烧写结束后设备长时间不回归，
  检查是否留下未恢复的 host 级 pause（正常路径会在 `finally` 中对称
  resume）。
- 手工断开抑制（suppression）与烧写 pause 是两套独立机制；手工断开后
  短时间内不自动重连属预期。

## 烧写中断恢复

**现象**：源端烧写超时（5400s）、失败或中途断电/拔线，设备停留在中间
状态。

- 平台对失败**不降级重试**：超时/失败的烧写不会自动再次下发，由上层
  决定设备是否进入隔离状态。
- 烧写失败路径同样会调度 USB/IP 重连，让设备回到平台管理；设备通常
  停留在 Loader 或 MaskROM 状态。
- 恢复路径：
  1. Source 端确认设备当前 USB 状态（Windows 设备管理器 / `usbipd list`，
     或 Linux `lsusb`），Rockusb 设备通常显示 `Rockusb Device` 标记。
  2. 若设备卡在 Loader/MaskROM，可重新发起一次完整固件烧写（平台按
     原物理路由恢复导出后重走所有权状态机），或在 Source 端用 RKDevTool
     手工完成烧写/复位。
  3. 若烧写已完成但设备未回到平台：确认烧写结束路径已恢复 pause 并按
     原物理路由重新导出与 attach；异常中断的场景最长等待 2 小时 TTL
     过期后 watchdog 重新接管，或直接在设备管理页重新连接该 USB 设备。

## 其他常见问题

| 现象 | 原因与处理 |
|---|---|
| `USB/IP固件烧写缺少设备到物理BUSID的持久分配记录` | 设备缺少到 (Source Host, BUSID) 的持久分配。断开后从设备管理页重新选择该 USB 设备并连接。 |
| Windows 源主机地址报错要求 `user@host` 格式 | 烧写队列目录跟随 SSH 登录用户，必须携带显式用户名。 |
| `未找到 Windows 源主机 <host> 的 SSH 凭据` | 在平台中录入该来源主机的 SSH 密码。 |
| Source Agent 不消费任务 | 确认 Agent 在**交互桌面会话**中常驻（SSH 会话启动的 GUI 无可见窗口）、计划任务自启动已配置；检查 `%USERPROFILE%\gms-flash-queue` 与按天滚动日志。 |
| AutoBind 策略校验失败 | 管理员 PowerShell 执行 `usbipd --version`（需 4.2+）、`usbipd policy list` 核对策略；SSH 账号需管理员权限。 |
| Linux 来源 USB/IP 设备烧写被拒（HTTP 409） | 预期行为（fail closed）：Linux Source 完整烧写 backend 落地前不支持，不会误送入 Windows backend。 |
| 来源 OS 探测失败导致烧写被拒 | 预期行为（fail closed）：探测失败绝不默认走 Windows backend，修复来源主机 SSH 可达性后重试。 |

## 安全注意

- 排障时不要为绕过校验而放宽 SSH 严格主机密钥校验，也不要把 AutoBind
  策略放宽到非专用 USB 端口或整类设备。
- 固件烧写是破坏性操作；重试同样需要全新的一次性 approval token，旧
  token 不能复用。

## 相关文档

- [docs/usbip/overview.md](overview.md)
- [docs/usbip/firmware-flashing.md](firmware-flashing.md)
- [docs/usbip/adb-proxy-vs-usbip.md](adb-proxy-vs-usbip.md)
- [docs/architecture/adr/0005-usbip-firmware-ownership.md](../architecture/adr/0005-usbip-firmware-ownership.md)
- [docs/deployment/](../deployment/)
