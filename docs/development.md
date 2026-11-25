# 开发者指南

> 本文档面向在本仓库工作的开发者，说明仓库布局与依赖方向、架构门禁、
> 测试组织、常用命令、文档政策与提交前检查清单。根
> [AGENTS.md](../AGENTS.md) 是工作规则的权威来源，本指南与其冲突时以
> AGENTS.md 为准。

## 仓库布局

```text
bootstrap/       应用装配、生命周期、路由注册、生产安全自检（不堆业务逻辑）
foundation/      共享基础能力：配置、安全、SSH 执行器、进程边界、上传、
                 秘密、数据库、响应封装、端口（Port）
features/        按业务域拆分的 routes / services / repositories / tests
worker/    Worker 侧执行与 Controller 通信
agent/gms-remote-test/  手工维护的 CLI / MCP / Skill / 清单 / 包生命周期源码
plugins/gms-remote-test/ 由 sync 生成的插件载荷（禁止手改）
web/             浏览器 shell 与静态前端（shell / static / templates）
tests/           architecture / contract / unit / integration / e2e / reliability / soak
tools/           外部工具 + 项目维护脚本 tools/scripts/<category>/（见 tools/README.md）
scripts/         部署、运维与脱敏脚本
```

各层的职责与依赖方向：

- `bootstrap/` 只负责装配、生命周期、路由注册与启动期安全校验，避免把逻辑
  重新堆回单体应用入口。
- `foundation/` 承载共享逻辑（配置加载、JSON 响应、安全、SSH 执行器、进程
  边界、循环 watchdog 等）。**`foundation/` 永不 import `features/`。**
- `features/` 按业务域内聚（assistant、auth、automation、build、cluster、
  devices、email、firmware、gerrit、knowledge、redmine、reports、system、
  test_execution、users 等）。路由 handler 保持轻薄，逻辑下沉 service。
- **Feature 之间不互相 import 对方内部模块**，跨 Feature 协作只能经由对方
  的公开包边界或下沉到 `foundation/` 的共享 Port。
- `worker/` 负责设备探测、ADB/Fastboot/Tradefed 执行、USB/IP、Artifact
  上传等 Worker 侧工作。Worker 使用带 Token 和 session/generation 的 HTTP(S)
  注册、心跳、轮询命令与 ACK；Controller 将类型化任务写入受授权的命令队列
  （ADR 0001）。需要 SSH 的主机操作统一走审计执行器（ADR 0004）。

依赖方向：

```text
features/            →  foundation/  →  infrastructure
worker/        →  foundation/
bootstrap/           →  foundation/ / features/
```

以上由 `tests/architecture/test_dependency_rules.py` 静态守护：`foundation/`
不得 import `features/`；Feature 不得 import 其它 Feature 的深层内部模块
（`features.<other>.<...>`）；不得 `from features.<other> import _private`；
新代码不得 import 已废弃的 `core` / `routers` / `modules` 顶层包。

## 架构门禁（tests/architecture）

架构门禁是 CI Gate：违反边界的改动在 `pytest tests/architecture` 阶段直接失败。
常用「棘轮」模式是——历史债务登记为显式预算，预算**只减不增**。

- **源码文件行数预算**：Python 与第一方 JS/CSS/HTML 均采用 2000 行上限，按业务内聚性、依赖方向及安全边界组织文件，超限文件按门禁预算管理，不能增加新的超限文件。
- **前端性能预算**：保留 `web/static/js/*.js` 首屏基础 JS 总量 231 KB 的门禁。行数调整不允许改变字体、字号、间距、弹框、控件高度、路由、交互或视觉规范。第三方压缩资源不纳入第一方行数门禁。
- **SSH 边界**（`test_no_regression_rules.py`）：`.exec_command(` 只允许出现在
  `foundation/ssh_executor.py` 与登记的连接健康检查 `features/system/host/ssh.py`，
  其余业务模块一律经统一执行层。
- **shell 边界**（`test_shell_execution_boundary.py`）：AST 扫描
  `features/`、`foundation/`、`worker/`、`bootstrap/`，
  `shell=True` / `os.system` / `os.popen` 只允许在 `foundation/processes.py`
  与 `features/build/executor.py`。
- **路由授权**（`test_route_authorization.py`）：每个 state-changing `/api` 路由
  必须显式授权或登记到 `MIGRATION_ALLOWLIST`（上限 7，只减不增）。
- **依赖与私有符号**、**依赖版本**（`test_dependency_rules.py`、
  `test_dependency_security.py`：`cryptography`、`requests`、`requests-toolbelt`
  等需锁定含上游修复的精确版本）、**个人环境硬编码禁用**（
  `test_no_regression_rules.py` 扫描 `C:\Users\<name>`、`172.16.*` 等）。
