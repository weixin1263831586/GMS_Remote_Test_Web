# USB/IP 接入总览

本文档定位：概述平台支持的四种 Android 设备远程接入方式（网络 ADB、SSH ADB 端口转发、ADB Proxy、USB/IP）的定位、能力边界与选型结论，并说明平台优先采用 USB/IP 的原因。固件烧写细节见 `firmware-flashing.md`，方案取舍对比见 `adb-proxy-vs-usbip.md`。

## 背景：为什么“远程能看到 ADB”不够

GMS 自动化测试不只执行 `adb shell`。测试过程中设备可能进入 Recovery、
Bootloader Fastboot、Fastbootd 或 RockUSB Loader；固件/GSI 烧写还要求测试
主机能够持续访问同一个物理 USB 设备。因此“远程能够看到 ADB”不等于覆盖
完整的 GMS 测试和烧写链路。

CTS / GTS / VTS / STS 的 Host Side 测试、Tradefed 子进程以及 Fastboot /
Recovery 等场景通常要求测试主机能够看到**完整的本地 USB 设备**，这就是
平台优先采用 USB/IP、而不是把设备切换为 `adb tcpip 5555` 的原因：USB/IP
在 USB 协议层转发整个物理设备，尽量保持测试主机侧的 USB 语义，兼容需要
本地 ADB Server、Fastboot 或 USB 枚举的测试流程。

## 四种远程接入方案

按适用场景递进：

### 方案一：网络 ADB（`adb tcpip`）

```text
Android Device (adbd TCP:5555)
    │ TCP/IP
    ▼
Linux Worker (adb connect <ip>:5555)
```

- 设备端 `adbd` 切换到 TCP 模式，Worker 通过 `adb connect` 直接访问。
- **限制**：依赖设备端 `adbd` 进程和网络栈；设备重启、进入 Fastboot/Loader
  后 TCP 端口消失，ADB 链路中断且无法自动恢复。不支持 Fastboot、Recovery、
  RockUSB Loader 及固件烧写。
- **适用**：临时调试、不涉及 USB 模式切换的简单 `adb shell` 场景。

### 方案二：SSH ADB 端口转发

```text
Android Device (USB)
    │ USB
Source Host (adb server :5037)
    │ SSH Tunnel (-L 5037:localhost:5037)
    ▼
Linux Worker (ADB over forwarded 5037)
```

- 通过 SSH 隧道转发来源主机的 ADB Server 端口（通常 5037），Worker 侧
  `adb` 命令经隧道到达来源端 ADB Server。
- **限制**：来源端 ADB Server 是单点；5037 端口被独占时多设备互斥。设备
  离开 ADB 模式后链路中断，来源端 ADB Server 必须仍能看到设备才能恢复。
  不支持 Fastboot、Loader 和固件烧写。
- **适用**：兼容旧部署、无需 Fastboot 的远程 ADB 调试。

### 方案三：ADB Proxy（`adbproxy-rs`）

```text
Android Device (USB)
    │ USB
Source Host (adbproxy-rs agent)
    │ gRPC / TCP
    ▼
Linux Worker (adb client → adbproxy → device)
```

- 按设备粒度代理 ADB Server 协议，支持设备级选择、授权和租约，比端口转发
  更灵活。ADB Proxy 来源 Agent 当前仅发布 Linux x86_64 版本。
- **限制**：代理的是 ADB 协议层，设备离开 ADB 模式（Fastboot/Loader）后
  代理链路中断。不支持 Fastboot、Loader 和固件烧写。
- **适用**：ADB-only 测试场景和跨 Linux Worker 的设备共享。

### 方案四：USB/IP（推荐，全链路）

```text
Android Device (USB)
    │ USB
Source Host (usbipd-win / linux usbip)
    │ USB/IP TCP 3240
    ▼
Linux Worker (vhci_hcd → 本地 USB 设备)
    ├── adb server
    ├── fastboot
    ├── upgrade_tool (RockUSB Loader)
    └── Tradefed / CTS / GTS / VTS / STS
```

- 在 USB 协议层转发整个物理设备，Worker 侧看到的是完整的本地 USB 设备。
  设备在 ADB / Fastboot / Fastbootd / Recovery / RockUSB Loader 之间切换时，
  平台按来源主机和 BUSID 自动重新 bind/attach，保持测试主机对同一物理设备
  的持续访问。
- **适用**：GMS 全链路测试、固件/GSI 烧写、Bootloader OEM 锁定/解锁、
  企业设备池化管理。

## 对比总结

