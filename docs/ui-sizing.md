# UI 几何统一规范

唯一尺寸配置入口：`web/static/css/ui-sizing.css`。**字体、字号只由
`typography.css` 和五个规则文件管理**，不得在尺寸 CSS 中重复定义。

| 角色 | 变量 | 基准 |
| --- | --- | --- |
| 页面外围四边（所有 Shell 页面，桌面与移动端） | `--ui-page-edge` | 8px |
| 普通按钮、筛选输入、选择框 | `--ui-control-height` | 32px |
| 表格行内操作 / 紧凑工作区工具栏控件 | `--ui-control-height-dense` | 28px |
| 紧凑工作区单行工具栏 | `--ui-workspace-toolbar-height` | 36px |
| Shell 页头/密集微控件 | `--ui-control-height-small` | 26px |
| 内联图标/面包屑 | `--ui-control-height-icon` | 24px |
| 紧凑弹框操作 | `--ui-control-height-dialog` | 34px |
| 弹框标准表单 | `--ui-control-height-dialog-field` | 36px |
| 大型明确操作 | `--ui-control-height-large` | 40px |
| 数据表头 | `--ui-table-header-height` | 42px |
| 数据行 | `--ui-table-row-height` | 36px |
| 数据表格单元格内边距（垂直 / 水平） | `--ui-table-cell-padding-block` / `--ui-table-cell-padding-inline` | 2 / 10px |
| 工作区页签（桌面/窄屏） | `--ui-tab-height` / `--ui-tab-height-narrow` | 42 / 38px |
| 公共工作区页签宽度 | `--workspace-tab-width` | 110px |
| 常规 KPI 卡片 | `--ui-stat-height` | 95px |
| 紧凑 KPI 备选（非页头默认） | `--ui-stat-height-compact` | 70px |
| 分段页签内部 | `--ui-segmented-inner-height` | 24px |
| 卡片标题区 | `--ui-card-heading-height` | 42px |
| 弹框操作区最小高度 | `--ui-modal-actions-min-height` | 58px |
| 弹框标题区（紧凑/正常/宽松） | `--ui-modal-header-height-dense` / `--ui-modal-header-height` / `--ui-modal-header-height-roomy` | 50 / 54 / 56px |
| 弹框关闭图标 | `--ui-modal-close-size` | 30px |
| 移动端触控目标（普通/危险） | `--ui-touch-target-height` / `--ui-touch-target-danger-height` | 40 / 44px |
| AOA 命令行按钮/编辑框（720p 特例） | `--ui-aoa-command-button-height` / `--ui-aoa-command-editor-height` | 20 / 22px |
| AOA 配置列（正常/低视口） | `--ui-aoa-config-control-height` / `--ui-aoa-config-control-height-short` | 27 / 30px |

页面外围唯一尺寸定义为 `ui-sizing.css` 中的 `--ui-page-edge: 8px`。该文件只实现全局通用模板：Shell 页面以公共 `.page-content.container` 选择器生效；独立工作区使用 `.ui-page-edge` 标记内容区域。**不允许在统一尺寸文件列出业务页面 ID、模块名称或各页独立覆写。** 各业务样式不得独立声明页面外围 padding/margin，四边统一 8px；卡片、表格、弹框、终端内部内容间距不是页面外围间距，继续由组件自身管理。具有固定顶部页签栏的独立页面仅对内容区域应用这一模板，避免外框与内部页签栏重复叠加留白。

普通数据表格（包含 `thead` 的 HTML 表格及公共 `.ui-table` / `.list-table`）和测试套件文件网格统一由 `ui-sizing.css` 管理 42px 表头、36px 数据行和垂直居中。业务样式只负责列宽、对齐、内容省略及交互，不重复声明行高或单元格内边距。空数据、加载和失败占位行沿用公共行尺寸；含授权按钮等多行内容时自然扩展。设备分组行也沿用 36px 数据行。AOA 命令列表保持紧凑单行网格，窄屏允许横向滚动；AOA 回放、表单布局、架构及工单键值说明（公共 `.ui-detail-table`）、正文表格和展开详情允许内容自适应，不能裁掉有效信息。

## 公共工作区布局