- **Controller/Worker 精确边界**（`test_controller_worker_boundary.py`）：
  features 文件 → 精确 `worker.*` module 两级 allowlist（双向），
  新增文件/新增 worker module 都会失败；僵尸 allowlist 条目一并拦截。
- **Assistant 动态依赖**（`test_assistant_dynamic_imports.py`）：executor_ref /
  `_fetch_router_json` 字符串通道的目标模块/符号必须存在且公开、feature
  白名单 shrink-only、禁 `routers./core./modules.` 死通道；facade 未声明的
  跨 feature 模块登记在 `FACADE_PENDING_MODULES`（只减不增）。
- **公共表面可解析**（`tests/contract/test_feature_public_surfaces.py`）：
  每个 feature `__all__` 逐个 `getattr`，声明为 public 必须真正可 import。
- **AI 诊断质量**（`tests/quality/`）：诊断 golden corpus 语料契约 +
  `tools/scripts/testing/eval_diagnosis_quality.py` 确定性评分
  （evidence recall / 禁止声明 / 结论证据锚点），development/holdout 分池。
- **前端完整性静态扫描**（`tests/test_frontend_integrity.py`）：识别遗留
  `on*` 内联事件及 `data-click`/`data-change` 等 act-bridge 委托声明，
  检查处理函数可解析；CSP 禁止重新引入内联事件处理器，新增交互应使用
  data-* 委托或显式 `addEventListener`。另守护初始化去重、导航及关键
  业务弹框的事件契约。

边界决策记录见 [architecture/adr/](architecture/adr/)：
[0001](architecture/adr/0001-controller-worker-boundary.md) Controller/Worker、
[0002](architecture/adr/0002-feature-foundation-boundary.md) Feature/Foundation、
[0004](architecture/adr/0004-ssh-execution-boundary.md) SSH/shell 边界。
门禁与 AI 治理子系统的全景图见
[architecture/ai-governance.md](architecture/ai-governance.md)。

## 测试组织

测试按职责归属而非统一平铺：单个 Feature 的业务逻辑与 API 回归留在该域，
跨域装配、架构与契约、浏览器交互、发布部署和稳定性验证留在根测试树的对应类别。
同一用例只保留一个实际收集入口；移动用例时同步 CI 的显式路径、导入及 fixture，
不能用兼容转发测试重复收集。临时截图探针不得进入版本库；有长期回归价值的场景
应转成带明确断言的 `tests/runtime_ui/` 用例。

- **`features/<domain>/tests/`**：每个业务域的单元/接口测试，只挂载本 Feature
  的依赖。
- **`tests/unit/`**：跨 Feature 的 foundation 单元测试（端口、秘密自举、出站
  安全等）。
- **`tests/contract/`**：契约冻结测试。`test_api_contract.py` 比对
  `tests/contract/snapshots/` 下的 `routes.json`、`openapi.json`（及 UI/config
  快照）与当前应用；快照经 `tests/contract/generate_snapshots.py` 生成。改动
  对外契约时需显式更新快照并评审差异。
- **`tests/architecture/`**：上节的静态架构门禁。
- **`tests/integration/`、`tests/e2e/`、`tests/reliability/`、`tests/soak/`**：
  启动装配、前端交互与稳定性/耐久测试。
- **Agent 包与生成树分开跑**：`agent/gms-remote-test/tests`（源码）与
  `plugins/gms-remote-test/tests`（生成载荷）必须在**独立 pytest 进程**中分别
  运行，随后跑秘密扫描与 `tools/scripts/agent/release.py --check`。参考
  `agent/gms-remote-test/skill/references/project-map.md` 的测试路由表。

`conftest.py` 在应用导入前设置测试环境（`GMS_SKIP_RUNTIME_ENV=1`、
`GMS_AUTH_REQUIRED=false`）以隔离部署配置；专门的边界测试会覆盖这些变量以
验证生产行为。

## 常用命令

```bash
# Python：定向 Ruff（line-length = 120，见 pyproject.toml）+ 本 Feature 的 pytest
ruff check features/<domain>
python -m pytest features/<domain>/tests -q
python -m pytest tests/architecture -q     # 架构门禁

# Shell：语法检查 + CLI 契约测试
bash -n <script>.sh
python -m pytest features/system/tests/test_skill_cli.py -q

# 前端：JS 语法 + 重复导航行为
node --check web/static/js/<file>.js
python -m pytest tests/test_frontend_integrity.py -q

# Agent 包一键自检与发布（Makefile）
make agent-check              # 双向 pytest + secrets 扫描 + release --check
make agent-release V=X.Y.Z    # 改版本号（内部自动 sync）→ 复跑完整自检
make agent-sync-check         # 校验 sync_package 幂等（plugins/ 无漂移）

# 源树改动后同步生成树
python tools/scripts/agent/sync_package.py .
```

