# ADR 0022：Web AOA 交互录制与无 ADB USB 恢复

- Status: Accepted
- Date: 2026-11-02
- Related: [ADR 0001](0001-controller-worker-boundary.md)、[ADR 0010](0010-actor-resource-owner-identity.md)、[ADR 0021](0021-ats-execution-options-and-evidence-retention.md)

## Context

User build 刷机后 USB 调试通常关闭，设备不会出现在 ADB 清单里。平台需要在
userdebug 录制机上把真实 UI 操作转换为 AOAv2 HID 命令，保存为账号隔离的配置，
再对同机型目标机回放。浏览器不能绕过现有 Worker 归属、设备租约和执行审计直接
访问 USB。

## Decision

1. 设备管理提供三列 AOA 工作台：左侧复用截图投屏并采集点按、长按、滑动、系统键
   和文本输入；中间管理录制、Profile 与目标回放；右侧展示并编辑等价 AOA 命令。
   页面打开只读取 Profile，不自动发送设备输入或启动标定。
2. 录制输入是白名单化的 `aoa_record_input` device action。Controller 在 Foundation
   校验结构和上限，经 Cluster device-action 的 ownership/claim/fencing 边界派发；
   Worker 用 argv 形式的 ADB 输入操作 userdebug 录制机，成功后才返回等价命令。
3. 触摸 HID 使用 360×640 逻辑坐标。默认按显示分辨率换算；实测标定会切换 USB
   模式并点击屏幕中心，因此只能由用户在明确提示后触发，不能在初始化或普通录制时
   隐式执行。
4. Profile 按 ResourceOwner 隔离，最多 64 条命令并受 300 秒估算预算约束。每个
   交互动作默认附加 800ms 回放节奏；导入内容在保存和回放边界再次校验。
5. Worker 心跳除 ADB/Fastboot 外，还从 sysfs 保留带序列号和 vendor-specific 接口的
   `usb_only` Android 候选。该状态只允许 AOA action，禁止截图、ADB Shell 和普通 UI
   操控，使“USB 调试已关闭”不会让恢复目标从平台消失。
6. AOAv2 回放仍使用 Worker 本地 pyusb，不新增 Controller 到 Worker 的 shell 通道。
   正常和异常路径都注销 HID、复位 USB 并释放资源；ADB 是否恢复作为独立结果返回。

## Consequences

Controller Web 与目标 Worker 都需部署本版本。旧 Worker 没有
`aoa_record_input_v1`/`aoa_usb_hid_v1` 能力时不能使用交互录制或 USB HID 回放。
AOA 回放期间截图会因 ADB 暂时消失而不可用，页面必须展示这一状态；若恢复为
`unauthorized`，仍需用户在设备上确认调试授权。
