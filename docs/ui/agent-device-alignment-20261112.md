# 其他页面统一 UI 重构补丁

基于 main `b0980f2153c1aed498f4c095a2b26aa27d763109`（2026-11-12）。

## 设计基准
- 用户已重构的 Agent：`web/static/css/workspace-panels.css`（多栏 pane、状态和操作区）。
- 用户已重构的设备管理：`web/static/css/device-inventory.css`（统计卡、筛选、紧凑列表）。
- 色彩：以最新 `web/static/css/theme.css` 的紫灰暗色主题为准，不再使用前几轮蓝色效果图。
- 本次新增 `web/static/css/other-workspaces.css`，仅通过 `.page-ui-rework` 应用于其余页面。
- 不修改 `#page-agent`、`#page-devices` 和内部 AOA 区域。

## 代码边界
只修改模板容器 class、用户/审计/API 的 KPI 卡片语义分层和 CSS。
保留 ID、data-click、data-change、API 请求、设备租约、用户权限、modal、WebSocket、终端流与所有原有功能。

## 验证
- 检查 1440×960、1920×1080、390×844；
- 校验测试页所有设备按钮、日志切换、上传和 CTS 执行；
- 校验远程桌面/终端单屏/左右/上下/四屏与 resize；
- 校验用户表、审计、API、报告、套件文件操作、APK 文件树、知识库、iframe 弹窗和滚动。
- 运行相关 Playwright runtime UI 合约测试，特别是 `tests/runtime_ui/test_ui_foundations.py`。
- 本补丁未部署内网，也尚未执行浏览器视觉回归。