`make agent-check` 的四步顺序（两个独立 pytest 进程 → 秘密扫描 →
`tools/scripts/agent/release.py --check`）是硬性要求，勿合并为单进程。

## 项目操作指导与架构图

“导航设置 → 项目操作指导”的说明与图注维护在
`web/shell/dialogs/sidebar-visibility.html`。操作入口、确认方式、模型行为或
功能变化时，同步核对说明和 `web/static/images/guide/` 下的截图。

运行 `python tools/scripts/docs/generate_guide_screenshots.py` 可重新生成全部
指导图。脚本启动临时认证实例，用 `guide_fixtures.py` 的演示数据渲染当前
页面，不连接真实测试主机或模型、不执行测试或烧写。网页截图区域为
1912 × 914，编号放在控件外侧。底部附加 144 像素高的编号说明，成品 PNG
统一为 1912 × 1058。步骤文案取自图库按钮的 `data-guide-steps`。
全部生成且与指南引用清单一致后才替换目标文件。
编号标记对应当前截图可见的控件。需滚动后操作的步骤会在图注中说明。
不要把真实配对码、Token、密码或业务日志放进截图。

只修订个别截图时可追加 `--only agent-routing.png cluster-refresh.png`；脚本
只替换指定图片，全量生成时仍校验画廊引用与实际捕获完全一致。模型状态
通过真实页面的加载函数读取演示接口，保存前验证可用状态，避免异步覆盖。

架构拓扑的节点与连线以 `docs/architecture/*-topology.mmd` 为准，展示布局在
`features/system/topology_view.py`；节点统一 14 px，连线标签统一 12 px，
标签沿连线排列，反向斜线保持文字可读。SVG 按原始尺寸显示，窄视口在图内
滚动，避免四个视图因缩放比例不同导致字号不一致。修改后运行拓扑契约测试
与 `tests/runtime_ui/test_architecture_views.py`，检查边方向、文字边界、标签
重叠以及重复切换后的显示。

## 文档政策

- **说明简洁、必要、准确**：代码注释、docstring、脚本说明和现行指南只描述当前职责、行为、输入输出、限制及必要的安全或技术依据。无需复述代码，不记录修改历史、旧方案对比、截图或评审过程。保留必要的规范引用、协议约束、证据出处和工具指令。由 `tests/architecture/test_review_markers.py` 与 `tools/scripts/maintenance/audit_comment_hygiene.py` 检查。
- **架构决策写入 `docs/architecture/adr/`**，采用 `NNNN-title.md` 命名，按既有
  ADR 的结构（背景 / 决策 / 理由 / 后果）撰写，并在「关联代码」中标明受约束的
  模块与架构测试。
- **生成的树 `plugins/` 禁止手改**：任何 Agent 包源改动都要在
  `agent/gms-remote-test/` 下完成，随后用
  `python tools/scripts/agent/sync_package.py .` 同步生成 `plugins/`，并跑
  `make agent-sync-check` 确认无漂移。

## 提交前检查清单

1. 变更被限定在请求波及的文件/模块内；未触碰本地 `configs/` 与 `data/` 下的
   密码、令牌、cookie、证书、运行配置、数据库与日志。
2. Python：`ruff check` 通过，本 Feature 的 `pytest` 通过，`pytest tests/architecture`
   通过（行数预算只减不增、SSH/shell 边界、路由授权均满足）。
3. Shell：`bash -n` 通过，CLI 契约测试通过。
4. Agent 包：源码与 `plugins/` 生成树分别跑 pytest，秘密扫描通过，
   `tools/scripts/agent/release.py --check` 通过（`make agent-check`）。
5. 前端：`node --check` 与前端完整性/重复导航检查通过。
6. 对外契约变更同步更新 `tests/contract/snapshots/` 并在评审中说明差异。
7. 架构决策或安全边界变化已补入 `docs/architecture/adr/`。

## 相关文档

- [../AGENTS.md](../AGENTS.md) — 仓库工作规则（权威）
- [architecture/overview.md](architecture/overview.md) — 架构总览
- [architecture/adr/](architecture/adr/) — 架构决策记录
- [security.md](security.md) — 平台安全模型总览
- [agent/gms-remote-test/skill/references/project-map.md](../agent/gms-remote-test/skill/references/project-map.md) — 归属与测试路由
- [deployment/configuration.md](deployment/configuration.md) — 配置项与环境变量
