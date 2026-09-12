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
worker_agent/    Worker 侧执行与 Controller 通信
agent/gms-remote-test/  手工维护的 CLI / MCP / Skill / 清单 / 包生命周期源码
plugins/gms-remote-test/ 由 sync 生成的插件载荷（禁止手改）
web/             浏览器 shell 与静态前端（shell / static / templates）
tests/           architecture / contract / unit / integration / e2e / reliability / soak
tools/           包发布、同步、校验工具（如 release_agent.py、sync_agent_package.py）
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
- `worker_agent/` 负责设备探测、ADB/Fastboot/Tradefed 执行、USB/IP、Artifact
  上传等 Worker 侧工作，与 Controller 只通过带 Worker Token 的 HTTP(S) 通信。

依赖方向：

```text
features/            →  foundation/  →  infrastructure
worker_agent/        →  foundation/
bootstrap/           →  foundation/ / features/
```

以上由 `tests/architecture/test_dependency_rules.py` 静态守护：`foundation/`
不得 import `features/`；Feature 不得 import 其它 Feature 的深层内部模块
（`features.<other>.<...>`）；不得 `from features.<other> import _private`；
新代码不得 import 已废弃的 `core` / `routers` / `modules` 顶层包。

## 架构门禁（tests/architecture）

架构门禁是 CI Gate：违反边界的改动在 `pytest tests/architecture` 阶段直接失败。
常用「棘轮」模式是——历史债务登记为显式预算，预算**只减不增**。

- **文件行数预算**（`test_file_size_rules.py`）：`bootstrap/`、`foundation/`、
  `features/`、`workflows/` 下**未被登记**的新 Python 模块上限 **600 行**；
  历史超限模块登记在 `MIGRATION_LINE_LIMITS` 中，不得超过各自登记值（债务可
  缩小、不可增长）。真正拆小文件后应同步收紧预算值，可用
  `python tools/update_size_baseline.py --shrink-only` 一键把预算降到当前
  实际行数（只降不升；增长中的文件保持原预算让门禁继续报警）。
- **前端体积预算**（`test_frontend_size_rules.py`）：扫描 `web/shell/`、
  `web/static/css/`、`web/static/js/`。未登记文件默认上限 HTML **100 KB**、
  JS **50 KB**、CSS **50 KB**；历史超限文件登记在 `MIGRATION_BYTE_LIMITS`
  且只减不增；`web/static/js/*.js` 首屏基础 JS 总量不得超过 **231 KB**。
- **SSH 边界**（`test_no_regression_rules.py`）：`.exec_command(` 只允许出现在
  `foundation/ssh_executor.py` 与登记的连接健康检查 `features/system/ssh.py`，
  其余业务模块一律经统一执行层。
- **shell 边界**（`test_shell_execution_boundary.py`）：AST 扫描
  `features/`、`foundation/`、`worker_agent/`、`workflows/`、`bootstrap/`，
  `shell=True` / `os.system` / `os.popen` 只允许在 `foundation/processes.py`
  与 `features/build/executor.py`。
- **路由授权**（`test_route_authorization.py`）：每个 state-changing `/api` 路由
  必须显式授权或登记到 `MIGRATION_ALLOWLIST`（上限 7，只减不增）。
- **依赖与私有符号**、**依赖版本**（`test_dependency_rules.py`、
  `test_dependency_security.py`：`cryptography`、`requests`、`requests-toolbelt`
  等需锁定含上游修复的精确版本）、**个人环境硬编码禁用**（
  `test_no_regression_rules.py` 扫描 `C:\Users\<name>`、`172.16.*` 等）。
- **前端完整性静态扫描**（`tests/test_frontend_integrity.py`）：解析 HTML 内联
  事件处理（`onclick` 等）并比对页面定义的函数/符号，确保内联处理器可解析；
  同时固化一批前端行为回归断言（初始化去重、导航引导失败即停、登录/烧录/集群
  等关键页面的交互契约）。

边界决策记录见 [architecture/adr/](architecture/adr/)：
[0001](architecture/adr/0001-controller-worker-boundary.md) Controller/Worker、
[0002](architecture/adr/0002-feature-foundation-boundary.md) Feature/Foundation、
[0004](architecture/adr/0004-ssh-execution-boundary.md) SSH/shell 边界。

## 测试组织

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
  运行，随后跑秘密扫描与 `tools/release_agent.py --check`。参考
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
make agent-sync-check         # 校验 sync_agent_package 幂等（plugins/ 无漂移）

# 源树改动后同步生成树
python tools/sync_agent_package.py .
```

`make agent-check` 的四步顺序（两个独立 pytest 进程 → 秘密扫描 →
`tools/release_agent.py --check`）是硬性要求，勿合并为单进程。

## 文档政策

- **源码注释不得引用不存在的评审文档编号**：注释里只应引用真实存在的 ADR
  或文档；不得出现带日期的审计/评审轮次引用、审计分节号或评审编号等
  已废弃标记。该政策由
  `tests/architecture/test_review_markers.py` 静态强制；清理存量时把引用
  改为指向真实 ADR（如 `ADR 0006`）或直接描述行为本身。
- **架构决策写入 `docs/architecture/adr/`**，采用 `NNNN-title.md` 命名，按既有
  ADR 的结构（背景 / 决策 / 理由 / 后果）撰写，并在「关联代码」中标明受约束的
  模块与架构测试。
- **生成的树 `plugins/` 禁止手改**：任何 Agent 包源改动都要在
  `agent/gms-remote-test/` 下完成，随后用
  `python tools/sync_agent_package.py .` 同步生成 `plugins/`，并跑
  `make agent-sync-check` 确认无漂移。

## 提交前检查清单

1. 变更被限定在请求波及的文件/模块内；未触碰本地 `configs/` 与 `data/` 下的
   密码、令牌、cookie、证书、运行配置、数据库与日志。
2. Python：`ruff check` 通过，本 Feature 的 `pytest` 通过，`pytest tests/architecture`
   通过（行数预算只减不增、SSH/shell 边界、路由授权均满足）。
3. Shell：`bash -n` 通过，CLI 契约测试通过。
4. Agent 包：源码与 `plugins/` 生成树分别跑 pytest，秘密扫描通过，
   `tools/release_agent.py --check` 通过（`make agent-check`）。
5. 前端：`node --check` 与前端完整性/重复导航检查通过。
6. 对外契约变更同步更新 `tests/contract/snapshots/` 并在评审中说明差异。
7. 架构决策或安边界变化已补入 `docs/architecture/adr/`。

## 相关文档

- [../AGENTS.md](../AGENTS.md) — 仓库工作规则（权威）
- [architecture/overview.md](architecture/overview.md) — 架构总览
- [architecture/adr/](architecture/adr/) — 架构决策记录
- [security.md](security.md) — 平台安全模型总览
- [agent/gms-remote-test/skill/references/project-map.md](../agent/gms-remote-test/skill/references/project-map.md) — 归属与测试路由
- [deployment/configuration.md](deployment/configuration.md) — 配置项与环境变量
