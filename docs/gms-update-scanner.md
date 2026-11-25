# GMS 更新统一扫描服务

## 执行入口

所有生产扫描统一由 features.gms.cli 执行。更新中心使用
POST /api/gms-update-center/scan?mode=full（可重复指定 source），
GET /api/gms-update-center/scan/status 查看状态，
GET /api/gms-update-center/scan/runs/{run_id} 查看源和文档覆盖证据。
更新雷达只负责调度和通知，使用同一执行器和同一文件锁。

默认数据根中的 gms_update.sqlite3 是唯一业务扫描数据库。新安装和升级
均从新库建立基线，不读取、不迁移旧扫描数据库。旧库文件不会自动删除。
雷达配置和通知历史使用独立的 gms_update_radar_v2.sqlite3；首次切换需重新
设置计划和收件人。所有业务查询与扫描使用 /api/gms-update-center 前缀，旧 API 不再注册。
扫描、查询、网页、Gerrit 匹配、原始文档解析和证据召回均位于 features/gms。
雷达位于同一业务域 features/gms/radar，独立负责通知和调度，不持有独立扫描器。

## 命令

从项目根执行：

- python3 -m features.gms.cli --mode full
- python3 -m features.gms.cli --mode incremental
- python3 -m features.gms.cli --mode full --source mainline_preload
- python3 -m features.gms.cli --mode full --audit

全量 Mainline 默认覆盖所有发现的历史 PRELOAD 和 Release Notes。
可显式指定 --mainline-month-depth N 限制 PRELOAD 历史范围；默认 0 表示
不限历史月份，豁免/CVE 仍读取所有发现的发布说明。
只读核对在临时数据库中执行，不写入业务数据库；输出汇总状态。

## 发布与覆盖

每轮一次认证会话；同一 Mainline 文档只获取一次，在共享 DOM 上解析
PRELOAD、Known Issues、CVE。来源失败不清空表格；Mainline 子页不可用时
只发布已验证的记录，不删除原先记录，覆盖结果标为 partial。
HTTP 404、登录、重定向或解析错误均不能视作正常空结果。
source result 与 document evidence 保存真实覆盖、指纹和状态。
进程异常退出后，下次持锁扫描将未终态批次标记中断。

变更事件标记 baseline（首次基线）、reindex（重建索引）、
source_changed（源文档指纹改变）或 partial（部分覆盖补录）。
这些都是本地观察而非 Google 官方发布时间；元数据刷新不作为业务变更。
源文档变化仅说明本轮证据变化，不保证每条事件都是官方新发布。

## 验收

Action Runner 在临时数据库实际执行全量扫描和重复扫描。
核对七个数据源、八张表的完整分页、原始文档覆盖和同一指纹下的幂等性。
任何源失败或覆盖不完整都使验收失败，不以非零解析数量作为成功依据。
