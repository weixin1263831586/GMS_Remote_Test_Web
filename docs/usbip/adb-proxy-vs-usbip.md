# ADB Proxy 与 USB/IP 取舍对比

本文档定位：在只需 ADB 与需要完整 USB 链路两类场景之间，对比 ADB Proxy（`adbproxy-rs`）与 USB/IP 两种远程接入方式，给出选型边界与结论。

## 两者转发的是什么

| | ADB Proxy（`adbproxy-rs`） | USB/IP |
|---|---|---|
| 转发层级 | 按设备代理 ADB Server（5037）协议 | USB 协议层，转发整个物理设备 |
| 来源端组件 | `adbproxy-rs` agent | Windows：usbipd-win；Linux：用户态 `usbipd` |
| 数据通道 | gRPC / TCP | USB/IP TCP 3240 |
| Worker 侧形态 | adb client → adbproxy → device | vhci_hcd 枚举成本地 USB 设备 |

```text
ADB Proxy:
Android Device (USB) → Source Host (adbproxy-rs agent) → gRPC/TCP → Linux Worker (adb client)

USB/IP:
Android Device (USB) → Source Host (usbipd-win / linux usbip) → USB/IP TCP 3240 → Linux Worker (vhci_hcd → 本地 USB)
```

## 能力对比

| 维度 | ADB Proxy | USB/IP |
|---|---|---|
| ADB 操作 | 支持 | 支持 |
| 设备级选择/授权/租约 | 支持（比端口转发更灵活） | 支持（BUSID 分配 + 物理设备池化） |
| CTS/GTS/VTS/STS | 支持 ADB/测试场景及平台设备租约 | 完整支持，推荐 |
| Recovery | 仅支持仍提供 ADB 的 Recovery | 支持 |
| Fastboot / Fastbootd | 不支持 | 支持 |
| RockUSB Loader / MaskROM | 不支持 | 支持 |
| 固件/GSI 烧写 | 不支持 | 支持（完整烧写在物理 Source 端执行） |
| Bootloader OEM 锁定/解锁 | 不支持 | 支持 |
| 系统重启 / USB 重枚举 | 有限，ADB 消失后代理链路中断 | 支持，平台按来源与 BUSID 自动重连 |
| 多设备 / 多 Worker | 强，设备级选择、授权和租约 | 强，BUSID 分配和物理设备池化 |
| 来源 Agent 平台 | 当前仅发布 Linux x86_64 | Windows（usbipd-win 4.2+）与 Linux（用户态 usbipd） |

## 取舍要点

- **转发层级决定能力边界**：ADB Proxy 代理的是 ADB 协议层，设备一旦离开
  ADB 模式（Fastboot/Loader/Recovery 无 ADB），代理链路即中断；USB/IP 转发
  的是 USB 协议层，Worker 侧看到完整本地 USB 设备，设备在 ADB / Fastboot /
  Fastbootd / Recovery / RockUSB Loader 之间切换时可被重新 bind/attach。
- **租约机制独立于传输协议**：平台设备占用/任务租约与传输协议独立，
  ADB Proxy 和 USB/IP 都会受到租约保护。页面中的“锁定设备/解锁设备”指
  Bootloader OEM 锁定，只有完整 USB 通道（USB/IP 或本地 USB）支持。
- **来源 Agent 可用性**：当前 ADB Proxy 来源 Agent 仅发布 Linux x86_64
  版本；Windows 设备来源应使用 USB/IP。
- **模式切换与自动恢复**：USB/IP 依赖平台保存来源 BUSID 并在模式切换时
  自动 bind/attach；`usbipd-win` 的 attach 不是持久连接，设备重启或切换
  USB 身份后需重新 attach，由平台自动完成。

## 选型结论

- 只需要 ADB 命令、不涉及 USB 模式切换的测试：可使用 **ADB Proxy**。
- 需要 Fastboot、Fastbootd、Recovery、RockUSB Loader、固件/GSI 烧写或
  Bootloader OEM 锁定：必须使用 **USB/IP**（或本地 USB），不能回退到网络
  ADB、5037 端口转发或 ADB Proxy。

## 相关文档

- [docs/usbip/overview.md](overview.md)
- [docs/usbip/firmware-flashing.md](firmware-flashing.md)
- [docs/usbip/troubleshooting.md](troubleshooting.md)
- [docs/architecture/adr/0005-usbip-firmware-ownership.md](../architecture/adr/0005-usbip-firmware-ownership.md)
- [docs/deployment/](../deployment/)
