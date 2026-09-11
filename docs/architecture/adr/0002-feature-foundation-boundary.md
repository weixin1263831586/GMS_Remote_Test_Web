# ADR-0002: Feature 与 Foundation 的包边界

- 状态：已采纳（Accepted）
- 关联代码：`features/`、`foundation/`、`tests/architecture/`

## 背景

平台覆盖测试执行、设备、固件、构建、集群、报告、Redmine / Gerrit / AI 等大量业务域。早期单体式组织（逻辑堆在 `app.py` 与少量大模块）随功能增长出现：

- 跨域 import 蔓延，形成循环依赖；
- SSH、进程执行、配置读取等基础能力在每个 Feature 内各写一份，安全修复无法收敛；
- CI 难以用静态规则约束架构，回归只能靠 code review 兜底。

## 决策

代码组织遵循单向分层：

```text
Feature
  ↓
Foundation / Port
  ↓
Infrastructure
```

1. `features/` 按业务域拆分（assistant、auth、automation、build、cluster、devices、email、firmware、gerrit、knowledge、redmine、reports、system、test_execution、users 等），每个 Feature 内聚自己的 routes / services / repositories / tests，路由 handler 保持轻薄、逻辑下沉 service。
2. **Feature 之间不互相 import 对方内部模块**。跨 Feature 协作只能经由：
   - 对方 Feature 的公开包边界（public API）；
   - 下沉到 `foundation/` 的共享 Port。
3. 共享逻辑（配置加载、JSON 响应封装、安全、SSH 执行器、进程边界、循环 watchdog 等）统一放在 `foundation/`，由架构测试守护（如 `tests/architecture/test_shell_execution_boundary.py`、`tests/architecture/test_no_regression_rules.py`）。
4. 避免把逻辑重新堆回 `app.py`；`bootstrap/` 只负责应用装配、生命周期、路由注册与安全启动检查。
5. 前端页面不复制设备 / Job 真值状态，状态以 Controller API 为唯一来源。

## 理由

- **安全收敛**：把 `shell=True`、裸 `ssh.exec_command()` 等高风险执行面收敛到少数被架构测试审计的文件（见 [ADR-0004](0004-ssh-execution-boundary.md)），前提就是存在明确的 Foundation 边界。
- **可测试**：Feature 级 pytest 可以只挂载本 Feature 的依赖；架构测试可以静态扫描 import 与调用边界。
- **可演进**：单个 Feature 重写或拆分时，只要 public API 与 Foundation Port 契约不变，不影响其他域。

## 后果

- 新增跨域能力时必须先判断「这属于某个 Feature 还是 Foundation」，偶发地需要把已有 Feature 内部模块提升为公开 API 或下沉 Foundation，迁移成本高于直接 import。
- 架构测试是 CI Gate 的一部分：违反边界的 PR 在 `pytest tests/architecture` 阶段直接失败。
- 跨 Feature 读取需经由公开入口（例如固件烧写读取 Windows 源主机凭据经由 `foundation` 层 config_manager，而不是直接引用 devices feature 的内部实现）。
