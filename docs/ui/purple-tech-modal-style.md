# Purple Tech 弹框统一风格（2026-11-16）

## 设计基准

参考用户认可的 **Tailscale 组网 / 检查路由连通性** 深色科技感弹框：

- **系统主题**：使用现有 \`theme.css\` 的深紫灰背景和主题变量 \`--primary-color\`、\`--primary-light\`；不用独立蓝/青主题。
- **纯色，无渐变**：外壳 \`#1c1b27\`、输入区域 \`#14131e\`，边框与主要操作使用系统紫色；禁用原先的渐变按钮。
- **正式、克制的科技感**：角落紫色 L 型线条、标题短紫色横杠、清楚的表单视觉层级，不改变文案、控件顺序和状态颜色。
- **风险感知**：危险/警告操作保持原本红/黄色语义，尤其保留带 inline danger background 的通用确认按钮。
- **可访问性**：键盘焦点仍清晰；不修改 visibility、stack、inert、ESC、滚动锁或焦点归还控制器。
- **尺寸与响应式**：保留 \`modal-xs/s/m/l/xl/workspace/pin\` 宽高契约，只把 Tailscale 五行安装清单扩展为 \`modal-l\`；小屏在各自内容区滚动。

## 实际覆盖范围

Shell 在 \`web/shell/components/head.html\` 引入 \`web/static/css/modal-tech.css\`，
统一覆盖 \`web/shell/dialogs/\` 下 40 个模板，以及 Shell 运行时动态创建的同类弹框
（例如报告来源与 Agent 分析）。

隔离 iframe 内再单独加载同一个 CSS：Redmine、Gerrit、GMS ATS、主机集群、
设备串口、GMS 更新雷达。原本不使用共享弹框类的侧边抽屉（ATS trace drawer）
保留原有交互与定位，不将其强行替换成居中弹框。

**不包含**浏览器原生 \`window.alert/confirm\`、外部第三方页面和跨源 iframe，
这些不在应用 CSS 控制范围内。

## 兼容性边界

- Shell：继续由 \`web/static/js/modal.js\` 的 ModalManager 独占可见性。
- 独立页面：\`web/static/js/embedded-ui/modal-controller.js\` / ClusterModalController 持有独立生命周期。
- \`#route-check-dialog\` 已有特殊四角 SVG/规则线，沿用其结构；只去除按钮与装饰的渐变。
- \`#config-modal\` 之前的渐变标题变成纯色深灰；保留其各设置分组。
- \`#radar-settings-save\` 保持保存操作，用纯色紫色覆盖其旧 \`!important\` 渐变。
- \`install-guide.html\` 的亮色警告面板与进度条调整为深色。
- Tailscale、用户名识别弹框的表单行加边框，并保留全部原始 \`data-click\`/ID。

## 审核与验证

\`\`\`bash
pytest -q tests/architecture/test_modal_tech_style.py
pytest -q tests/runtime_ui/test_modals.py
pytest -q tests/runtime_ui/test_ui_foundations.py
\`\`\`

请在 1440×960、768×720、390×844、844×390 检查：焦点、ESC、遮罩点击、
嵌套弹框栈、超长内容滚动、文件选择、危险确认操作；特别是大尺寸设备配置、
报告诊断、固件烧写和雷达设置弹框。

此提交处于独立临时分支，未部署私有内网环境，**未声明视觉回归通过**。
审核通过并完成 CI 后，再由维护者合并到 main。


## 2026-11-16 第二版：逐个重构，而不是全局套皮

参考图是唯一视觉基准：以 **Tailscale 组网** 的大尺寸切角折线框、标题图标、逐行图标卡、底部操作横栏为依据。现有 760px 紧凑列表不能只改边框颜色；必须调整真实 HTML 与布局。

本分支新增的代码由三层组成：

1. `modal-tech.css`：从第一版保留的按钮色和输入色兼容层。不能代替独立重构。
2. `modal-tech-dialogs.css`：真正的八角截角 SVG-like CSS 边框、阶梯状标题下划线、底栏折线和动作按钮；Tailscale 专属 1300px / 五行 grid / 图标/字体比例与参考截图匹配。全部使用纯色，**无 CSS gradient()**。
3. `modal-tech-cases.css`：每个弹框专属内容层。固件用设备/恢复选项两列；报告拷贝用来源/目标两列；SSH 认证用图标输入卡；路由检测使用现有真实 Ping 结果区；报告诊断保留多页签信息结构；危险确认仍使用警告语义。

已逐个修改 `web/shell/dialogs/` 40 个 HTML 模板，增加各自的 `tech-dialog--<name>` 类与图标标题，并分别调整内部分区。在 Redmine 的 11 个、Gerrit 的 7 个、主机集群的 2 个、设备串口的 1 个弹框中也增加独立 DOM 标识；Shell 动态 Agent 分析弹框同样处理。**不伪造不存在的状态、字段或按钮**：参考图如果包含旧系统没有的操作，不应仅为了像图片而加假按钮。

### 本地必须进行的真实截图验收

```bash
git fetch origin ui/purple-tech-modal-system-20261116
git switch -c review-purple-tech-modals FETCH_HEAD

python tools/scripts/maintenance/capture_modal_gallery.py \
  --url 'https://127.0.0.1:5001/#test' --insecure \
  --ids tailscale-info-modal,firmware-modal,report-copy-modal,username-detect-modal

# 查看 artifacts/modal-gallery/index.html 与 PNG
```

若需要登录，可传 `--storage-state /path/to/playwright-auth.json`，避免将账号凭据硬编码进脚本。更多独立 iframe 中的弹框请直接在其业务页面打开后截图核对。截图来自**实际浏览器渲染**，不是 AI 模拟效果图；建议对照用户给出的 1672×941 Tailscale 模板，检查多栏、间距、阴影、切角及小屏滚动。

### 安全、测试与限制

- 不修改业务接口、用户租约、ModalManager 的显示/退出及焦点协议；保留原控件 ID 与操作绑定。
- 变更通过新结构测试 `tests/architecture/test_individual_modal_redesign.py` 和原有 `tests/runtime_ui/test_modals.py` 进行检查。
- 所有弹框仍待**用户内网实际渲染截图验收**；没有在内网验收的前提下，不可宣称与参考图片逐像素一致。
- 浏览器原生 alert/confirm、第三方外部网页、非弹框抽屉不属于可控 DOM 目标。
