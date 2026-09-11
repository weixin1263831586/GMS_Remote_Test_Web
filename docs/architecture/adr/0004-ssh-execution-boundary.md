# ADR-0004: SSH 执行边界与 shell=True 白名单

- 状态：已采纳（Accepted）
- 关联代码：`foundation/ssh_executor.py`、`foundation/processes.py`、`features/system/ssh.py`、`features/build/executor.py`、`tests/architecture/test_no_regression_rules.py`、`tests/architecture/test_shell_execution_boundary.py`

## 背景

平台大量依赖 SSH：Ubuntu 测试主机（本地 USB 设备操作、upgrade_tool 烧写）、Windows / Ubuntu USB/IP 来源主机（设备发现、usbipd 操作、固件 Source Agent）、SSH 构建服务器（受控 Build Template）。历史上：

- 各 Feature 直接调用 `paramiko` 的 `ssh.exec_command()`，并且常见「先读完 stdout 再读 stderr」的手工轮询写法。当远端输出较大时会触发 SSH channel 窗口互锁（双流都想写、缓冲区写满即死锁），实机出现过烧写日志流卡死；
- `subprocess` 调用散落各处，个别模块用 `shell=True` 拼接用户可控输入，形成注入面；
- 没有静态规则约束，上述问题随新代码不断回归。

## 决策

1. **唯一 SSH 执行层**：业务代码禁止直接调用 `ssh.exec_command()`；SSH 命令统一经由 `foundation/ssh_executor.py` 执行。执行器对 stdout / stderr 双流并发 drain（`get_pty=True` 时 stderr 并入 stdout），支持逐行流式回调、超时与退出码提取。
2. **唯一登记的例外**：`features/system/ssh.py`（连接健康检查）允许使用原始 `exec_command` —— 它需要 raw channel 的超时语义（`recv_exit_status` 无超时参数），详见其模块 docstring。
3. **shell=True 白名单**：`subprocess`（`shell=True` / `os.system` / `os.popen`）只允许出现在两个经审计的边界内：
   - `foundation/processes.py` — 受控本地进程边界；
   - `features/build/executor.py` — 构建服务器执行器（Build Template 本质是受控远程代码执行，由参数 Schema / `choices` / `pattern` 约束）。
4. 新增本地命令执行优先使用参数数组（例如 `run_local_command(["adb", "devices"])`）；只有确实需要 Pipeline / Redirect / Shell Program 的场景才使用受控 Shell Boundary。
5. 上述规则由架构测试静态守护并纳入 CI：
   - `tests/architecture/test_no_regression_rules.py`：扫描 `features/`、`foundation/`、`worker_agent/`、`workflows/`、`bootstrap/` 中的 `.exec_command(`，白名单外直接失败；
   - `tests/architecture/test_shell_execution_boundary.py`：AST 扫描 `shell=True`，白名单外直接失败。
6. SSH 连接统一使用严格 Host Key 校验（`foundation/ssh_security.py` 的 `configure_strict_host_keys`）；生产环境不得关闭 Host Key 检查来「解决」首次连接问题。高风险链路（如 Windows Source Agent 固件烧写）必须走严格校验。

## 理由

- 双流并发 drain 是对实测 channel 窗口死锁的结构性修复；把该语义收敛到一个执行器后，任何绕过它的代码都会被架构测试拒绝。
- `shell=True` 的风险无法靠调用点自律保证，只能靠「少数可枚举、可审计的文件 + CI 强制」收敛。
- 健康检查的例外是真实技术需求（超时语义），保留在白名单中并要求注释说明，比让它伪装成普通执行更诚实。

## 后果

- 新增 SSH 调用必须 import foundation 执行层并适配其结果对象（`result.ok` / `result.code` / `result.stdout` / `result.stderr`）；偶尔需要 raw channel 语义时必须显式登记白名单并写明理由，否则 CI 失败。
- 执行器成为平台 SSH 行为的单点：对超时、编码、PTY 行为的修改会影响所有 Feature，需要回归测试（`features/system/tests/test_ssh_executor.py` 等）。
- Build Template 与 Shell Boundary 的参数约束（`trusted_shell_fragment` 必须 `pattern` 或 `choices`）成为安全契约的一部分，修改这些机制属于高危变更。
