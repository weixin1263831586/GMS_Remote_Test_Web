# ADR-0001: Controller 与 Worker 的职责边界

- 状态：已采纳（Accepted）
- 日期：2026-09（源码审计定稿）
- 关联代码：`app.py`、`bootstrap/`、`features/cluster/`、`worker_agent/`

## 背景

GMS 测试执行涉及两类物理上分离的主机：

- 运行 Web 平台、数据库与外部集成的 **Controller**（Linux，FastAPI）；
- 实际连接 Android 设备并执行 CTS / GTS / VTS / STS 的 **Worker**（Linux，需 ADB / Fastboot / Java / Tradefed / USB/IP 能力）。

若把测试执行、设备访问和 Web 编排放在同一进程，会导致：

- 测试主机必须暴露 Web 面，扩大攻击面；
- 设备必须与 Web 服务同机，无法组池；
- 长时间 Tradefed 任务与 Web 请求生命周期互相拖累；
- 多套测试并行只能垂直扩展单机。

同时，业界常见替代方案（Controller 直接 SSH 到测试机执行命令）缺乏注册、心跳、丢失检测与任务回收语义，难以支撑可恢复的设备池。

## 决策

采用 **Controller + Worker Agent 双角色**架构：

1. **Controller** 只负责：Web UI、认证与权限、任务调度与 Job/Command 队列、集群状态、报告与 Artifact 汇总、配置、外部系统集成（Gerrit / Redmine / AI / OpenGrok）。
2. **Worker Agent** 是独立进程，负责：设备探测、ADB / Fastboot / Tradefed 执行、USB/IP Client attach/detach、Suite 扫描与库存、Artifact 上传、Host Metrics。
3. 两者之间只通过 **带 Worker Token 的 HTTP(S) API** 通信：Worker 启动时注册，Controller 返回 `session_id` / `connection_generation` / `heartbeat_interval`；之后 Worker 周期性 Heartbeat（上报主机资源、Android 设备、运行中测试、命令状态、Suite Inventory、被撤销的 Device Claim），并轮询与 ACK 命令。
4. Controller 负责 **丢失检测与任务回收**：Worker 超时未 Heartbeat 时回收其 Job 与 Device Claim。
5. Worker 支持 `source_only: true` 模式：只承担设备来源 / 传输角色，不执行测试。

Controller 与 Worker 不信任客户端拼出的任意 Shell `argv`；执行规范在 Controller / Worker 之间共享并由受控代码构造。

## 理由

- **安全边界清晰**：设备所在主机只需对 Controller 暴露出站 HTTPS，无需开放入站执行通道；Worker Token 按 Worker ID 独立管理，可单独吊销。
- **可恢复性**：session + generation 模型使 Worker 重启、网络抖动后的 reconciliation 有明确语义（USB/IP attach 状态、Suite 库存、运行中任务都能重新对齐）。
- **可扩展性**：多 Worker 组池并行跑多套 CTS / GTS；Device Claim / Lease 保证同一物理设备不被并发占用。
- **与 USB/IP 组合**：Worker 是 USB/IP Client（`vhci_hcd`），只有 Worker 侧进程需要 USB/IP 内核能力，Controller 不感知。

## 后果

- Worker 与 Controller 之间是最终一致视图：设备状态、Suite 库存依赖 Heartbeat / 上报，存在短暂滞后；UI 需要呈现 `reconnecting` 等中间态。
- Worker 丢失会中断运行中的 Tradefed 任务，Controller 只能回收状态，不能跨主机续跑任务。
- 部署面变宽：每个 Worker 主机需要独立安装依赖（ADB / Fastboot / Java / usbip / noVNC 组件）并维护 Token。
- 生产模式下 Worker 强制要求 HTTPS Controller URL（`GMS_ENV=production` 时拒绝 HTTP），Worker Token 文件必须 `0600`。
