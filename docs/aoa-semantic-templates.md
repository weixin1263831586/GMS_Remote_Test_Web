# AOA 类别认证与语义模板使用说明

适用版本：包含 `aoa_semantic_v1` 的 Controller/Worker。架构见
[ADR 0029](architecture/adr/0029-aoa-class-certification-and-semantic-playback.md)。
这些软件能力已实现；仓库不包含已完成真机认证的设备类别，也没有把模拟测试写成硬件验证结果。

## 1. 从参考机签发类别认证

1. 准备同一型号、board/hardware、内核和 Android 版本的两台不同序列号参考设备，均允许 ADB。
   安装 [安全标定应用](../worker_agent/calibration_app/README.md)。选择录制 Worker/设备，
   展开工作台“设备类别认证 / 语义模板”，填写显示 ID，点击“验证录制设备 direct 输入”。
2. 系统打开专用 Activity，检查前台/显示器/锁屏，注册 direct HID，采集输入类型和轴信息并测量七点。
   验证会改变 USB 模式，必须在安全页面操作。失败时查看原命令回执，不用普通页面手工点击作为认证证据。
3. 两台参考机的成功命令 ID 会逐行加入输入框。填写认证名称后点击“签发认证”。
   服务端重新读取完成回执，核对账号、目标、物理端口和租约，然后保存认证。
4. 认证仅覆盖实际验证过的指纹、显示尺寸、密度、方向和显示路由。新增配置需补充实测回执重新签发，
   不能仅改 metadata 把未知配置当作兼容。其他同类设备在这些已测配置下可免逐台仿射标定。
5. 可刷新或撤销认证。撤销阻止新解析和新派发；已经接受的操作按冻结计划执行，以保留清晰的刷机终态。

相关接口（均沿用人工会话和设备授权边界）：

| 接口 | 用途 |
| --- | --- |
| `POST /api/devices/aoa/certifications/verify` | `{worker_id, device, display_id}`，返回可查询命令 ID |
| `POST /api/devices/aoa/certifications` | `{label, command_ids}`，从服务器回执签发 |
| `GET /api/devices/aoa/certifications` | 仅列出当前账号认证 |
| `DELETE /api/devices/aoa/certifications/{id}` | 撤销当前账号认证 |
| `POST /api/devices/aoa/templates/resolve` | `{worker_id, device, profile_id?, display_id?}`，唯一匹配当前账号模板 |

## 2. 编辑、保存和选择模板

模板和命令列表一起放在 Profile JSON，导入/导出保留 metadata。下面是结构示例，值均为占位示例，
必须替换为参考机实际环境、当前账号认证 ID 和实测页面。示例没有真实认证效力。

```json
{
  "name": "示例：进入下一页",
  "commands": ["click 180 481"],
  "metadata": {
    "hid_touch_mode": "direct",
    "hid_session_mode": "per_command",
    "hid_settle_seconds": 2,
    "class_certificate_ids": ["替换为已签发认证ID"],
    "target_display_id": 0,
    "semantic_template": {
      "version": 1,
      "layouts": [{
        "id": "product-en-portrait-v1",
        "selector": {
          "model": "实际型号", "release": "实际版本", "fingerprint": "实际完整指纹",
          "locale": "en-US", "display": [800, 1280], "density": 240,
          "rotation": 0, "display_route": "实际uniqueId", "display_id": 0
        },
        "frame": [0, 0, 1, 1],
        "rotate_quarters": 0,
        "targets": {
          "start": {"point": [0.5, 0.75], "bounds": [0.3, 0.65, 0.7, 0.85]}
        }
      }],
      "steps": [{
        "target": "start", "timeout_seconds": 5,
        "before": {"type": "ui", "selector": {"text": "Hi there"}},
        "after": {"type": "ui", "selector": {"text": "Next"}}
      }]
    }
  }
}
```

`commands` 保留可见命令顺序和动作种类；触摸坐标以具名 target 重新编译，原数字仅作兼容显示。
归一化 `point` 和 `bounds` 相对于布局 frame，frame 相对于目标显示屏。
像素宽高和 density 用于精确选择布局，不用 density 推导 HID 比例。
`longClick` 使用 target；`swipe` 步骤使用 `from_target` / `to_target` 并保留原命令时长。
等待步骤可省略断言；其他动作必须同时定义 before/after，before 必须包含可观察页面身份。
每个输入仅执行一次，after 超时不会再次点击。保存命令数量变化时须同步更新 steps。

ADB 可用时，target 可附加 `selector: {resource_id: "包名:id/按钮"}`，系统从当前唯一控件的实际 bounds
选择中心，解决已知页面中行位置变化。选择器支持精确 text/resource_id/content_desc、checked、enabled；
匹配多个元素或控件过小会拒绝。无 ADB 时不能使用这个动态选择器，需要经过画面断言的已知布局点。

