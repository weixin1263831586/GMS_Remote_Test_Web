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
