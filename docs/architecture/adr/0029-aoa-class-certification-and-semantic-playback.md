# ADR 0029：AOA 设备类别认证与语义回放

- Status: Accepted
- Date: 2026-11-05
- Supplements: [ADR 0027](0027-aoa-validated-mapping-boundaries.md)
- Partially supersedes: [ADR 0028](0028-aoa-firmware-bound-recovery-and-strict-environment.md) 的标准 direct 路径禁用约束；原有校准、身份与固件约束继续有效。

## Context

单机仿射拟合不能证明 Android 将外接 HID 归类为直接触摸，也不能证明同类设备可复用标准映射。
固定坐标的发送成功回执无法确认页面跳转。USB 调试未开启时，ADB 无法提供页面结构，必须另有可验证观察通道。

## Decision

1. 类别认证从已完成、属于当前账号、带 fencing 租约的 `verify_direct` Worker 回执签发。
   至少两台不同物理序列号，原命令目标和 USB 端口必须与证据一致；不接受客户端上传证据或 `certified=true`。
   每台参考机在专用安全 Activity 上测量七点，保留原始 getevent/InputReader 输出。
2. 校验新注册输入设备的 INPUT_PROP_DIRECT、ABS_MT 轴 0..360/0..640、SOURCE_TOUCHSCREEN、
   DeviceType、显示 ID/uniqueId/viewport 和标准变换误差。五点与两个独立点必须全部匹配唯一标准方向，
   不能用任意仿射修正获得 `direct_standard`。
3. 认证按账号持久化并可撤销；设备类键包括型号、Android 版本、内核、board、hardware 和输入驱动。
   固件指纹、显示尺寸、密度、旋转、路由及显示 ID 只接受已有实测用例。相同用例取参考机中最大误差。
   描述符摘要改变后，旧认证失效。新派发重新查认证；已经接受并持有租约的操作使用冻结证明，撤销不会暗中中断刷机。
4. 标准直接触摸仅通过认证语义计划启用。旧 `TouchMapping` 的 `direct_standard` 仍拒绝。
   Controller 从真实设备清单解析唯一布局和认证，按所有权复查后派发；Worker 重新验证完整证明及编译结果。
   传输层接收已编译的 HID 坐标，不进行第二次映射；对外回执才报告 `direct_standard`。
5. Profile 增加 `metadata.semantic_template`：具名归一化 target、控件 bounds、唯一布局 selector、frame、
   显式布局旋转和逐条 before/after/skip_if。非等待输入必须有页面前置条件及后置条件，不能仅凭 ADB absent 盲点。
   UI 元素按精确标识唯一匹配；动态控件取实际中心。误差容限为控件边缘余量的一半、最大 8px，并计入量化误差。
6. HID 方向和 UI 布局方向分别表达；viewport、应用 frame 与显示 ID 必须明确。多显示 viewport 冲突时拒绝。
   输入前复核可变显示环境。非默认显示器的 UI 结构观察不假定 uiautomator 归属正确，使用绑定外部画面。
   未认证旋转、未知语言、布局或显示配置不做自动猜测。
7. ADB 未授权时用操作员在 Worker 配置的 `serial@USBport` → V4L2 画面绑定。
   Profile 只能引用绑定 ID 和区域灰度参考，不能指定设备文件、URL 或 shell 命令。
   捕获使用有时限的 argv、临时 PNG 和尺寸上限。参考只是有限区域比较，不声称 OCR、控件语义识别或任意布局自适应。
8. 语义回放每个输入后清理 USB/恢复 ADB，再观察状态；因此比连续 HID 回放慢。
   后置条件通过才发布 completed。状态不匹配、观察不可用、租约丢失或清理失败均停止后续输入；不重复不确定的触摸。
   暂停/继续/取消沿用原控制点，语义验证总预算最多 600 秒；整次回放沿用现有运行上限并为最终 ADB 等待预留时间。
9. 固件恢复同时需要固件 SHA-256 绑定声明、冻结的语义计划/认证、Worker 能力及成功烧写回执。
   类别认证不能替代镜像摘要校验；软件测试不能替代真机验收。

## Evidence and verification

Android 对直接触摸、指针分类及显示关联的机制见
[官方 Touch devices 文档](https://source.android.google.cn/docs/core/interaction/input/touch-devices?hl=en)
和 [AOSP TouchInputMapper](https://android.googlesource.com/platform/frameworks/native/+/776399cd109d54ed2ac9dfff71a18fbf2a37f5b9/services/inputflinger/reader/mapper/TouchInputMapper.cpp)。
本项目的 8px 工程阈值不是 Android 官方兼容保证。

测试覆盖原始证据篡改、重复参考机、错误类型/轴/显示关联、未知固件、所有权/撤销、真实 API 到持久 payload、
前后断言、无观察通道、暂停/取消/租约失效、单次输入、控件边界、外部画面绑定、模板导入导出及重复导航。
硬件分类、实际落点和刷机闭环仍需对应设备验收；没有预置任何已认证设备类别。