外部知识源和测试套件复用 `ui-foundations.css` 的 `.ui-workspace-stack`、`.ui-workspace-panel`、`.ui-workspace-toolbar-panel`、`.ui-workspace-content`。区块间距使用 `--ui-spacing-sm`（8px），面板使用统一边框、背景和 `--ui-radius-card`（8px）。单行工具栏外框总高 36px（包含上下边框），内部控件 28px；可选进度或多行内容自然扩展。需要独立滚动的面板使用 `.ui-workspace-panel--scroll`。各业务页面仅定义特殊列宽、内容布局和交互，不重复声明面板边框或纵向区块间距。对话 Agent、个人知识库和测试套件的导航栏共用 `--ui-workspace-navigation-width`（250px）；知识库文档栏和 Agent 执行栏比例独立保留。系统接口与安全审计共用 `.ui-workspace-grid--aside`：宽屏 305–350px，1350px 及以下 275–310px，1150px 及以下单列。

## 设计边界

1. **保持单行**：单行标题、表格单元格及按钮标签不得折行。
   优先采用 `white-space: nowrap`、局部 `overflow-x: auto`、合理列宽和
   `text-overflow: ellipsis`；不能通过缩小字号压缩内容。
2. **同角色同高度**：同一行的普通搜索输入、过滤下拉和动作按钮都用 32px。
   紧凑分栏工具区使用 `.ui-workspace-toolbar` 和 `.ui-workspace-control`，统一 36px 单行栏及 28px 控件；窄屏允许工具栏内容换行扩展。
   Shell 顶部标题动作区沿用 26px，避免增加整页头部高度。
3. **卡片内容优先**：用户、设备、审计、系统接口四页的 KPI 统一 95px。
   大量数据/趋势图的卡片由内容决定高度；70px 紧凑 KPI 只供显式独立小卡片选用。例外基于组件角色，不能根据页面
   空间随意改字号或另造尺寸值。
4. **特殊工作区**：xterm、VNC、AOA、日志、固件拖拽上传、长表单/复杂弹框，
   可根据功能采用独立尺寸或内容自适应，但普通表单控件应使用共享角色。
5. **响应式**：遵循移动端 40/44px 最小触控高度、折行断点和
   页面宽度约束。单行表格允许**表格容器**横向滚动，不能让整个 shell 横溢。

## 合理例外与禁止操作

- **AOA 录制/标定 720p**：命令行内 20/22px 操作、27/30px 配置列为已登记的命名尺寸角色。不能为满足变量覆盖率把它们增至 32px；如需变更，必须检验 720p 无滚动溢出及命令回放可用。
- **集群 Worker 卡片**：卡片行内动作不等于顶部普通按钮。保留信息密度和 CPU/内存基线，不改变设备/任务状态语义。
- **API 明细/代码/日志**：代码行、JSON、时间戳可在自己的阅读区域滚动或换行；普通按钮/表格列标题不得新增自动换行。
- **Tailscale/固件/报告拷贝等弹框**：沿用原有紧凑/标准/宽松布局层级。尺寸变量替代字面量时不能改变绝对几何；不得通过统一高度撑开空白。
- **手机**：普通触控 40px、危险操作 44px。必要时滚动控件容器，不能让单行按钮文字折行或挤掉操作按钮。
- 规范按**控件角色**而非页面划分；仅允许带理由、测试的特殊功能尺寸。未使用共享变量的 SVG 图标、滚动容器、内容决定高度、标定输入不视为普通控件残留。

## 验证

运行 `python -m pytest -q tests/test_ui_sizing_contract.py
tests/test_typography_contract.py`；布局回归运行
`python -m pytest -q tests/runtime_ui/test_page_outer_inset.py
 tests/runtime_ui/test_shell_title_geometry.py
tests/runtime_ui/test_workspaces.py`。建议在桌面宽度 1440/1792px
逐页复核标题右侧按钮不换行、KPI 等高、表头对齐及弹框按钮不裁切。
自托管 Chromium 完整矩阵使用 `.github/workflows/onprem-ui-sizing-validation.yml`，
涵盖 390/1440/1792px 的关键页面和复杂弹框；运行中只启用隔离测试服务，
不重启生产业务。

修改 `ui-sizing.css` 的全局尺寸角色后，**所有独立工作区和 Shell 入口**
必须同时更新样式 URL 版本，避免旧浏览器缓存缺失新变量。
`test_geometry_cache_version_is_consistent_between_ui_roots` 用于检查版本一致性。

