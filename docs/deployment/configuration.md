# 配置、密钥与部署迁移

本文描述当前配置路径与加载行为。配置文件修改后，环境变量需重启消费它们的进程才生效。
源码依据为 `foundation/config_paths.py`、`foundation/config.py`、
`foundation/runtime_config_store.py`、`bootstrap/env_loader.py` 和 `install.sh`。

## 目录与 Git 跟踪

| 路径 | 内容 | 管理方式 |
| --- | --- | --- |
| `configs/examples/` | 主配置、运行环境、集群、构建服务器与自动化方案的脱敏模板 | 按 `.gitignore` 白名单跟踪 |
| `configs/local/config.json` | 静态默认值、集成设置 | 本机配置，不入库 |
| `configs/local/deployment.json` | 测试主机、安装身份和路径 | 本机配置，不入库 |
| `configs/local/environment.json` | 非敏感环境变量、密钥文件路径 | 本机配置，不入库 |
| `configs/local/cluster.json` | 集群与 Controller 设置 | 本机配置，不入库 |
| `configs/local/build_servers.json` | 构建主机与命令模板 | 本机配置，不入库 |
| `configs/local/automation_profiles.json` | 自动化方案 | 本机配置，不入库 |
| `configs/local/layout.json` | 配置布局迁移标记 | 迁移工具维护 |
| `configs/secrets/environment.json` | 密码、API Key、Bootstrap / Metrics / Webhook Token | 私密配置，不入库 |
| `configs/secrets/runtime_credentials.json` | 加密 SSH 等运行配置凭据 | 私密配置，不入库 |
| `configs/secrets/worker_tokens.json` | Controller 持有的 Worker Token | 私密配置，不入库 |
| `configs/secrets/certs/` | 部署证书、私钥及可选 CA | 私密配置，不入库 |
| `configs/secrets/<feature>/by_user/<owner>/config_runtime.json` | Redmine / Gerrit 等账号配置和凭据 | 按账号隔离，不读取全局凭据 |

