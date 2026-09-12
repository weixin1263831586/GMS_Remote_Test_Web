# 配置与运行数据

仅 `examples/` 中明确列入 `.gitignore` 白名单的模板和本说明文档可以提交。
部署配置、凭证、证书、用户数据和迁移备份均不得提交；密文也属于凭证。

```text
configs/
├── examples/                       # 可以提交：通用模板，无实际凭证
│   ├── config.example.json
│   ├── runtime.example.json
│   ├── cluster.example.json
│   ├── build_servers.example.json
│   └── automation_profiles.example.json
├── local/                          # 忽略：本机部署配置，JSON 权限 0600
│   ├── config.json                 # 应用集成和静态默认值，密码引用环境变量
│   ├── environment.json            # 非敏感进程环境变量、密钥文件路径
│   ├── deployment.json             # 测试主机、安装身份、路径、静态路由
│   ├── cluster.json                # 集群开关、容量和 Controller 地址
│   ├── build_servers.json          # 构建服务器连接和命令模板
│   ├── automation_profiles.json    # 本机自动化方案
│   └── layout.json                 # 迁移版本标记
└── secrets/                        # 忽略：目录 0700，JSON/私钥 0600
    ├── environment.json            # 密码、API Key、Webhook/Bootstrap Token
    ├── runtime_credentials.json    # 加密 SSH、Redmine 等凭证
    ├── worker_tokens.json          # 按 Worker ID 保存的 Token
    └── certs/                      # 部署证书与私钥

data/                               # 全部忽略
├── settings/preferences.json        # Web 用户设置及未分类的兼容配置
├── settings/user_tools.json         # 客户端工具配置
├── devices/runtime.json             # USB/IP 分配、来源、网络质量历史
├── redmine/legacy/redmine_user_map.json  # 旧全局映射；不自动分配给某个用户
└── config-migration-backups/         # 原配置及回退时保留的新配置，含敏感数据
```

已有 `data/secrets/` 下的主密钥、签名密钥及 Worker 私有数据保持原引用，
避免改变加密身份。不要把这些文件复制进示例。

## 初始化与加载

生产安装器会准备本机配置。开发者可以按需复制模板：

```bash
mkdir -p configs/local
cp -n configs/examples/config.example.json configs/local/config.json
cp -n configs/examples/cluster.example.json configs/local/cluster.json
cp -n configs/examples/build_servers.example.json configs/local/build_servers.json
cp -n configs/examples/automation_profiles.example.json configs/local/automation_profiles.json
```

`runtime.example.json` 描述生产环境变量。实际非敏感变量放入
`local/environment.json`，密码和 Token 放入 `secrets/environment.json`。
开发环境不要直接启用模板的 production 设置。

环境变量优先级：进程已有环境 > `secrets/environment.json` >
`local/environment.json`。`${PROJECT_ROOT}` 按部署树根目录展开；修改环境
文件后需要重启进程。应用静态配置支持 `${ENV_NAME:默认值}` 引用。

应用先读取 `local/config.json`，不存在时回退到示例；部署字段参与路径
占位符展开，随后应用运行时覆盖。静态 `ai_models` 的既有优先级保持不变。
运行时配置对调用者仍呈现同一个 JSON 对象，存储层负责分类、合并、加锁、
私密写入及失败恢复。嵌套凭证从普通配置中提取，含凭证的列表整体保存在私密文件中。
按用户隔离的 Redmine/Gerrit 文件保持原有隔离，不读取全局凭证。

`GMS_DATA_ROOT` 继续控制运行时设置与设备状态的存储根目录；配置与凭证
仍位于项目的 `configs/`。`GMS_CLUSTER_CONFIG` 和 `GMS_WORKER_TOKENS_FILE`
保留原有显式路径覆盖能力。构建模板的 `{参数}` 与应用的 `${环境变量}`
不是同一种语法，不应互换。

## 旧路径兼容期限（canonical + legacy 回退）

`foundation/config_paths.py` 中的「canonical 缺失 → 回退 legacy」逻辑是
**迁移期兼容层**，不是永久架构。为避免新目录结构退化为「新代码永久维护
所有旧目录」，兼容层按以下节奏收敛：

- **0.19**：canonical + legacy 双读（当前状态）；旧路径命中时输出
  warning 日志（含迁移提示），便于统计仍有旧布局的部署；
- **0.20**：canonical only——legacy 回退删除，旧路径存在时启动告警
  并指向 `scripts/migrate_config_layout.py`。

新增代码禁止引入新的 canonical→legacy 回退；需要兼容旧路径时必须在本表
登记期限。

## 旧布局迁移与回退

旧版根目录文件仍可读取。迁移必须在 Controller 和本机 Worker 停止后执行；
工具检查 Controller 锁，不会在线拆分它正在写入的配置。

```bash
python scripts/migrate_config_layout.py
python scripts/migrate_config_layout.py --apply
```

本机使用标准 systemd 服务时，可在自己的终端执行以下维护命令；它会检查
未完成任务，停止服务，以服务用户身份迁移，并在成功或失败后恢复原先运行的服务：

```bash
sudo bash scripts/migrate_running_config.sh
```

第一次命令只显示目标路径。应用迁移时会拒绝覆盖已有目标、备份原文件、
校验新 JSON、收紧权限，然后移除旧位置的配置文件。证书目录保留相对兼容链接
`configs/certs -> secrets/certs`，用于现有 systemd/Worker 的 TLS 参数。
`configs/config.json -> local/deployment.json` 仅用于旧版已安装 CLI 的主机、
用户名和端口发现；应用本身读取 `local/config.json`，该链接不包含静态密码。
重复执行不会重新导入旧数据。工具当前要求待迁移部署的数据根为项目内 `data/`。

静态配置与环境中的密钥值不同时，迁移保留两份原值及各自消费关系：
静态配置引用带 `_STATIC_CONFIG` 后缀的私密环境变量。迁移不替用户猜测
哪个凭证应生效，也不等同于轮换凭证。后续确认有效凭证后可统一引用。

回退时停止服务，使用迁移输出中的备份目录：

```bash
python scripts/migrate_config_layout.py --apply --rollback /absolute/path/to/data/config-migration-backups/layout-v2-...
```

回退恢复原始文件，并把新布局当前值另存到私密备份目录，避免丢失迁移后的修改。
旧全局 Redmine 用户映射只归档；确认数据归属后才能导入对应用户的数据目录。

## 发布、备份与历史凭证

发布流程排除 `configs/local/`、`configs/secrets/`、旧部署文件及 `data/`，
再从模板生成脱敏的本机默认配置。升级复制不会覆盖目标机器的本机目录。
加密备份包含新布局的配置、凭证、证书及运行时数据；恢复会重建证书兼容链接。

`.gitignore` 不影响已经跟踪的文件，也不会清除历史。提交前应检查索引，
确保 `configs/` 中只包含说明和模板；对曾进入 Git 历史的凭证，需要在所属
系统轮换或撤销，再协调清理远端历史。不要因凭证已经加密或文件已删除而认为
历史泄露已解决。
