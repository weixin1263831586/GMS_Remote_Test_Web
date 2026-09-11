# 配置体系

本文说明本项目的配置目录布局、加载与回退顺序、模板与运行时配置的关系，以及哪些配置属于本机数据、不得入库或进入发布包。

## 配置目录与 Git 跟踪策略

配置分为「可提交的模板」与「本机部署数据」两类。仓库根 `.gitignore` 的策略是：整个 `configs/` 目录默认忽略，只放行经过审核的模板与说明文档。

```text
/configs/*
!/configs/examples/
/configs/examples/*
!/configs/examples/config.example.json
!/configs/examples/runtime.example.json
!/configs/examples/build_servers.example.json
!/configs/examples/automation_profiles.example.json
!/configs/examples/cluster.example.json
!/configs/README.md
!/configs/AGENTS.md
```

因此 `configs/config.json`、`configs/local/*`、`configs/secrets/*` 等真实配置都是本机数据，不进入版本库。完整布局见 `configs/README.md`。

## 模板与回退链

### 静态主配置

- 真实路径：`configs/local/config.json`（兼容旧布局 `configs/config.json` 读取）。
- 模板：`configs/examples/config.example.json`。
- 当真实 `configs/local/config.json` 不存在时，`foundation/config.py` 的 `ConfigManager` 回退到示例模板，保证全新 checkout 或 CI 可直接启动。

本地部署时复制模板：

```bash
cp -n configs/examples/config.example.json configs/local/config.json
```

然后只在本机填写真实配置。

### 运行时配置覆盖

运行数据保存在 `data/settings/preferences.json`（兼容旧路径 `configs/config_runtime.json`），由 `RuntimeConfigStore` 管理。`ConfigManager._load_and_merge_config` 先用运行时配置覆盖静态默认值，但保留静态 `ai_models` 配置。

### 运行环境与秘密

- 非敏感环境变量：`configs/local/environment.json`（兼容旧路径 `configs/runtime.json`）。
- 密码与 Token：`configs/secrets/environment.json`。
- `bootstrap.env_loader` 会在应用模块导入前加载运行时环境变量，但真实系统环境变量优先级更高。

环境变量包含：

```text
GMS_ENV
GMS_AUTH_REQUIRED
GMS_SECURE_COOKIES
GMS_SECRET_KEY_FILE
GMS_AUDIT_HMAC_KEY_FILE
GMS_METRICS_TOKEN
GMS_AUTOMATION_WEBHOOK_TOKEN
GMS_BOOTSTRAP_TOKEN
GMS_SKILL_SIGNING_KEY_FILE
```

运行时配置文件默认声明 `GMS_ENV=production`，开发测试不要原样复制成真实配置。

### 其它结构化配置路径

| 配置 | 真实路径（兼容旧路径） | 模板 |
| --- | --- | --- |
| 集群 | `configs/local/cluster.json`（`configs/cluster.json`） | `cluster.example.json` |
| Worker Token | `configs/secrets/worker_tokens.json`（`configs/worker_tokens.json`） | 无（安装器写入） |
| 构建服务器 | `configs/local/build_servers.json`（`configs/build_servers.json`） | `build_servers.example.json` |
| 自动化方案 | `configs/local/automation_profiles.json`（`configs/automation_profiles.json`） | `automation_profiles.example.json` |
| 证书 | `configs/secrets/certs/`（`configs/certs/`） | 无（安装器生成自签证书） |

### 占位符展开

静态配置支持占位符展开，推荐用环境变量占位符承载 Secret，不要把真实密码直接写进可提交配置：

```text
${ENV_NAME:}
```

`${PROJECT_ROOT}` 会展开为当前部署树根目录，配置文件随部署目录迁移时无需手工改绝对路径；`${UBUNTU_USER}`、`${UBUNTU_HOST}` 在环境变量缺失时也有兜底行为。

## 关键配置键

`external_services` 段保存外部服务地址。例如 AI 助手代理地址：

```text
external_services.gms_assistant_url
```

未配置时助手页面会提示设置该项。该键可通过 Web 接口 `POST /api/config/external-services` 修改并持久化到运行时配置。

其它常用配置段包括 Ubuntu 测试主机、Firmware Share、SSH、USB/IP VID:PID、Wi-Fi、VNC、VPN、GMS Suite 路径、测试脚本、GSI 脚本、scrcpy、OpenGrok、Redmine、Gerrit、AI Provider、Sidebar 等。

## 秘密管理

- 密码、API Key、Webhook / Bootstrap Token 等保存在 `configs/secrets/environment.json`，与 `configs/local/environment.json` 分离。
- 敏感文件建议权限：

```bash
chmod 600 configs/local/environment.json
chmod 600 configs/secrets/environment.json
chmod 600 configs/secrets/worker_tokens.json
```

- 秘密不进入发布包：`install.sh` 打包与安装时的 rsync 排除 `configs/local/`、`configs/secrets/`、`configs/runtime.json`、`configs/worker_tokens.json` 等运行时文件。
- `scripts/sanitize_release_config.py` 在打包或全新 checkout 场景下从 `examples/` 模板生成目标配置，并清空敏感键（`api_key`、`password`、`secret`、`token`、`ubuntu_pswd`、`vnc_password` 及对应后缀），同时清除 `ubuntu_user`、`ubuntu_host`、`private_key_path`、`client_hosts` 等部署身份，避免发布包暴露打包机的用户与主机。

## 相关文档

- [快速安装](quick-install.md)
- [生产部署](production.md)
- [Worker 部署](worker.md)
- `configs/README.md`：配置目录完整布局与迁移方式
