# 工作区布局规范

- Agent 工作区使用会话、对话和执行步骤三栏布局，由 `web/static/css/workspace-panels.css` 管理。
- 设备管理使用统计卡、筛选和紧凑列表，由 `web/static/css/device-inventory.css` 管理。
- 其他工作区使用 `web/static/css/other-workspaces.css` 的 `.page-ui-rework` 规则，不覆盖 Agent、设备管理及 AOA 内部布局。
- 主题使用 `web/static/css/theme.css`；字体和尺寸遵循全局规范。

## 验收

检查 390×844、1440×960、1920×1080 视口的工作区、表格、弹框、日志及远程桌面/终端布局。运行 `tests/runtime_ui/test_ui_foundations.py`，验证事件绑定、导航、权限和控件可达性。
