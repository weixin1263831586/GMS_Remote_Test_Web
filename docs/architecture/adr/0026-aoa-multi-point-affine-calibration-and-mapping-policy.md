# ADR 0026：AOA 多点仿射标定与映射策略

- Status: Accepted
- Date: 2026-11-04
- Related: [ADR 0022](0022-web-aoa-recording-and-usb-recovery.md), [ADR 0023](0023-aoa-direct-touchscreen-and-stable-hid-set.md), [ADR 0024](0024-aoa-pointer-compatibility-and-input-modes.md)

## Context

不同设备的 `wm size` / `wm density` / 屏幕尺寸不同，逐台手动标定不可扩展。
触摸标定评审（2026-11，含外部模型对标 Android 官方触摸设备文档的核对）确认
了三条边界：

1. `wm size` 与 `wm density` 只能估算初始坐标。Android InputReader 对触摸
   的缩放、旋转与显示绑定由设备端输入配置决定，不保证 HID 逻辑坐标与屏幕
   像素按宽高线性对应；density 只影响 UI 布局，不是 HID 坐标换算系数。
2. AOSP 触摸设备文档的映射分母是 `max - min + 1`。HID Logical Maximum
   360/640 的按轴估算应使用 `W/361`、`H/641`，此前实现用 `W/360` 偏差虽小
   但与官方公式不一致。
3. 单点中心探针只能在"零偏移、无旋转、纯按轴缩放"下成立；实机已出现两轴
   共用统一比例并带 Y 偏移的案例（800×1280 两轴 ~2.216，中心偏差 ~69px），
   单点比例无法表达。

单点标定（ADR 0022/0023 时代）只产出 `measured` 布尔值与 scale 元数据，
标定结果未持久化，前端 `aoaPanelState` 丢失后即需重标；无法支撑
"userdebug 标定 → 刷 user build → 匹配标定记录 → AOA 恢复" 流程。

## Decision

1. **多点仿射标定**：五点 HID 探针（四角 25%/75% 内缩 + 中心）实测
   `dumpsys input` cooked 坐标，最小二乘拟合 `X = a·u + b·v + tx`、
   `Y = c·u + d·v + ty`。采样不足或共线时失败退回单点比例，再退回按轴
   AOSP 估算。每个采样点保留欧氏残差并输出 RMS 与最大误差。
2. **AOSP 公式对齐**：`real_to_aoa_point` 与按轴估算分母改为
   `AOA_TOUCH_MAX + 1`；标定上下文版本升到 3（v2+ 均按 direct 推断的
   规则不变）。
3. **mapping_policy** 替代 `measured` 布尔值：
   `direct_standard`（已验证的 AOSP 直接触摸映射，免逐台标定）、
   `direct_unverified`（描述符 direct 但未经设备端验证）、
   `pointer_calibrated`（旧指针语义 + 实测映射）、`estimated`（仅按轴
   估算）、`unsupported`（无法确认分类/显示绑定，禁止自动触摸）。
   多点标定最大误差 > 8px 时自动降为 unverified。
4. **标定持久化**：新增 `aoa_calibrations` 表（owner 隔离），持久化完整
   `TouchMapping`（矩阵、偏移、采样、误差、policy、环境上下文）。复用
   匹配键为 model + Android release + display + rotation + hid_touch_mode，
   全等且 `verified` 才复用；环境任一变化必须重新标定。
5. **执行时动态转换**：`recover`/烧写恢复在派发时按设备环境匹配标定记录，
   命令坐标经 `TouchMapping.screen_to_hid` 重投影；跨设备复用同一 Profile
   时经 "源映射 → 参考像素 → 目标映射" 归一化换算。无匹配记录时明确标记
   unverified 回放，不得当作已验证坐标。`unsupported` 策略拒绝自动触摸。
6. **归一化坐标优先**：经设备端验证 direct 分类与显示绑定后（参考设备上
   `getevent -lp` 确认 `INPUT_PROP_DIRECT`、ABS_MT 轴范围与显示关联），
   标准映射设备免逐台标定；验证结论按设备类别复用，不按 serial。

## Consequences

Controller、Worker、契约快照同步更新；旧 Profile 不改坐标，新策略字段
缺省为空（沿用 ADR 0024 的模式推断）。多点标定在真实界面执行探针触摸，
沿用"明确用户动作 + 设备租约 + 物理端口绑定"边界，不在系统设置页长按。
`wm density` 不进入任何坐标公式，仅作为环境上下文记录。
单位与阈值（8px 误差门槛）是工程验收值，不是 Android 官方保证；固件更换、
旋转或显示模式变化都会使标定失效并要求重新验证。

## References

- [Android 触摸设备分类](https://source.android.com/docs/core/interaction/input/touch-devices)
- [Android 兼容性：多显示器输入路由](https://source.android.com/docs/core/interaction/input/touch-devices)
- [ATS User Builds（官方 AOA 恢复定位）](https://source.android.com/docs/core/tests/development/android-test-station/ats-user-builds)
- [标定引擎](../../../foundation/aoa_calibration.py)
- [标定持久化](../../../features/devices/aoa_calibrations.py)
- [多点探针采集](../../../worker_agent/aoa_actions.py)