工作台“读取当前模板”载入编辑器，“应用到当前命令”仅修改当前编辑状态，保存时由服务端完整校验。
“按目标匹配模板”使用 Worker 真实清单，要求当前账号恰有一个匹配 Profile/布局/认证，否则提示冲突。
单条“执行”仍支持调试，但语义步骤会恢复 USB 后检查状态；保持 HID 的勾选不会取消这些检查。

## 3. 状态条件与无 ADB 观察

`before` / `after` / `skip_if` 支持：

- `ui`：唯一精确元素及其 checked/enabled 状态，`exists:false` 表示在成功取得页面结构后确认不存在。
- `adb_state`：device、unauthorized、offline、absent。探测失败不等于 absent。
- `visual`：绑定画面 ID、归一化 ROI、16×16 灰度参考和最大平均灰度差（0..12，默认 3）。
- `all` / `any`：有界组合。无法观察不会变成“页面满足”；any 只有确实满足的分支才通过。

例如 USB 调试已经成功开启且 ADB 为 device 时，可在该点击步骤设置
`skip_if: {"type":"adb_state","state":"device"}`，避免再次关闭开关。
after 可以等待 unauthorized，随后专门处理 RSA 确认步骤，最终仍须以 ADB device 验收。
RSA 对话框必须核对可信电脑信息；模板页面条件不能只填写“有一个按钮”。

AOA 负责输入，不提供屏幕回读。未开启 ADB 的 user 固件需要外部摄像头或 HDMI→V4L2 采集源。
由 Worker 运维在正常 Worker 环境配置中设置 `GMS_WORKER_AOA_CAPTURE_BINDINGS`，例如：

```json
{
  "实际序列号@1-2": {
    "id": "camera-left", "device": "/dev/video0", "width": 1920, "height": 1080,
    "crop": [0.1, 0, 0.9, 1], "rotate_quarters": 0,
    "context": {"display": [800, 1280], "rotation": 0, "display_route": "实际uniqueId", "display_id": 0}
  }
}
```

需要 ffmpeg、Pillow、采集设备权限和固定画面。crop 先裁取设备有效屏幕，rotate_quarters 再顺时针旋转。
实际 USB 端口、显示身份和画面方向必须由操作者核对，不能由上传 Profile 改写。
工作台填写 ROI 后点击“采集目标页面参考”，将生成的 condition 放入对应 before/after。
采集本身也走已有设备租约通道；Profile 不能提供任意文件路径或远程 URL。
选择有辨识力且稳定的控件/页面区域，避开时钟、动画、曝光变化；比较是有限参考断言，并非通用图像理解。

## 4. 显示适配与固件接续

不同屏幕方向的 HID 变换来自实测标准方向；UI 布局是否旋转由独立 rotate_quarters 明确指定。
分屏使用明确 frame，多个显示器使用已认证 display_id/uniqueId/viewport；非默认显示器用绑定外部画面观察。
工作台“目标显示器 ID”或 metadata.target_display_id 明确选择目标；同一配置包含多个可匹配显示布局时须显式选择。
新的语言、密度、窗口布局、方向或显示路由需要对应布局和实测配置，不能只沿用旧坐标。
ADB 可用时输入前重查尺寸、密度、方向和路由，变化即停止。无 ADB 时依赖固定的绑定画面及页面断言。

烧写接续仍需 [固件兼容声明](aoa-mechanism-and-architecture.md)、真实镜像 SHA-256 和冻结的刷后环境。
Worker 缺少 aoa_semantic_v1 会在烧写前被拦截。无 ADB 固件恢复的首个输入必须含可用 visual 前置条件；
烧写前还核对 Worker 的 ffmpeg/Pillow 能力、目标串号/端口绑定、画面 ID 及显示配置。
类别认证、固件声明和最后 ADB 验收各自独立校验。
语义回放每个输入后清理 USB，因此比连续回放慢；后置断言通过才显示“已完成”。
缺少画面、页面不匹配、控件歧义、租约失效或 USB 清理异常时停止后续动作，不自动重放可能已经执行的点击。

## 5. 验收状态

软件测试覆盖证据、权限、撤销、模板解析、几何、页面断言、执行控制、持久 payload 和浏览器重复导航。
本次扩大回归 649 项通过；后续收口回归 153 项通过；架构/契约 115 项和浏览器 18 项通过，各组不累加。
尚待现场提供 RK3562/RK3576/RK3588 等实际参考机，完成分类/轴/显示绑定、独立落点及对应 user 固件
从烧写、开机向导、USB debugging、RSA 确认到 ADB device 的闭环。当前清单未发现 RK3576GMS1，未操作其他设备代替它验收。
