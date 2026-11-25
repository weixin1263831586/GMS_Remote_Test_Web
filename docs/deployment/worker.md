# Worker 部署

本文说明集群 Worker 的安装入口、配置与认证。Worker 通过带 Token 的 HTTPS 注册、心跳、轮询命令和回报结果。

Agent Runtime 是 Codex / Kimi / kkagent 的 Controller 客户端；安装、配对、
profile 和验收统一见 [Agent 安装](../agent/installation.md)。下面的 Worker
运行在连接设备、执行 Tradefed 的主机，使用独立 Worker Token。

## 集群 Worker 安装

远端 Worker 节点由 `scripts/deployment/worker/install_cluster_worker.sh` 安装，可通过集群页面部署，也可以手工执行。脚本用法：

```bash
bash scripts/deployment/worker/install_cluster_worker.sh \
  WORKER_ID CONTROLLER_URL TOKEN CONTROLLER_CERT [SUITE_ROOT] [WORKER_ADDRESS] [GTS_CREDENTIAL_FILE]
```

- `WORKER_ID`：Worker 唯一标识。
- `CONTROLLER_URL`：Controller HTTPS 地址。
- `TOKEN`：Worker Token。推荐传入 0600 Token 文件的路径而不是 Token 字符串本身（脚本会对小于 4096 字节的可读文件按路径读取），避免 Token 出现在远端进程 argv 中被同机用户通过 `ps` 看到。
- `CONTROLLER_CERT`：Controller CA 证书路径；传 `-` 表示不部署 CA 文件，需要目标环境的系统信任库已信任 Controller。控制器打包部署 Bundle 时必须存在 CA 证书 `gms-local-ca.crt`（缺失即报错，不会回退到叶子证书 `gms-local.crt`——叶子证书不是 CA 信任锚）。
- `SUITE_ROOT`：GMS Suite 根目录，默认 `~/GMS-Suite`。
- `WORKER_ADDRESS`：Worker 地址，同时决定 noVNC 监听地址；`0.0.0.0` / `::` 会被拒绝，只允许私有、回环或 CGNAT 地址。
- `GTS_CREDENTIAL_FILE`：GTS service account 凭证文件（必填），安装时以 0600 权限安装到 `~/Software/gts-rockchip.json`。

脚本执行内容：

1. 安装 Host Tools 到 `~/Software`（`jdk-11`、`platform-tools`、`env.sh` / `verify.sh`），按固定 SHA-256 获取部署工件并解包 scrcpy。
2. 配置 `~/.bashrc` 的 Host Tools 环境块，安装 adbproxy。
3. 安装桌面依赖（`x11vnc`、`xvfb`、`novnc`、`websockify`）与 usbip 工具，配置 `/usr/local/libexec/gms-worker-usbip` 与对应 sudoers 规则。
4. 部署 `worker/`、`foundation/` 与原生工具到 `~/gms-worker-agent`，安装测试启动脚本到 Suite 根目录。
5. 写入 `~/.config/gms-worker/config.json`、`~/.config/gms-worker/token`（0600）与 `novnc-targets`。
6. 生成用户级 systemd 服务：`gms-worker-agent`，以及（检测到 noVNC web 根目录时）`gms-worker-xvfb`、`gms-worker-x11vnc`、`gms-worker-novnc`。
7. `systemctl --user daemon-reload`、`sudo loginctl enable-linger`（保证注销后继续运行），最后启动并显示 `gms-worker-agent` 状态。

### 通过集群页面部署

Controller 的集群部署流程（`features/cluster/deployment/api.py`）会通过 SSH 完成整个安装：

- 打包 `worker`、`foundation`、安装脚本与 Host Tools，连同 Worker Token、GTS 凭证一起上传到随机化的 `/tmp` 暂存名（`_staged_credential_path`）。Token 与 GTS 文件权限为 0600，并以**文件路径**传给安装脚本——内容不能出现在远端命令 argv 中。
- 安装结束后按这些路径清理远端临时文件；若部署失败会回滚已持久化的 Worker Token。
- 需要远程 sudo 时，sudo 密码经 stdin 传入，不写入命令行。

