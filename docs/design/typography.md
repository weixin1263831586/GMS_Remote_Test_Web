# GMS 平台 Typography：全局字体与字号契约

所有页面只加载一个 CSS 入口。唯一的字体 Token 入口为 [`web/static/css/typography.css`](../../web/static/css/typography.css)，其通过 CSS `@import` 集中加载 `web/static/css/typography/` 下的页面作用域文字规则；业务页面不直接链接子文件。业务 CSS 只保留布局、几何和配色，不维护字号/字重/字体族声明。

| 场景 | 语义 Token | 字号 | 建议字重 |
| --- | --- | ---: | ---: |
| 页面主标题 | `--ui-type-size-page` | 18px | 600 |
| 弹框主标题 | `--ui-type-size-dialog` | 16px | 600 |
| 一级区域标题 | `--ui-type-size-section` | 15px | 600 |
| 卡片 / 分区标题 | `--ui-type-size-card` | 14px | 600 |
| 正文、表单、正常说明 | `--ui-type-size-body` | 13px | 400 / 500 |
| 表格、按钮、辅助说明 | `--ui-type-size-small` | 12px | 400 / 500 / 600 |
| 极紧凑状态、图表节点 | `--ui-type-size-compact` | 11px | 500 |
| KPI 数值 | `--ui-type-size-stat` | 24px | 600 |

图表强调数值可使用 `--ui-type-size-display`（20px）、`--ui-type-size-display-medium`（22px）。普通文字统一 `--ui-type-font-sans`，源码、命令、日志和文件路径统一 `--ui-type-font-mono`。字重使用 `--ui-type-weight-regular`（400）、`-medium`（500）、`-semibold`（600）、`-bold`（700）；正文/标题行高分别由 `--ui-type-line-body`、`--ui-type-line-heading` 管理，页面标题使用 `--ui-type-line-page`（24px）。

## 适用与例外

- Shell 的 `web/shell/components/head.html` 和所有独立 iframe HTML 入口只加载 `/static/css/typography.css` 一次，不单独引入任何 `typography/` 子样式文件；iframe 不继承父页面样式。
- 文本只使用统一 Token，模块不定义字号别名。
- `typography.css` **仅维护 Token 和五个 `@import`**；语义角色在 `roles-and-pages.css`，Shell 和工作区细则在 `shell-layout.css`、`shell-workspaces.css`，弹框与架构图在 `shared-diagrams.css`，内联语义角色及技术例外在 `inline-and-exceptions.css`。业务 CSS/JS 不单独定义字号数字。
- 11px **仅用于已确认的状态徽标、图形刻度、技术性节点标注**，不用于普通按钮、表格、表单、业务正文、提示说明或搜索框。普通辅助信息/列表元数据使用 12px；说明段落和表单使用 13px。
- 设备串口打印、xterm 字符格、代码/日志/命令等宽字体、SVG 和 Emoji 图标、拓扑连接与坐标轴刻度属于技术性例外。必须可说明用途和尺寸约束，普通按钮、表格和表单**绝不因空间不足成为例外**。
- 外发邮件由独立邮件客户端渲染，不能依赖本网站 CSS；邮件 HTML 为兼容邮箱可保留独立内联字体栈，其字体选择不等于网站业务页面的本地配置。
- 页面布局允许不同，同类组件必须共用语义 Token：普通按钮和表格 12px、输入控件和表单 13px、弹框标题 16px、页面标题 18px。
- APK 文件树标签、文件路径、权限说明等普通列表信息统一采用 12px；代码与路径仍保留等宽字族，不得使用 11px 压缩普通列表信息。
- 文件浏览器失败原因及提示、测试历史邮件数量、主机终端模式文字等属于普通辅助信息（12px），不允许复用 11px 状态/图形标注；工具分类页签在 390px 与桌面宽度一律保持 12px，架构侧卡标题使用卡片标题 14px。相关规则只存在五个子文件中，业务动态 HTML 只使用语义类。
- 不允许通过缩小普通字号消除换行：单行按钮、表头和工具条保持单行布局，空间不足时调整控件宽度、间距、可滚动区域或响应式布局，确实很长的正文可正常换行。

## 验证

运行 `python -m pytest tests/test_typography_contract.py tests/test_frontend_integrity.py -q` 和 `python -m pytest tests/runtime_ui -q`；验证 390px / 1440px 的弹框、顶部操作栏、表格溢出、报告分析与 APK 分析、架构图及主机集群。请勿仅按 CSS 字符串比较判断视觉正确性。

## 自托管 Runner 实机字体验证

`tests/runtime_ui/test_typography_rendered.py` 使用浏览器 `getComputedStyle()` 对 1440px 和 390px 页面字号进行验证，生成浏览器截图与 JSON 证据；`.github/workflows/onprem-typography-validation.yml` 通过 `ats-041055-64g` 自托管 Runner 运行。验证在临时隔离的测试实例中进行，不重启或写入生产服务，也不代表线上部署已升级到该分支。

## 动态页面与自动回归

- JS 动态构造的页面同样必须使用集中定义的 CSS 类，或只声明 `data-ui-type-size` / `data-ui-type-weight` 语义角色；不允许把 `11px`、`12px`、`13px` 写回业务文件，更不能直接设置普通按钮和表单的内联字号。
- `tests/test_typography_contract.py` 验证入口所有权、字号用途、普通控件和动态内容契约；`tests/runtime_ui/test_typography_rendered.py` 在 Chromium 运行时测量 Shell/iframe 的计算字号和 ADB Proxy、USB/IP 路由记录。`tests/runtime_ui/test_modal_compact_layout.py` 检查接入弹框在四个视口下的实际布局与 13px 路由文字。
- 任何更改字体角色的提交均必须检查 390px/1440px 页面、可见文本、无意换行及浏览器最终 `getComputedStyle`。专项验证通过不能代替完整 CI、弹框回归和线上部署版本核验。
