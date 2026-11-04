# AOA 配置模板

`configs/examples/aoa/` 统一存放纳入版本管理的 AOA 命令模板。账号实际保存、编辑的 Profile
仍由平台数据库管理；移动或新增模板文件不会自动写入账号配置，也不会自动出现在
固件烧写弹框的配置列表中。使用前需要通过 AOA Profile API 保存到当前账号。

## 命名约定

文件名统一使用小写英文、数字和连字符：

```text
<model>-<firmware>-<scenario>.json
```

- `model`：设备型号或板型，例如 `rk3576`。
- `firmware`：固件系列或版本标识，例如 `ss17`；页面布局不同的固件使用不同模板。
- `scenario`：操作路径，例如 `wizard-to-usb-debug`。

同一适用环境的修订由 Git 和账号 Profile 的 revision 追踪，文件名保持稳定。
JSON 的 `name` 是界面显示名称，可以保留中文和已有版本说明；目录迁移不修改它。

## 当前模板

| 文件 | 适用环境 | 起始页面 | 目标 | ADB 等待 |
| --- | --- | --- | --- | --- |
| [rk3576-ss17-wizard-to-usb-debug.json](rk3576-ss17-wizard-to-usb-debug.json) | RK3576 / SS17；原录制环境为 `ssi_17_arm64`、`CP2A.260605.016`、1200×1920 | 开机向导首屏 `Hi there`，屏幕亮且未锁屏 | 完成开机向导、进入开发者选项、开启 USB 调试并处理授权步骤 | 60 秒 |

表中的适用环境来自模板原有说明。模板的坐标和等待时间依赖实际固件页面。
该旧模板按兼容鼠标模式标定；未提供输入模式 metadata 时按 `pointer` 回放。
如选择 `direct`，需要在直接触摸模式下重新验证坐标，不能混用两种模式的修改结果。
不同语言、分辨率或布局需要重新调试；文件迁移本身不代表通过新的完整真机验证。
新增模板时同步登记适用环境、起始页面、目标和验证范围，并在根目录 `.gitignore`
中逐文件加入白名单；目录内未明确放行的文件仍被忽略。

模板保留 Profile 创建接口使用的 `name`、`description`、`commands`、
`wait_adb_seconds` 字段。保存为账号配置后，先在 AOA 工作台验证，再用于烧写后的自动恢复。

原理、架构、录制和烧写接续说明见
[AOA 机制文档](../../../docs/aoa-mechanism-and-architecture.md)。
