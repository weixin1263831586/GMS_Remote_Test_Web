# AOA 完成度评审与 8e4dbf3781825b31 验证

评审日期：2026-11-02（Asia/Shanghai）。范围：当前工作区 AOA 的协议层、Worker 执行、
Profile/API、自动化编排和现场平台。本文为评审记录，未修改这些实现。

**快照边界**：评审期间源码仍有其他同步修改。38 项通过对应新增触摸校准之前的
`aoa_actions.py` 快照；10:41 新增校准后的代码仅补做受 USB 拦截保护的检查（见 F9），
没有在已被 Tradefed 占用的真机上重新运行其单元测试。

## 结论

**AOA 基础通道已可用，但尚未完成 user build 刷机后自动恢复 ADB 的闭环验收。**
实机确认 AOAv2 支持、触摸回放有效、回放后 ADB 在线；最小按键录制可用。
真实 USB 调试关闭场景仍被现有入场检查和阶段顺序挡住，且存在账号隔离、结果契约和
崩溃恢复问题。38 项既有测试通过不能覆盖这些集成缺口。

Google 官方将 AOA 动作放在刷机之后，以无需 ADB 的输入恢复 USB 调试。
GMS 必须保证恢复动作先于依赖 ADB 的固件验证。
[ATS User Builds 官方说明](https://source.android.com/docs/core/tests/development/android-test-station/ats-user-builds)

## 真机结果

所有操作均通过已有 Agent Profile 的 TLS 校验和 Agent Token，经平台设备动作入口发起，
没有通过直连 ADB/SSH 绕过设备归属和 claim。MCP 当前没有 AOA 工具，AOA 调用使用
同一 Profile 的 canonical `GmsClient`，由平台完成权限和 claim 处理。

| 项目 | 现场证据 | 结论 |
| --- | --- | --- |
| 设备 | `ats-worker-controller:8e4dbf3781825b31`，USB `1-6`，RK3562 | 目标唯一、初始空闲 |
| 固件 | Android 16，`ro.build.type=userdebug`，`ro.debuggable=1`，启动完成 | 并非刚刷入的 USB 调试关闭的 user build |
| 初始 USB | VID/PID `2207:0006`，`sys.usb.config=adb`，`adb_enabled=1` | ADB 已可用 |
| 协议探测 | `POST /api/devices/aoa/probe` → `aoa_protocol=2`、`aoa_supported=true` | 通过 |
| 系统命令 | 回放 `wake`、`home` 返回执行 2/2、`adb_state=device` | 传输及 ADB 重枚举返回成功；未据此判定 Home 的 UI 语义通过 |
| 触摸命令 | 回放 `click 22 37` 返回执行 1/1、`adb_recovered=true` | **通过：截图确认从 Quick settings developer tiles 返回 Developer options** |
| 元素定位录制 | `locate_only=true` 定位 USB debugging，不点击开关 | **失败：HTTP 500；journal 显示 `_dump_elements` 报 device not found** |
| 最小录制 | 仅录制 `wake`，返回 display `800×1280`、型号、Android 版本及 `wake,wake` | 基础路径通过，未证明导航旅程可回放 |
| 键盘命令 | 尝试 `key TAB` 时平台返回 409，提示 manual Tradefed test 占用 | 未执行，不作通过判断 |
| 最终库存 | 设备仍出现在 ADB 清单，transport_id `1057`，状态 `external_busy`，`claimed=false` | 已停止后续输入；未取消占用任务 |

触摸证据截图保存在本机临时文件：
`/tmp/gms-aoa-8e4dbf3781825b31-home.png`（点击前）和
`/tmp/gms-aoa-8e4dbf3781825b31-after-click.png`（点击后）。文件可能被系统清理。
本次未刷机、未关闭 USB 调试、未回放录制出来的 USB 调试开关点击。

AOAv2 的 HID 注册及事件传输说明支持这里的 USB 通道验证，但控制传输成功不能证明
Android 设置已改变。[AOAv2 协议](https://source.android.com/docs/core/interaction/accessories/aoa2)

## 必须修复的发现

### F1 · P1：ADB 关闭时到不了 AOA 执行器，自动恢复阶段顺序也靠后

- 位置：`worker_agent/device_actions.py:328`；`features/cluster/device_actions_api.py:93`；
  `features/automation/executors.py:525`；`features/automation/orchestrator.py:212`。
- 原因：通用设备动作先从 `probe_devices()` 的 ADB/Fastboot 清单验证目标；设备不在清单
  就拒绝，尚未进入 libusb AOA 分支。Controller 同时拒绝离线/未知设备。
  另外 `flash()` 先执行 ADB 属性校验，之后编排器才进入 `aoa_recovering`。
- 复现：隔离桩令 `probe_devices()` 返回空列表，调用 `aoa_probe`，得到
  `device is not attached to this worker`；USB 探测函数未被调用。
- 修复：为 AOA 使用受租约保护的物理 USB 绑定，在无 ADB 时仍可准确认领同一设备；
  顺序改为烧写完成、AOA 恢复、ADB/启动/固件校验、测试。

### F2 · P1：自动化按名称解析 Profile 会串用其他账号的命令

- 位置：`features/automation/executors.py:645`。
- 原因：用不存在的 `run.owner` 字段读取配置，同时 `include_all=True`，随后只按名称取
  第一个匹配项，绕过了 ProfileStore 原有 owner 分区。
- 复现：Bob 与 Alice 各有同名配置；Alice 的命令是 `back`，Bob 的命令是 `home`。
  Alice 的运行实际向设备提交了 Bob 的 `home`。
- 修复：按运行的 `created_by`/资源所有者读取精确 Profile ID；在创建运行时保存命令版本和
  哈希快照，运行中不再全局按名称解析。

### F3 · P1：本地 Worker 成功返回与编排器契约不一致

- 位置：`features/cluster/device_actions_api.py:159`；`features/automation/executors.py:703`。
- 原因：本地设备动作同步返回结果，不产生 `command_id`；AOA 编排器要求非空命令 ID。
- 复现：返回 `success=true, adb_recovered=true` 后，编排结果为
  `success=false, error="AOA recovery did not return a command id"`。
  现场本地 Worker 回放返回也确实没有 command ID。
- 修复：统一持久化操作回执；或者显式区分已完成同步结果和异步命令，避免成功误报失败。

### F4 · P1：远程命令完成可能掩盖 ADB 恢复失败

- 位置：`worker_agent/app.py:583`；`features/automation/executors.py:681`。
- 原因：Worker 命令执行结束可标为 completed，同时 result 中为 `success=false`；
  编排器只判断命令状态，不检查内层结果和 `adb_recovered`。
- 复现：输入 completed 命令，其 result 是
  `success=false, adb_recovered=false, adb_state=unauthorized`，编排器仍返回 `success=true`。
- 修复：恢复结果、授权状态、ADB online 必须参与成功判定，之后再做启动/固件验证；
  恢复失败不能进入测试。

### F5 · P1：派发和回执持久化之间中断会重放点击序列

- 位置：`features/automation/executors.py:663`、`:688`；
  `features/cluster/device_actions_api.py:130`、`:166`。
- 原因：只有已保存的 `aoa_command_id` 才能查询旧命令。新派发没有运行关联字段、固定
  幂等键或提交回执，端点每次生成新 operation/command。
- 复现：在命令 ID 尚未写入运行的同一输入上调用两次，发出两次 POST；payload 没有
  `automation_run_id` 或幂等键。源码宣称复用同一命令的注释与实际路径不符。
- 修复：以 owner/run/stage/配置哈希建立稳定回执；响应丢失后查询原操作。
  USB 调试开关的点击不能在不确定状态下从头重放。

### F6 · P1：重枚举只按 serial 取第一台，缺少物理身份约束

- 位置：`worker_agent/aoa_actions.py:68`、`:157`。
- 原因：USB 枚举只匹配 `serial_number` 并取第一个；没有核对 Worker 上的物理端口、
  claim generation，也没有拒绝同串号多设备。固件改变 serial 时则可能永远找不到原设备。
- 修复：烧写前记录 USB 拓扑及 VID/PID/serial，重枚举时按同一端口重新绑定并拒绝歧义。
  当前设备的 `1-6` 库存信息尚未进入 AOA 执行契约。

### F7 · P2：HID 中途失败不会执行正常退出的清理

- 位置：`worker_agent/aoa_actions.py:570`、`:586`。
- 原因：注销 HID 和 USB reset 位于正常路径，异常时 finally 仅 dispose handle，可能留下
  HID 注册及 accessory 状态，影响后续 ADB 连接。
- 复现：隔离桩让 system HID 通道拒绝注册，`wake` 报错后已注册通道为 `[0,1]`，
  reset 调用数为 0。
- 修复：用 finally 记录并清理已经注册的通道；恢复 USB 状态的失败单独记为诊断证据，
  保留原始动作错误。

### F8 · P2：录制存在坐标与自动导航遗漏

- 位置：`worker_agent/aoa_actions.py:196`、`:385`。
- 复现一：`wm size` 同时返回 Physical `800x1280`、Override `400x640` 时，代码取
  第一行，实际返回 `800x1280`，与“Override 优先”的注释相反。
- 复现二：查找元素时自动滚动确实执行了 ADB swipe，但导出的命令只有
  `wake, click 22 75, sleep 2500`，缺失这次必要滚动。
- 修复：完整解析后优先 Override；把查找过程中实际需要的滚动和等待编入回放序列，
  验证最终导出仍满足动作数/总预算限制。

### F9 · P1：新校准隐含真实输入，既有单元测试没有隔离 USB

- 位置：新增校准版 `worker_agent/aoa_actions.py:331`、`:338`、`:447`；
  `worker_agent/tests/test_aoa_record.py:115`、`worker_agent/tests/test_aoa_actions.py:138`。
- 原因：录制无条件调用 `_calibrate_touch_mapping()`，切换 accessory 模式并向屏幕中心
  发送真实触摸。`locate_only` 只限制后续匹配动作，没有禁用这次输入。
  两个既有录制测试只替换 `_adb_run` 等函数，未替换 `_usb`/校准函数，且写入真实设备串号。
- 隔离验证：拦截 `_usb` 后调用仅含 `locate_only` 的录制，确认仍尝试打开 USB；拦截器
  在触及真实 USB 前停止执行。未在现场执行这个校准。
- 影响：仅定位操作也可能改变界面；直接运行这些测试可能重枚举并触摸真机，绕过平台
  claim 与手工 Tradefed 占用检查。此前的 38 项通过不能证明新版测试安全或通过。
- 修复：校准作为明确且有设备租约的单独步骤，在已知可输入界面上执行；录制使用
  已保存的校准结果。所有单元测试默认禁止真实 USB/ADB，将真机测试放入显式启用的
  集成测试组，使用虚构串号并替换校准/USB 依赖。

## 其他完成度和验证边界

- **错误模型**：现场元素录制因 ADB 丢失返回未分类 500，而不是可诊断的依赖/连接错误；
  `recover` 的语法校验也没有像保存接口一样将 ValueError 转成 ApiError。
- **部署一致性**：评审期间 Worker 源码继续更新，新增了 pacing sleep。现场最小录制返回
  `wake,wake`，当前源码对 `wait_ms=100` 应追加 `sleep 100`。需统一加载新版本后再验收；
  当前 Worker 能力中没有独立的 AOA 协议/录制版本声明。
- **架构**：目标检查 11 项通过、2 项失败：跨 feature 内部导入、引入新依赖环。
  违规依赖包括 AOA API 导入 Cluster 内部路由/模型、Automation 导入 Devices 内部服务。
  应通过公开服务接口与组合根接线修复，不能提高基线或添加无理由豁免。
- **操作入口**：有后端 API；当前 GMS ATS 前端没有 AOA 配置入口，MCP/CLI 的 AOA
  命令发现结果为 0。不能把后端函数存在等同于面向用户的功能交付。
- **源码规则**：AOA 的正式设计边界已收敛到 ADR 0022，不再依赖临时评审材料。

## 已执行的验证

1. `gms_rt_context`：Profile、TLS、Agent Token 和 Controller 健康通过。
2. 通过集群库存定位精确 Worker/设备，随后执行上表中的平台 API 请求。
3. 最后一次完整通过的相关测试：`test_aoa_actions.py`、`test_aoa_record.py`、
   `test_aoa_profiles.py`、`test_orchestrator.py`，共 **38 passed**，不包含随后新增的触摸校准。
4. 依赖、循环、Controller/Worker 和 shell 边界检查共 **11 passed / 2 failed**。
5. AOA 与编排相关 Python 的 targeted Ruff 通过，`git diff --check` 通过。
6. 账号串用、结果契约、无 ADB 入场、重复派发、显示 Override、录制漏滚动及异常清理
   使用临时目录与隔离桩复现，没有对现场设备制造这些失败条件。

本次没有完成“清刷 user build → ADB 关闭 → AOA 开启 USB 调试 → RSA 授权 →
正确设备与固件校验 → Tradefed 启动”的真机验收。F1–F6 修复且设备空闲后才具备
开展这一验收的前提；现有 38 项测试需补覆盖上述真实 HTTP/编排边界。

## 源码快照

工作区存在暂存和未暂存改动，使用文件 SHA-256 标识本次核心结论的源码快照：

| 文件 | SHA-256 |
| --- | --- |
| `worker_agent/aoa_actions.py` | `daefb228246c50e34fd250626a290c57cb8ef0fd0e308cd2198135e2426daa6a` |
| `worker_agent/device_actions.py` | `58f34aa62a4ece9fdc62346fff87b5c25c43fe10e331a8fadfa2b141746cd2b4` |
| `features/automation/executors.py` | `a9ab8cd8c1f4d74193ea15f4da47b2ff9a0d920b085ccf1e0fee149c021eedac` |
| `features/automation/orchestrator.py` | `e501340dc08201960fab962009a40283059114bd22ec327eef5cf477c78ffba9` |
| `features/devices/aoa_api.py` | `f065385cc92c3e562005e7e021af813be5810f3b2c91bbd15ad9e8506b73ef2b` |

新增触摸校准版 `aoa_actions.py` 的补充检查快照 SHA-256 为
`2f3acf0989319338360e11d6ba13b0e3ea2ee79583e8fbcf29edacd4ca83011d`。
较早快照的行号在新增代码后会偏移，F7/F8 仍可按函数名定位。