前提：Controller 侧需先构建 adbproxy 与原生工具分发包（`scripts/native/build_adbproxy_rs.sh`、`scripts/native/build_gms_worker_native.sh`），否则部署命令会提前报错退出。

## Worker 配置与 Token

默认配置路径 `~/.config/gms-worker/config.json`，也可用 `GMS_WORKER_CONFIG` 指定。最小结构：

```json
{
  "worker_id": "ats-worker-01",
  "name": "ATS Worker 01",
  "controller_url": "https://controller.example.com:5001",
  "worker_token_file": "/home/operator/.config/gms-worker/token",
  "heartbeat_interval_seconds": 15,
  "suite_scan_interval_seconds": 300,
  "max_jobs": 2,
  "suite_roots": [
    "/home/operator/GMS-Suite",
    "/opt/GMS-Suite"
  ],
  "data_root": "/home/operator/gms-worker-data"
}
```

Worker Token 文件通过上方 JSON 的 `worker_token_file` 指定；
`GMS_WORKER_TOKEN` 可覆盖 Token 值，但部署推荐使用文件。其他环境覆盖示例：

```bash
export GMS_WORKER_ID=ats-worker-01
export GMS_CONTROLLER_URL=https://controller.example.com:5001
export GMS_WORKER_ADDRESS=192.0.2.20
export GMS_WORKER_SSH_USER=operator
export GMS_CONTROLLER_CA=/path/to/controller-ca.crt   # 使用经可信渠道确认的 Controller CA；叶子自签部署信任其自签证书
```

集群页面的 CPU 百分比按相邻两次 Linux 采样之间的忙碌时间计算，首个心跳填 `0`。一分钟平均负载通过 `load_1m` 单独上报。磁盘容量和使用率取 `data_root` 所在的文件系统。

### Worker Token 的 0600 要求

Token 从 `GMS_WORKER_TOKEN` 或 `worker_token_file` 读取。使用 Token 文件时权限必须为 `0600`，否则 Worker 会拒绝启动：

```bash
chmod 600 /path/to/token
```

这是全链路约定：Controller 侧 `configs/secrets/worker_tokens.json`、集群部署上传的临时 Token 文件、`install.sh` 生成的本地 Worker Token（`data/secrets/local-worker.token`）均以 0600 保存。

### 生产环境要求

`GMS_ENV=production` 时 Worker 强制要求 HTTPS Controller URL。

只承担设备来源或传输角色、不执行 CTS / GTS / VTS / STS 的节点可配置：

```json
{
  "source_only": true
}
```

## 相关文档

- [快速安装](quick-install.md)
- [生产部署](production.md)
- [配置体系](configuration.md)
- [常见部署问题](troubleshooting.md)

### Worker 服务启动

Worker 源码包位于 `worker/`，统一使用 `python -m worker.app` 启动。独立 Worker 使用对应安装脚本部署并重新启动；源码生产安装使用 `install.sh` 生成本地 Worker 的 systemd 服务配置。

### 手动与自动部署统一入口

复制集群页面的单行命令，在 Controller 安装根目录执行 `bash scripts/deployment/worker/deploy_worker.sh WORKER_ID USER@HOST https://CONTROLLER '~/GMS-Suite' 1`。SSH 主机指纹按 OpenSSH 正常校验；Worker Token 通过终端输入，GTS 凭证由 `GMS_GTS_CREDENTIAL_FILE` 指定。两种部署均使用同一个后端离线包构建器，并携带 Controller CA。源码环境分发 Worker 源码包；编译发布环境只分发经清单校验的原生运行时及部署资源，不分发后端 Python 源码。编译版 Worker 要求 Linux x86_64 / Ubuntu 22.04 兼容环境。
