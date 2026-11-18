# Purple Tech 弹框：按内容重构与紧凑布局

参考系统中的 Tailscale、路由连通性、SSH、报告导入、固件与报告诊断现有弹框。
视觉参考用于切角边框、标题折线、图标、深色面板与操作层级；弹框尺寸按实际内容决定。

## 尺寸与排版

- 普通表单沿用 `modal-xs/s/m/l` 档位，使用自然高度，不为短表单设置工作台高度。
- 弹框统一使用标题 16px、正文/标签/输入/按钮 13px、辅助说明 12px；常用按钮与单行输入默认 32px 高，桌面与手机沿用相同的字号层级。
- 路由检测保持紧凑；用户名检测最大 740px，Tailscale / 固件最大 820px，避免固件对话框被后置覆盖扩展为 1080px。
- ADB 与 USB/IP 接入的设备来源、接入主机同排显示；设备列表和当前接入分别成区，短内容自然高度，长列表内部滚动。
- 固件目标和文件卡片按实际内容定高；空文件信息折叠，AOA 说明不占用空白预留行。
- 报告拷贝最大 760px，状态消息出现时自然增高，操作区不随正文滚走。
- 共享固件弹框最大宽度 1040px、高度 `min(840px, 100dvh - 40px)`；表格最小宽度 1080px，支持表格区域横向和纵向滚动，操作底栏固定且右对齐。
- 报告诊断、设备配置、周报和长篇分析保留工作台空间，正文或分栏内滚动。
- AOA 配置的标签与下拉框始终同行，启动等待的标签与数值也同行。较窄屏幕将两个字段分成两行，每个字段内部不拆行。
- 删除重复的小标题，空状态不保留多余高度。底部按钮按可用宽度换行，避免手机上所有按钮各占一整行。

## 图标语义

- 修改设置：`settings`；选择扫描方式：`search`；实际重新加载、刷新或同步：`refresh`。
- 复制报告等复制动作：`copy`；烧写后自动恢复工作流：`workflow`。
- 从 `web/static/icons/ui.svg` 复用图标，不根据出现频率统一替换；同一操作保持相同含义。

## 结构与实现

`web/shell/dialogs/` 下的模板保留原有控件 ID、事件绑定、权限与业务提交。

| 场景 | 内容结构 |
| --- | --- |
| 认证、Wi-Fi、SN、分类 | 图标表单行、说明、独立底部操作 |
| 登录与管理员初始化 | 凭据图标行、账号与密码说明、可滚动的初始化指引、独立提交区 |
| 固件烧写 | 设备与固件来源并列、AOA 恢复配置、清除数据警告、提交操作 |
| 报告拷贝 | 来源与目标分别成区，保留真实 Worker / 套件选择 |
| 报告导入 | 文件和文件夹两种真实选择入口，保留原有上传链路 |
| 报告诊断 | 实际报告摘要与用例计数、独立滚动的诊断证据、底部操作 |
| SDK 源码注册 | 连接来源、根目录、凭据与版本、索引元数据 |
| 设备配置 | 标题与设备信息、独立可换行页签、查询正文 |
| ADB / USB-IP | 来源与目标、设备选择、当前接入记录 |
| Redmine / Gerrit | 成员、项目、连接设置、日期范围、分析和统计分别布局 |
| 安装 / 使用指导 | 原有步骤、命令和说明分区，统一标题与操作区 |

`modal-tech-dialogs.css` 负责框架、图标行和控件；`modal-tech-cases.css` 负责各任务的内容布局。
`dialog-frame.js` 按实际 CSS 像素描绘连续细切角与粗亮角标；粗细描边共用完全相同的中心线与切角坐标。带 SVG 框的弹框由 CSS 的 `polygon` 裁切 16px 四角，SVG 独占外框与标题折线（矩形 CSS 边框关闭），避免重影；装饰层不承接点击或键盘事件。
参考设计使用深紫色主面板（`#1c1929`）、紫色边框与高亮主按钮；危险和警告色仍按语义独立显示。遮罩保留 40% 中性深色与约 3px 模糊，而非纯黑背景。

