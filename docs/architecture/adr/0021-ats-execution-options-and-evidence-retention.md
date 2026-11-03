# ADR 0021：ATS 执行选项、观测与证据保留

- Status: Accepted
- Date: 2026-10-31
- Related: [ADR 0004](0004-ssh-execution-boundary.md)、[ADR 0020](0020-scheduled-test-plans-and-bounded-retries.md)

## Context

GMS 需要借鉴 ATS 的设备准备、分片、运行进度和资源管理，同时保留现有执行体系、
设备租约、账号隔离及归档报告的结果权威性。

## Decision

1. 执行选项由 Foundation 统一校验，写入配置快照和提交指纹，经现有 SSH 边界交给
   Worker。准备动作仅允许预定义、可重复的 ADB argv；顺序可配置，总预算 20 秒，
   单命令最多 5 秒。准备的设备集合必须等于被围栏保护的任务设备集合。
2. 显式分片数不得大于当前租约设备数，传入已有 Tradefed 启动器；不自行拆分模块或
   跨 Worker 合并结果。旧 Worker 缺少能力声明时拒绝高级选项，不能静默忽略。
3. 排队、执行和输出空闲预算可按运行配置；零值沿用原有默认行为。取消通过现有
   编排清理路径。空闲计时必须绑定所有者、job 和当前 attempt。
4. 模块进度从日志尽力提取，保留原日志，按 job/attempt 存储。模块计数和观察耗时
   仅供运行观测，不能替代最终报告，也不能据此宣告整套用例通过。
5. 套件哈希由用户显式触发计算，不在 GET 或页面初始化时扫描大文件。Worker 以
   SHA-256 验证缓存和下载；校验失败保留原文件，不覆盖为不可信内容。
6. 证据保留策略默认停用，按人类账号管理。仅终态任务的 stdout/stderr 可压缩，
   只有本功能生成的 retained_log 可到期删除；报告/XML/结果文件及 pin 不参与。
   每次操作绑定 artifact ID、预期 SHA 和操作类型；使用事务和恢复日志处理文件系统
   与数据库提交之间的中断，不做无索引目录清扫。
7. 测试计划批量修改在一个事务内检查全部所有者和版本。导出不携带执行授权及秘密，
   导入重新验证权限且强制暂停，不因文件导入启动测试或自动调度。

## Consequences

需同步更新 Controller、Automation Worker 和执行 Worker；能力协商允许旧 Worker
继续使用原有基础选项。准备动作仅覆盖 ADB 已可用的测试前设置。User build 在刷机后
恢复 USB 调试需要独立的 AOA 阶段，见 [接入分析](../../ats-user-builds-aoa-analysis.md)。

日志清理只覆盖 Controller 已登记的副本；Worker 原始目录及外部构建产物不在此策略范围。
压缩单文件上限 256 MiB；手动每批最多 25 项，自动每账号每小时最多 5 项。