## KPI、看板控件与表格操作

普通 KPI 卡片共用 95px 高度、8px 圆角及主题面板边框，网格间距使用 `--ui-spacing-sm`（8px）。晨报六项指标使用公共紧凑卡片 `.ui-kpi-card--compact`（最小高度 70px），列数由业务页面决定。图标式卡片由 `ui-foundations.css` 提供共同结构，桌面图标容器 42×42px、图标 25×25px；窄屏使用共同的紧凑尺寸。Redmine/Gerrit 居中计数卡、GMS ATS 纵向统计卡保留各自内容角色，业务网格列数及状态颜色不强制相同。报告分析的专属统计条不改动。

普通输入框、下拉框和表单行使用 32px 控件角色；明确的组合输入及其操作按钮使用 28px 紧凑角色。普通卡片使用 8px 圆角。普通空状态及日志工具栏复用 `ui-foundations.css`；日志内容滚动、特定工具的操作顺序仍由模块管理。

Redmine/Gerrit 通过 `ui-dashboard` 共用普通按钮、输入/选择框及页头筛选布局。普通控件 32px，紧凑筛选 28px，移动端触控要求保留。分页采用 `ui-pagination`，页码控件 28px，`ui-pagination-footer` 管理页大小选择框；表格操作组采用 `ui-table-actions`，可通过修饰类指定右对齐。弹框共用科技风底部按钮布局，特定业务宽度及文件浏览、复杂内容布局仍独立。

状态标签统一使用 `.ui-status` 和语义色角色（`is-success`、`is-warning`、`is-danger`、`is-info`、`is-accent`），业务状态和值保持原样。普通空状态使用 `.ui-empty-state`，行内提示使用 `ui-empty-state--inline`，表格空态保留单个跨列单元格。日志工具栏、操作组、内容滚动分别使用 `.ui-log-toolbar`、`.ui-log-actions`、`.ui-log-content`；xterm 及日志操作权限不变。报告行操作允许按列宽换行，按钮标签本身不换行。

Runner 的页面清单测试输出 `table-inventory.json`、`component-inventory.json` 和各页面截图；控件采样用于核对字体、尺寸、圆角和滚动，不能替代实测结果审查。

普通卡片可复用 `.ui-card`（8px 圆角、公共边框、背景、12px 内边距），模块只定义必要的内容布局。`.ui-file-preview` 复用滚动边界但保持源代码缩进与横向阅读；日志采用独立换行角色。普通、紧凑和行内空状态使用公共角色。字体规则仍只在 typography/ 中定义。

CSS 引用扫描生成 `css-reference-inventory.json`，包含孤立字面选择器候选、同文本重复规则候选和内联按钮样式候选。该清单不等于死代码结论：动态类名、媒体查询、主题及状态依赖必须结合运行时证据复核。

CSS 引用审核同时记录媒体查询/容器查询上下文与动态类名前缀，不将动画关键帧、不同断点或 `log-${type}` 等生成类当作可直接删除的重复定义。扫描报告附带公共组件的源码引用文件；运行时 `component-role-coverage.json` 记录每个实际页面及分辨率采样到的公共角色。未激活的 Tab、未打开的弹框与无数据状态之外的动态内容仍须通过相应专项用例验证。

普通独立工作区使用 `ui-standalone-controls` 与 Shell 字段共用基础表单外观；紧凑工具栏和 AOA 专用控件继续使用对应尺寸角色。`ui-segmented` 用于标题级视图切换，`workspace-header--content` 用于内容区 Tab（APK Manifest 长标签允许较宽，常规工作区 Tab 保持 110px）。状态字体由 typography 共享语义规则管理，业务颜色仍由各模块提供。测试启停按钮固定宽度 90px，开始、启动中、停止及停止中保持等宽。

最近一次已完成的 Runner（46097629116，a72ef031）扫描报告中有 2 个静态无引用候选、5 个动态候选、1 个同作用域重复候选、0 个内联按钮候选。`.card-title` 已清理，旧 `.terminal-opened` 与其字号规则已移除；架构侧栏只保留有效的最后一个 760px 断点声明。此历史产物仅用于指导清理，不能替代最新提交的实机验收。