Shell 生命周期仍由 `ModalManager` 管理。独立文档沿用各自控制器。
登录层沿用 `api.js` 的认证生命周期，表单与 SSH 安装指引共享外框几何和主题；
`auth-dialog.css` 单独组织凭据行、提示和操作区，不把登录层加入普通弹框栈。
`dialog-prompts.js` 把知识库输入、邮件收件人与独立页面确认改为异步内容表单；
存在控制器时复用控制器，无控制器的独立页面使用浏览器原生 `<dialog>` 焦点管理。
取消、ESC 和遮罩关闭均返回取消结果；确认前不执行后续请求。
用户输入通过 DOM 属性写入，不作为 HTML 解析。

路由检测保留实际 Ping 接口，没有添加无数据支撑的 TCP / WebSocket / Gerrit 检测结果。
参考图中的租约时长、烧写模式、日志预览等不存在的业务能力未伪造为可点击控件。
外部第三方网站和跨源 iframe 的界面不属于本仓库的弹框实现。

## 特殊场景的内容级验收

- 系统配置使用 `showConfig()` 在测试隔离环境内生成真实配置表单；仅开启空模板不算验收。邮件、凭据与路由读取统一用无敏感内容的 GET fixture，避免管理员认证层覆盖截图。
- 图片预览需提供合成图片来源、说明文字与缩放区域，禁止把一整块黑色占位当作已渲染通过。
- 测试结果、审计日志详情与历史邮件详情分别构造真实结构的测试数据，检查表格、分栏、长文本以及滚动区域。
- Redmine 统计设置采用不收缩的纵向字段卡片和内部滚动，防止下一个字段的标题覆盖上一个输入框；内部工单创建的三个 ID 字段改为独立行，禁止手机上出现逐字换行。
- 只运行隔离 Uvicorn + Chromium 测试，不向真实 Worker/设备执行刷机、删除、邮件发送及其他变更操作。独立功能性的安全性和权限仍由专项测试把关。

## 静态资源版本与浏览器缓存

弹框框架样式与描边脚本由 Shell、Redmine、Gerrit、设备控制台、自动化、主机集群和 GMS 更新雷达共用。更新 `modal-tech-dialogs.css`、`modal-tech-cases.css` 或 `dialog-frame.js` 后，应同步更新七处 HTML 入口的 `?v=` 版本号，使已部署浏览器不会沿用旧资源。不要只修改 Shell 的引用，造成同一弹框在不同工作区显示不一致。
`tests/architecture/test_modal_asset_versions.py` 会要求各入口的这三份资源版本一致。

## 核验与截图

```bash
python -m pytest tests/architecture/test_modal_tech_style.py \
  tests/architecture/test_modal_frame_single_owner.py \
  tests/architecture/test_modal_reference_fidelity_e6f16a49.py \
  tests/architecture/test_shell_script_loading.py \
  tests/architecture/test_frontend_size_rules.py -q

GMS_MODAL_GALLERY_OUT=artifacts/modal-redesign \
  python -m pytest tests/runtime_ui/test_modal_redesign.py -q

GMS_AUTH_GALLERY_OUT=artifacts/auth-dialog \
  python -m pytest tests/runtime_ui/test_auth_dialog.py -q

python -m pytest tests/runtime_ui/test_modals.py \
  tests/runtime_ui/test_firmware_aoa.py tests/runtime_ui/test_report_copy.py \
  tests/runtime_ui/test_navigation.py tests/runtime_ui/test_ui_foundations.py -q
```

图库由 `GMS_MODAL_GALLERY_OUT` 设置输出目录，包含 `index.html`、`results.json` 和 Chromium 截图。临时验证分支使用 GitHub-hosted Ubuntu 24.04 Runner；弹框覆盖数量由源码清单与 Actions 报告计算，不在文档内写死。工件保留期限以工作流配置为准。
图库使用隔离环境与明确标记的渲染测试数据，不执行烧写、设备接入或邮件发送。
桌面与手机检查控件越界、按钮截断、字段卡片重叠、底栏可达性、角标中心线与外框重合、标题斜线不与关闭按钮相交；原有回归还覆盖 768px 与横屏、
ESC、焦点归还、嵌套弹框、重复导航、AOA 提交和初始化无写请求。

视觉验收需要查看实际截图，不能用模板类名或通过测试代替视觉判断。