`configs/` 的实际跟踪白名单以仓库根 `.gitignore` 为准；不要把本机文件加入白名单。
仓库没有随代码交付的 AOA 设备命令 JSON。账号 Profile 需由工作台或 Profile API 保存，
文件放入本机目录不会自动导入；共享要求见 [AOA 用户指南](../aoa/user-guide.md#命令配置与共享约定)。

## 加载与覆盖

运行环境优先级为：**进程已有环境 > secrets/environment.json > local/environment.json**。
`${PROJECT_ROOT}` 按部署树根目录展开；静态配置支持 `${ENV_NAME:默认值}`。
环境加载在应用模块导入前完成。环境文件模板默认声明 production，开发时不要原样启用。

静态配置先读 `configs/local/config.json`，缺失时回退示例；运行时覆盖由
`ConfigManager` 和 `RuntimeConfigStore` 分类、合并及加锁保存，静态 `ai_models`
保留既有优先级。嵌套凭据从普通配置中提取，含凭据的列表整体保存在私密文件中。
主机、账号、套件路径等示例值只保证结构完整，不能表示实际 SSH 或设备能力已配置。

开发环境按需创建：

```bash
mkdir -p configs/local
cp -n configs/examples/config.example.json configs/local/config.json
cp -n configs/examples/cluster.example.json configs/local/cluster.json
cp -n configs/examples/build_servers.example.json configs/local/build_servers.json
cp -n configs/examples/automation_profiles.example.json configs/local/automation_profiles.json
```

`GMS_DATA_ROOT` 控制运行数据根；配置默认仍位于项目 `configs/`。
`GMS_CLUSTER_CONFIG`、`GMS_WORKER_TOKENS_FILE` 等显式路径覆盖继续有效。
构建模板的 `{参数}` 和配置的 `${环境变量}` 使用不同的展开规则。

## 身份密钥与 data/ 的保留范围

`data/` 包含持久状态，不能整体视为缓存：

| 内容 | 当前来源与处理 |
| --- | --- |
| 加密主密钥 | `GMS_SECRET_KEY_FILE` 优先；未指定时优先 `configs/secrets/master.key`，兼容已有 data 根下的 `secrets/master.key` |
| 审计 HMAC 密钥 | `GMS_AUDIT_HMAC_KEY_FILE` 优先；未指定时优先 `configs/secrets/audit_hmac.key`，兼容已有 data 根下的同名密钥 |
| Agent 包签名密钥 | 由 `GMS_SKILL_SIGNING_KEY_FILE` 指定；production 分发要求有效 Ed25519 私钥 |
| 安装器生成的密钥 | `install.sh` 目前仍在 `data/secrets/` 生成主密钥、审计密钥、本地 Worker Token，并默认生成签名密钥；环境文件保存实际引用 |
| 本地 Worker 配置 | 安装器生成于 `data/local-worker/config.json`；包含 Worker 身份、Controller 和路径引用 |
| 认证状态 | `data/platform_auth.sqlite3` 保存账号、会话及 Agent Token 等状态，不能从源码重建原身份 |
| 账号 AOA 资产 | 数据根下的 Profile、标定与类别认证数据库；只恢复配置文件不会恢复这些资产 |
| 用户设置与设备状态 | `data/settings/`、`data/devices/` 等，按需要保留 |
| Run、Job、报告、日志、附件和镜像 | 业务状态及证据，按保留政策备份或明确重新开始 |

私密目录使用 0700、私密文件使用 0600。密文和解密密钥应作为一组保留。
审计日志必须使用能验证其现有 HMAC 链的密钥；换一把新密钥不能修复原链。

## 重新克隆后仅恢复必要配置

`git clone` 只恢复代码和模板。`git status` 干净不代表本机配置齐全；
`restart_services.sh` 能启动也只说明服务健康检查通过，不代表 SSH、Worker 和外部服务已恢复。

只迁移配置、不迁移历史运行记录时：

1. 从实际旧部署恢复 `configs/local/`、`configs/secrets/` 中仍有效的配置、凭据和证书。
2. 按环境文件和服务 unit 的实际引用，逐个保留必要身份密钥及 Worker 配置。
   这些文件可能仍位于旧 `data/`，不要因此批量复制整个目录，也不要遗漏它们。
   迁到规范位置时同时更新消费方引用，保留密钥原内容。
3. 明确是否重新建立账号、重新 enrollment 和导入账号 AOA 配置；旧账号 ID 相关的
   私密集成配置不能自动认领给新账号。重新建立认证库会使旧会话和服务 Token 失效。
4. 核对主机、账号、套件路径、证书 SAN、Controller URL 和文件权限后，
   在操作者终端运行 `./restart_services.sh`。Agent 部署按
   [安装文档](../agent/installation.md)的 doctor / system-selfcheck 路径验收。

当前程序直接读取规范路径，不要求创建 `configs/certs` 或 `configs/config.json` 软链接。

## 旧布局兼容与迁移

当前代码保留以下“规范路径缺失才读取旧位置”的兼容行为；没有按版本自动停用，
`config_paths._prefer_existing` 也没有为每次命中输出告警：

| 内容 | 兼容旧位置 |
| --- | --- |
| 静态配置、集群、构建服务器、自动化方案 | `configs/` 根下的同名 JSON |
| 非敏感进程环境 | `configs/runtime.json` |
| Worker Token | `configs/worker_tokens.json` |
| 证书 | `configs/certs/` |
| 运行时覆盖 | `configs/config_runtime.json`，规范位置为 data 根下 `settings/preferences.json` |
| 按用户集成配置 | data 根下 `<feature>/by_user/<owner>/config_runtime.json` |

迁移工具在 Controller 和本机 Worker 停止后运行；预览与应用：

```bash
python scripts/migrate_config_layout.py
python scripts/migrate_config_layout.py --apply
```

使用标准 systemd 服务的本机，可在操作者终端执行：

```bash
sudo bash scripts/migrate_running_config.sh
```

维护脚本检查未完成任务、停止服务、以服务用户迁移，结束后恢复原来运行的服务。
迁移会拒绝覆盖已有目标、备份原文件、校验 JSON 和收紧权限；证书移入规范目录，
不创建兼容软链接。`restart_services.sh` 与维护脚本会备份并修正已安装 unit 的旧证书参数。
工具当前要求被迁移部署的数据根为项目内 `data/`；它不是跨项目历史数据复制工具。

静态配置与环境中凭据不同时，迁移保留各自值及消费关系，静态引用使用
`_STATIC_CONFIG` 后缀变量；迁移不等于轮换凭据。旧全局 Redmine 用户映射只归档，
确认归属后才能导入对应账号。回退前停止服务：

```bash
python scripts/migrate_config_layout.py --apply --rollback /absolute/path/to/data/config-migration-backups/layout-v2-...
```

回退恢复原文件，并私密备份新布局中的后续修改。兼容层退出需另行实施和验收，
不要把未实现的版本计划写成当前行为。

## 外部服务与结果下载

`external_services.gms_assistant_url` 配置助手服务；
`POST /api/config/external-services` 将变更写入运行时配置。SSH、套件、Redmine、
Gerrit、AI Provider 等使用相应配置段，真实凭据不写入示例。

### 远端测试结果目录下载

目录 ZIP 由经审核的 SSH 执行边界在测试主机生成，压缩过程运行在线程池，
不阻塞 Web 事件循环。以下环境变量控制远端打包，均须为正整数：

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `GMS_SUITE_ZIP_TIMEOUT_SECONDS` | 600 | SSH 总超时，最低 60 秒；远端进程提前 5 秒触发清理 |
| `GMS_SUITE_ZIP_MAX_BYTES` | 8589934592（8 GiB） | 源文件总量与 ZIP 输出各自的字节上限 |
| `GMS_SUITE_ZIP_MAX_FILES` | 100000 | ZIP 文件数量上限，扫描与写入时均检查 |
| `GMS_SUITE_ZIP_MIN_FREE_BYTES` | 268435456（256 MiB） | 远端临时磁盘至少保留的空闲空间 |
| `GMS_SUITE_ZIP_STALE_SECONDS` | 86400（24 小时） | 遗留临时文件的回收年龄 |

每次下载使用独立的 `gms-suite-downloads-<uid>/<operation>/` 临时目录。
SFTP 初始化失败、打开失败、流读取异常和客户端断开都会关闭资源、尝试删除
ZIP 并归还 SSH。超时通过同一 SSH 边界取消该次打包；Linux pidfd 将信号绑定到
原进程，避免 PID 重用。旧内核通过取消标记和远端定时器终止打包。

SIGKILL 或主机中断留下的目录会在后续目录下载时按年龄回收，回收会跳过仍
持有打包锁的进程，同时处理旧 `suite_dl_*.zip` 文件。文件/容量超限返回
`INVALID_SEMANTICS`（422），磁盘不足返回 `DEPENDENCY_UNAVAILABLE`（503），
超时返回 `DEPENDENCY_TIMEOUT`（504），SSH/SFTP 失败返回 `UPSTREAM_FAILURE`（502）。

## 发布与备份

打包排除本机配置、密钥和运行数据，再从模板生成脱敏默认值；升级复制不覆盖本机目录。
完整加密备份包含配置、身份密钥和运行状态，恢复操作见[生产部署](production.md)。
只恢复配置与完整备份恢复有不同的状态保留范围，不应混用。

`.gitignore` 不清除已经跟踪的文件或 Git 历史；历史凭据泄露需在所属系统撤销或轮换。
提交配置变更时只纳入已审核模板和说明。
