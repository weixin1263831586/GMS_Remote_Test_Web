# UI 几何统一规范

唯一尺寸配置入口：`web/static/css/ui-sizing.css`。**字体、字号仍只由
`typography.css` 和原有五个规则文件管理**，不得在尺寸 CSS 中重复定义。

| 角色 | 变量 | 基准 |
| --- | --- | --- |
| 页面外围四边（所有 Shell 页面，桌面与移动端） | `--ui-page-edge` | 8px |
| 普通按钮、筛选输入、选择框 | `--ui-control-height` | 32px |
| 表格行内操作 | `--ui-control-height-dense` | 28px |
| Shell 页头/密集微控件 | `--ui-control-height-small` | 26px |
| 内联图标/面包屑 | `--ui-control-height-icon` | 24px |
| 紧凑弹框操作 | `--ui-control-height-dialog` | 34px |
| 弹框标准表单 | `--ui-control-height-dialog-field` | 36px |
| 大型明确操作 | `--ui-control-height-large` | 40px |
| 数据表头 | `--ui-table-header-height` | 42px |
| 数据行 | `--ui-table-row-height` | 44px |
| 文件列表表头/嵌入式紧凑表格 | `--ui-table-header-height-dense` / `--ui-table-row-height-dense` | 36 / 36px |
| 工作区页签（桌面/窄屏） | `--ui-tab-height` / `--ui-tab-height-narrow` | 44 / 38px |
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

Shell 页面的标题内边距、内容区域外框四周留白统一使用 `--ui-page-edge: 8px`。只调整页面外围间距，卡片、表格、终端、弹框等内部组件保留各自的间距规范。独立 iframe 的内部工具栏与工作区不额外叠加同一层页面边距。

## 设计边界

1. **保持单行**：原先单行的标题、表格单元格、按钮标签不允许因统一尺寸变成多行。
   优先采用 `white-space: nowrap`、局部 `overflow-x: auto`、合理列宽和
   `text-overflow: ellipsis`；不能通过缩小字号压缩内容。
2. **同角色同高度**：同一行的普通搜索输入、过滤下拉和动作按钮都用 32px。
   Shell 顶部标题动作区沿用 26px，避免增加整页头部高度。
3. **卡片内容优先**：用户、设备、审计、系统接口四页的 KPI 统一 95px。
   大量数据/趋势图的卡片由内容决定高度；70px 紧凑 KPI 只供显式独立小卡片选用。例外基于组件角色，不能根据页面
   空间随意改字号或另造尺寸值。
4. **特殊工作区**：xterm、VNC、AOA、日志、固件拖拽上传、长表单/复杂弹框，
   可根据功能采用独立尺寸或内容自适应，但普通表单控件应使用共享角色。
5. **响应式**：维持已经存在的移动端 40/44px 最小触控高度、原有折行断点和
   页面宽度约束。单行表格允许**表格容器**横向滚动，不能让整个 shell 横溢。

## 合理例外与禁止操作

- **AOA 录制/标定 720p**：命令行内 20/22px 操作、27/30px 配置列为已登记的命名尺寸角色。不能为满足变量覆盖率把它们增至 32px；如需变更，必须检验 720p 无滚动溢出及命令回放可用。
- **集群 Worker 卡片**：卡片行内动作不等于顶部普通按钮。保留信息密度和 CPU/内存基线，不改变设备/任务状态语义。
- **API 明细/代码/日志**：代码行、JSON、时间戳可在自己的阅读区域滚动或换行；普通按钮/表格列标题不得新增自动换行。
- **Tailscale/固件/报告拷贝等弹框**：沿用原有紧凑/标准/宽松布局层级。尺寸变量替代字面量时不能改变绝对几何；不得通过统一高度撑开空白。
- **手机**：普通触控 40px、危险操作 44px。必要时滚动控件容器，不能让原本单行的按钮文字折行或挤掉操作按钮。
- 规范按**控件角色**而非页面划分；仅允许带理由、测试的特殊功能尺寸。未使用共享变量的 SVG 图标、滚动容器、内容决定高度、标定输入不视为普通控件残留。

## 验证

运行 `python -m pytest -q tests/test_ui_sizing_contract.py
tests/test_typography_contract.py`；布局回归运行
`python -m pytest -q tests/runtime_ui/test_page_outer_inset.py
 tests/test_shell_title_geometry.py
tests/runtime_ui/test_workspaces.py`。建议在桌面宽度 1440/1792px
逐页复核标题右侧按钮不换行、KPI 等高、表头对齐及弹框按钮不裁切。
自托管 Chromium 完整矩阵使用 `.github/workflows/onprem-ui-sizing-validation.yml`，
涵盖 390/1440/1792px 的关键页面和复杂弹框；运行中只启用隔离测试服务，
不重启生产业务。

修改 `ui-sizing.css` 的全局尺寸角色后，**所有独立工作区和 Shell 入口**
必须同时更新样式 URL 版本，避免旧浏览器缓存缺失新变量。
`test_geometry_cache_version_is_consistent_between_ui_roots` 用于检查版本一致性。
