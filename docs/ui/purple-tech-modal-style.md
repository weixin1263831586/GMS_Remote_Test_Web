# Purple Tech 弹框：按内容重构与紧凑布局

参考仓库根目录的 Tailscale、路由连通性、SSH、报告导入、固件与报告诊断效果图。
视觉参考用于切角边框、标题折线、图标、深色面板与操作层级；弹框尺寸按实际内容决定。

## 尺寸与排版

- 普通表单沿用 `modal-xs/s/m/l` 档位，使用自然高度，不为短表单设置工作台高度。
- 标题 17px，表单 13px，输入与常用操作约 34–36px 高；手机标题 16px。
- 路由检测最大 600px，用户名检测最大 620px，Tailscale / 固件最大 820px。
- 报告拷贝最大 760px，状态消息出现时自然增高，操作区不随正文滚走。
- 固件分享最大 960px、自然高度；分享列表超过 340px 后在列表内滚动。
- 报告诊断、设备配置、周报和长篇分析保留工作台空间，正文或分栏内滚动。
- AOA 配置的标签与下拉框始终同行，启动等待的标签与数值也同行。较窄屏幕将两个字段分成两行，每个字段内部不拆行。
- 删除重复的小标题，空状态不保留多余高度。底部按钮按可用宽度换行，避免手机上所有按钮各占一整行。

## 结构与实现

`web/shell/dialogs/` 下的模板保留原有控件 ID、事件绑定、权限与业务提交。

| 场景 | 内容结构 |
| --- | --- |
| 认证、Wi-Fi、SN、分类 | 图标表单行、说明、独立底部操作 |
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
`dialog-frame.js` 按实际 CSS 像素描绘连续细切角与粗亮角标；粗细描边共用完全相同的中心线与切角坐标，标题斜线限制在下划线区域，避开关闭按钮。装饰不裁剪根容器，也不承接点击或键盘事件。
背景、输入、边框与主按钮复用 `theme.css` 的中性灰和冷紫色变量；内部分隔线用灰色，外框仅混入少量主题紫色。保留危险和警告语义、40% 中性深色遮罩及 3px 背景模糊。

Shell 生命周期仍由 `ModalManager` 管理。独立文档沿用各自控制器。
`dialog-prompts.js` 把知识库输入、邮件收件人与独立页面确认改为异步内容表单；
存在控制器时复用控制器，无控制器的独立页面使用浏览器原生 `<dialog>` 焦点管理。
取消、ESC 和遮罩关闭均返回取消结果；确认前不执行后续请求。
用户输入通过 DOM 属性写入，不作为 HTML 解析。

路由检测保留实际 Ping 接口，没有添加无数据支撑的 TCP / WebSocket / Gerrit 检测结果。
参考图中的租约时长、烧写模式、日志预览等不存在的业务能力未伪造为可点击控件。
外部第三方网站和跨源 iframe 的界面不属于本仓库的弹框实现。

## 核验与截图

```bash
python -m pytest tests/architecture/test_modal_tech_style.py \
  tests/architecture/test_individual_modal_redesign.py \
  tests/architecture/test_shell_script_loading.py \
  tests/architecture/test_frontend_size_rules.py -q

GMS_MODAL_GALLERY_OUT=artifacts/modal-redesign \
  python -m pytest tests/runtime_ui/test_modal_redesign.py -q

python -m pytest tests/runtime_ui/test_modals.py \
  tests/runtime_ui/test_firmware_aoa.py tests/runtime_ui/test_report_copy.py \
  tests/runtime_ui/test_navigation.py tests/runtime_ui/test_ui_foundations.py -q
```

图库位于 `artifacts/modal-redesign/index.html`，附 `results.json` 和真实 Chromium 截图。
图库使用隔离环境与明确标记的渲染测试数据，不执行烧写、设备接入或邮件发送。
桌面与手机检查控件越界、按钮截断、底栏可达性、角标中心线与外框重合、标题斜线不与关闭按钮相交；原有回归还覆盖 768px 与横屏、
ESC、焦点归还、嵌套弹框、重复导航、AOA 提交和初始化无写请求。

视觉验收需要查看实际截图，不能用模板类名或通过测试代替视觉判断。
