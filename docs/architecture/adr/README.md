# ADR 索引（docs/architecture/adr/）

架构决策记录总索引。新增 ADR 时在表尾追加一行；决策被后续 ADR 取代时，
不要改写历史 Decision 正文——在旧 ADR 顶部加 supersession 注记，并更新
本表的 `Superseded by` 列（参照 ADR 0008 与 ADR 0014 的处理方式）。

| ID | Title | Status | Supersedes | Superseded by |
|----|-------|--------|------------|---------------|
| 0001 | Controller 与 Worker 的职责边界 | Accepted | — | — |
| 0002 | Feature 与 Foundation 的包边界 | Accepted | — | — |
| 0003 | Agent Profile Store 唯一实现与 Fail-closed 选择契约 | Accepted | — | — |
| 0004 | SSH 执行边界与 shell=True 白名单 | Accepted | — | — |
| 0005 | USB/IP 完整固件烧写的所有权交还（Source-side Flash） | Accepted | — | — |
| 0006 | Agent Service 边界 —— Service Token / Approval Token / Enrollment | Accepted | — | — |
| 0007 | 按用户隔离的配置存放在 configs/secrets，而不是 data/ | Accepted | — | — |
| 0008 | Daily Brief separates triage from diagnosis | Superseded | — | 0013 |
| 0009 | Preserve kkagent diagnostic summaries | Accepted | — | — |
| 0010 | Actor 与 ResourceOwner 身份分离 | Accepted | — | — |
| 0011 | 设备归属与集群状态的跨库一致性 | Accepted | — | — |
| 0012 | 后台编排机器授权与 ATS 执行链身份 | Accepted | — | — |
| 0013 | Daily Brief batch analysis matches single-issue diagnosis | Accepted | 0008 | — |
| 0014 | External knowledge federation (android-internals-wiki, background only) | Accepted（triage 限制部分被 0013 取代，见文内注记） | — | — |
| 0015 | Allowlisted UI assets and domain modules | Accepted | — | — |
| 0016 | [Account-scoped Cluster job submission receipts](0016-cluster-job-submission-receipts.md) | Accepted | — | — |
| 0017 | [One approval for APK transmission and installation](0017-reviewed-apk-deployment.md) | Accepted | — | — |
| 0018 | [Finite app-scoped Agent debugging sessions](0018-scoped-debug-sessions.md) | Accepted | — | — |
| 0019 | [对话 Agent 会话持久化与崩溃恢复](0019-agent-session-persistence-recovery.md) | Accepted | — | — |
| 0020 | [人类预授权的测试计划与有界失败用例重试](0020-scheduled-test-plans-and-bounded-retries.md) | Accepted（补充 0012 的预授权派生规则） | — | — |
| 0021 | [ATS 执行选项、观测与证据保留](0021-ats-execution-options-and-evidence-retention.md) | Accepted | — | — |
| 0022 | [Web AOA 交互录制与无 ADB USB 恢复](0022-web-aoa-recording-and-usb-recovery.md) | Accepted | — | — |
| 0023 | [AOA 直接触摸屏与稳定的 HID 设备集合](0023-aoa-direct-touchscreen-and-stable-hid-set.md) | Accepted | — | — |
| 0024 | [AOA 指针兼容与显式输入模式](0024-aoa-pointer-compatibility-and-input-modes.md) | Accepted（补充 0023） | — | — |
| 0025 | [AOA 持久调试会话生命周期](0025-aoa-debug-session-lifecycle.md) | Accepted | — | — |
| 0026 | [AOA 多点仿射标定与映射策略](0026-aoa-multi-point-affine-calibration-and-mapping-policy.md) | Accepted（验证规则由 0027 取代） | — | 0027（部分） |
| 0027 | [AOA 映射验证与跨边界传递](0027-aoa-validated-mapping-boundaries.md) | Accepted | 0026（部分） | — |
| 0028 | [AOA 固件绑定恢复与严格环境验证](0028-aoa-firmware-bound-recovery-and-strict-environment.md) | Accepted（补充 0027，标准路径由 0029 补充） | — | 0029（部分） |
| 0029 | [AOA 设备类别认证与语义回放](0029-aoa-class-certification-and-semantic-playback.md) | Accepted | 0028（部分） | — |