| 对比维度 | 网络 ADB (`adb tcpip`) | SSH ADB 端口转发 | ADB Proxy (`adbproxy-rs`) | USB/IP |
|---|---|---|---|---|
| 转发层级 | 设备 `adbd` TCP 端口 | ADB Server（通常 5037） | 按设备代理 ADB Server 协议 | USB 协议/物理设备 |
| ADB 操作 | 支持 | 支持 | 支持 | 支持 |
| CTS/GTS/VTS/STS | 仅适合不切换 USB 模式的用例 | 部分支持，受单一 ADB Server 和端口约束 | 支持 ADB/测试场景及平台设备租约 | 完整支持，推荐 |
| Recovery | 网络重置后通常失联 | 仅在来源 ADB Server 仍可见时可用 | 仅支持仍提供 ADB 的 Recovery | 支持 |
| Bootloader Fastboot / Fastbootd | 不支持 | 不支持 | 不支持 | 支持 |
| RockUSB Loader / MaskROM | 不支持 | 不支持 | 不支持 | 支持 |
| 系统重启及 USB 重新枚举 | 弱，依赖网络和 `adbd` | 有限 | 有限，ADB 消失后代理链路中断 | 支持，平台按来源与 BUSID 自动重连 |
| 固件/GSI 烧写 | 不支持 | 不支持 | 不支持 | 支持 |
| Bootloader OEM 锁定/解锁 | 不支持 | 不支持 | 不支持 | 支持 |
| 多设备/多 Worker | 弱，需自行管理端口 | 受 5037 单端口模型约束 | 强，支持设备级选择、授权和租约 | 强，支持 BUSID 分配和物理设备池化 |
| 推荐用途 | 临时调试 | 兼容旧部署 | ADB-only 测试和跨 Linux Worker 共享 | GMS 全链路、烧写和企业设备池 |

## 选型结论

- 只需要 ADB 命令或不发生模式切换的测试，可使用 ADB Proxy。当前
  ADB Proxy 来源 Agent 仅发布 Linux x86_64 版本；Windows 设备来源应使用
  USB/IP。
- 需要 Fastboot、Fastbootd、Recovery、RockUSB Loader、固件/GSI 烧写或
  Bootloader OEM 锁定时，必须使用本地 USB 或 USB/IP，不能回退到网络 ADB、
  5037 端口转发或 ADB Proxy。
- 平台设备占用/任务租约与传输协议独立，ADB Proxy 和 USB/IP 都会受到租约
  保护；页面中的“锁定设备/解锁设备”指 Bootloader OEM 锁定，只有完整 USB
  通道支持。
- `usbipd-win` 的 attach 不是持久连接；设备重启或切换 USB 身份后需要重新
  attach。平台会保存来源 BUSID，并在 ADB/Fastboot/Fastbootd/Loader 切换时
  自动 bind/attach。参见 [usbipd-win 连接说明](https://github.com/dorssel/usbipd-win#connecting-devices)。

## USB/IP 身份识别与重枚举

Rockchip Loader 的 PID 会随 SoC 改变，VID 固定为 `2207`。例如当前 RK3572
Loader 枚举为 `2207:351a / Rockusb Device`。部署可在 `configs/local/config.json`
配置需要显式识别的身份：

```json
{
  "usbip_vid_pids": [
    "2207:0006",
    "18d1:4d00",
    "2207:351a"
  ]
}
```

平台还会识别 `Rockusb Device` 标记，并在烧写模式切换时优先重挂载原 BUSID，
降低新增 SoC/PID 导致重连失败的风险。RockUSB 模式和不同 SoC 使用不同 PID
的说明见 [Rockchip Rockusb 文档](https://opensource.rock-chips.com/wiki_Rockusb)。

## 平台已覆盖的 USB/IP 能力

- 来源设备发现
- BUSID 选择
- Windows bind
- Linux attach / detach
- `vhci_hcd` 检查
- ADB / Fastboot / Recovery 探测
- Assignment 状态跟踪
- Worker 重启后的恢复与 reconciliation
- 网络可达性检查
- 错误分类与清理流程

## 相关文档

- [docs/architecture/adr/0005-usbip-firmware-ownership.md](../architecture/adr/0005-usbip-firmware-ownership.md)
- [docs/usbip/firmware-flashing.md](firmware-flashing.md)
- [docs/usbip/adb-proxy-vs-usbip.md](adb-proxy-vs-usbip.md)
- [docs/usbip/troubleshooting.md](troubleshooting.md)
- [docs/deployment/](../deployment/)
