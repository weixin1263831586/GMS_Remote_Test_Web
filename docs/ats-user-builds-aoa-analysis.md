# ATS User Builds / WebAOA 在 GMS 平台的接入分析

分析日期：2026-10-31。本文描述接入位置、边界和验收条件；AOA 相关工作区源码的存在
不等于完整流程已经交付。本批 ATS 运行管理回归不构成 AOA 真机验收。

## 结论

值得借鉴的是 **刷机后恢复 USB 调试** 这一环。应由实际连接设备的 Worker 执行 AOA，
在刷机完成后、ADB 属性校验前完成恢复，再进入现有测试流程。

ATS 官方流程是先用 WebAOA 录制操作序列，再配置 `AoaTargetPreparer`，放在刷机动作之后。
它利用 AOA 输入能力，在尚未启用 ADB 时操作设备设置。
[ATS User Builds 官方说明](https://source.android.com/docs/core/tests/development/android-test-station/ats-user-builds)

## 现场 WebAOA 页面说明了什么

只读访问 [现场 WebAOA](http://172.16.14.246:8000/webaoa) 时，页面显示
“A secure context (localhost or HTTPS) is required.”；此 HTTP 局域网地址当前无法进入
正常设备操作界面。读取其 JS 可见 `navigator.usb.requestDevice()`、`getDevices()` 和
USB vendor control transfer 调用，没有操作设备或修改 ATS 服务。

WebUSB 访问浏览器主机连接的 USB；网站部署在 ATS 主机并不会让浏览器获得该主机的 USB。
因此访问 HTTPS 或通过 localhost 隧道打开页面可以解决安全上下文要求，但不能自动把
浏览器设备选择器改成远程 Worker 设备选择器。首次申请设备访问还需要用户点击授权。
[Chrome WebUSB 文档](https://developer.chrome.com/docs/capabilities/usb)

| 使用位置 | 适合的用途 | 与 GMS 的关系 |
| --- | --- | --- |
| 工程师电脑的 WebAOA，设备接该电脑 | 录制、人工调试、导出命令 | 导出后导入平台中的设备恢复配置 |
| Worker 本地浏览器，设备接该 Worker | 现场标定命令 | 可以沿用 ATS 工具辅助首次适配 |
| GMS 远程网页，设备接机房 Worker | 远程执行与观察 | 页面发起有类型的操作，Worker 执行 USB 输入 |

直接嵌入 ATS WebAOA 页面不能完成第三种用途。远程录制器需要 Worker 输入执行器与
独立画面源，后续再做；第一版可先支持导入已有录制脚本。

## 协议和兼容性

AOAv2 提供 HID 输入，通过 USB endpoint 0 的控制请求注册 HID、提交描述符、发送输入、
注销 HID。设备需要实际支持该协议，不能仅按 Android 版本推定可用。
这条链路没有提供屏幕视频回传；画面应另用摄像头、HDMI 等方式，ADB 可用后才可切回
原有截图或 scrcpy 路径。[AOAv2 协议](https://source.android.com/docs/core/interaction/accessories/aoa2)

录制命令至少兼容 WebAOA 的 `click`、`swipe`、`write`、`key`、`wake`、`home`、`back`、
`sleep`。参数需要按语法解析成结构化动作，不能拼成 shell 命令。`swipe` 的时长与
`sleep` 使用毫秒；例如 `swipe 0 0 100 360 640` 表示 100 ms。
`AoaTargetPreparer` 还列有 `longClick`，导入器应明确支持或明确拒绝。
[TargetPreparer API](https://source.android.com/reference/tradefed/com/android/tradefed/targetprep/AoaTargetPreparer)

## GMS 的准确接入点

`features/automation/executors.py` 中 `HttpAutomationExecutor.flash()` 在收到烧写命令
完成后调用 `_verify_cluster_post_flash()`；后者通过 ADB 获取启动完成状态、产品和指纹。
user build 的 USB 调试关闭时，单纯增加这里的 ADB 重试次数无法恢复设备。

建议执行顺序：

```text
保留设备租约并记录物理 USB 身份
  → 烧写完成
  → 检查同一物理设备的 ADB 是否已经可用
  → 必要时执行绑定版本的 AOA 恢复配置
  → 等待 ADB online / 处理授权状态
  → 现有 boot_completed、产品与 fingerprint 校验
  → 测试前准备 → Tradefed 测试 → 归档报告
```

本轮新增的保持唤醒、关闭动画、开启 Wi-Fi 是 **ADB 已可用** 后的准备动作，不能承载
前面的恢复阶段。AOA 也不能仅放进测试启动后的 TargetPreparer，而让现有 ADB 入场校验
继续挡在它前面。

## 建议的数据与执行契约

以下为设计要求，字段名和路由应在独立实现中定稿。

| 对象 | 必要信息 |
| --- | --- |
| 恢复配置 | ID、版本、内容哈希、命令列表、总预算、适配的产品/Android 版本/画面方向/语言 |
| 设备绑定 | Worker ID、USB 物理端口路径、VID/PID 与 USB serial 快照、租约 generation |
| 运行快照 | 精确配置版本/哈希、命令 ID、执行状态、开始/结束时间、最后成功步骤 |
| 结果 | AOA 能力、动作状态、ADB 状态、失败分类、随后启动/固件校验结果 |

刷机和 USB 模式切换可能改变 PID、设备地址或序列号；不能只按 ADB serial 找设备，
更不能选择第一台匹配 USB 设备。必须在烧写前绑定物理拓扑，并在重枚举后确认同一目标。
第一版限 Worker 本机直连 USB；ADB Proxy 不传输 USB vendor/HID 控制，应明确拒绝。
USB/IP 需要验证重枚举、交还与重新认领，不能默认与本地 USB 等价。

Controller 仍通过现有审计 SSH 边界发送类型化设备动作；Worker 经带 Token 的 HTTP(S)
回报状态和证据。沿用当前人类身份/提权、设备所有权、租约与 fencing 校验，不增加
任意 USB 控制请求代理或通用 shell 通道。长动作必须异步执行并继续心跳和续租。

## 恢复、授权与失败处理

1. 执行前检查 ADB：已可用就跳过 AOA，避免重放点击把开关再次关闭。
2. 一次恢复绑定一个命令 ID 和配置哈希。重启后先查询状态和设备状态，再决定继续；
   命令结果不明不能直接从头无限重放。不可证明安全的中断应停止并保留证据。
3. USB 调试开启与 ADB RSA 授权分开处理。`unauthorized` 需要设备信任当前 Worker 的
   ADB key；不能把“脚本点完”或“看到了 serial”算作成功。
4. 恢复后仍检查目标物理身份、ADB `device`、系统启动完成和预期固件指纹。
5. 限制命令数、坐标范围、每步等待与总时长。输入按键和写入文本纳入内容校验，日志
   不记录凭据。脚本只适配已知的初始界面，不宣称可跨 OEM、分辨率和语言通用。
6. 不支持设备协议、USB 无权限、目标不明确、设备重枚举超时、ADB 未授权、指纹不符
   应分别报告。保留执行轨迹，不把输入发送成功当成设置成功。

适用设备是实验室中具备操作权限且处于已知状态的测试机。AOA 输入不能保证解决
锁屏、FRP 或企业策略；这些状态应在设备准备规范中明确处理。

## 实施顺序与验收

| 阶段 | 交付 | 验收重点 |
| --- | --- | --- |
| 1 | Worker 协议探测、导入命令、限时执行、证据 | 一台明确绑定的直连设备上关闭 ADB 后仍可发送输入；不操作旁边设备 |
| 2 | 配置版本、刷机后恢复阶段、重启恢复 | user build 刷机后无需 ADB 前置即可恢复，再通过原有固件校验启动测试 |
| 3 | 远程命令编辑/录制与画面 | 操作的是所选 Worker 的设备，录制结果绑定画面规格和配置版本 |

软件测试应覆盖：坏脚本拒绝、总预算、USB 断开、未授权、租约过期、所有权变化、
重复命令和重启恢复。真机至少覆盖：ADB 已可用时跳过、清刷后恢复、USB 模式重枚举、
脚本与语言/方向不匹配、两台相同机型并接、失败后不误启动测试。

当前未对现场设备执行 AOA 输入、刷机或修改 USB 调试状态；能否在具体 Rockchip 板型
自动完成，仍需按上述矩阵标定与验收。运行管理功能的使用说明见
[GMS ATS 优化](automation-ats-improvements.md)。
