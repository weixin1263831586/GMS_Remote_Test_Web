# ADR 0023：AOA 直接触摸屏与稳定的 HID 设备集合

- Status: Accepted
- Date: 2026-11-04
- Related: [ADR 0022](0022-web-aoa-recording-and-usb-recovery.md)

## Context

录制使用 ADB 输入，回放使用 AOAv2 HID。目标设备在回放期间出现 Mouse 设置条目，
会让 System 页中的 Developer options 位置偏离录制时的坐标。

原触摸描述符使用顶层 Physical Collection，缺少明确的 Touch Screen Application
与 Contact Identifier。Android 在没有直接输入属性等信息时默认采用 pointer 分类，
因此不能仅凭 Usage 名为 Touch Screen 就认定设备会被当成直接触摸屏。
此外，回放原先固定注册触摸、键盘、系统键三个设备，使不使用键盘的脚本也引入外接键盘。

## Decision

1. 触摸描述符使用 Digitizer / Touch Screen 的顶层 Application Collection，内部
   使用 Finger Logical Collection、In Range、Tip Switch、Contact Identifier 和
   绝对 X/Y。Contact Identifier 使标准 Linux HID 扫描选择 hid-multitouch；该驱动
   对 Touch Screen Application 使用直接触摸语义。
2. 保持现有五字节报文和 360×640 坐标。首字节低两位仍表达 in-range/tip 状态，
   原六位填充区声明为 Contact Identifier，单指输入固定为 0；X/Y 的偏移不变。
3. 在执行任何输入前，按整段解析后的命令冻结所需 HID 集合：click/longClick/swipe
   使用触摸，key/write 使用键盘，wake/home/back 使用系统键，sleep 不需要输入设备。
   不在某一条命令前临时增加键盘设备；USB 重连后重建同一集合。
4. 标定上下文升级到 version 2。前端不加载 version 1 的缓存，服务端拒绝复用旧版
   映射；旧 Profile 的命令和 metadata 仍保留，不自动修改用户坐标。
5. 不通过开启目标设备的开发者设置或隐藏 ROM 设置条目规避分类问题，不自动重放
   不确定的触摸输入。沿用账号、设备租约、物理端口与 fencing 边界。

## Consequences

Controller、Worker 与前端需要匹配更新，并开始新的 AOA 会话以注册新描述符。
以前按旧指针映射实测的标定需要重做；五字节报文及命令文本格式保持兼容。

这项修改修正协议声明，并减少不必要的外接输入设备，不是目标 ROM 界面稳定性的
硬件验收结论。目标内核需要支持相应的 HID 驱动。若目标板仍出现 Mouse 条目，
应在 ADB 可用时只读检查输入设备的 sources、直接输入属性与 ROM 配置，再确认
实际布局；不能只偏移第 53 条坐标就宣称修复。

软件回归解析真实描述符并解码按下/抬起报文，验证集合与位偏移、按需注册、重枚举
后恢复相同通道，以及旧版标定拒绝。最终仍需在目标板确认触摸效果和 System 页面。

## References

- [Android 触摸设备分类](https://source.android.com/docs/core/interaction/input/touch-devices)
- [Linux v6.1 HID 扫描实现](https://github.com/torvalds/linux/blob/v6.1/drivers/hid/hid-core.c)
- [Linux v6.1 hid-multitouch 实现](https://github.com/torvalds/linux/blob/v6.1/drivers/hid/hid-multitouch.c)
- [协议与解析实现](../../../foundation/aoa_profile.py)
- [HID 注册与 USB 会话](../../../worker_agent/aoa_session.py)
