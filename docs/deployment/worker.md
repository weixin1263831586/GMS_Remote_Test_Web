# Worker 部署

本文说明 Controller 侧的 Agent / Worker 部署入口：Agent enrollment、集群 Worker 安装，以及 Worker Token 以 0600 文件传递的安全要求。

## Agent 一键安装与 Enrollment

Controller 暴露安装入口：

```text
/api/agent/install.sh
```

先由已登录用户在 Web 端生成一次性 Enrollment Code（配对码），再在 Agent 主机执行安装。生产环境推荐显式信任 Controller CA，例如：

```bash
export GMS_INSTALL_CA_CERT=/path/to/controller-ca.crt
curl -fsSL --cacert "$GMS_INSTALL_CA_CERT" \
  https://CONTROLLER:5001/api/agent/install.sh | bash -s -- <ENROLLMENT_CODE>
```

受控实验环境若使用自签名证书，可以按部署策略使用 installer 支持的 insecure bootstrap；不要在公网或不可信网络中关闭 TLS 校验。

安装完成后，Agent 通过 `GMS_AUTH_TOKEN_FILE` 指向权限为 `0600` 的 Service Token 文件（默认位于 `~/.local/state/gms-remote-test/<profile>.token`）。Agent 不需要、也不应接收平台用户密码。

常用维护入口：

```bash
gms-agent update
gms-agent rollback
gms-rt-system-selfcheck --json
```

## 集群 Worker 安装

远端 Worker 节点由 `scripts/install_cluster_worker.sh` 安装，可通过集群页面部署，也可以手工执行。脚本用法：

```bash
bash scripts/install_cluster_worker.sh \
  WORKER_ID CONTROLLER_URL TOKEN CONTROLLER_CERT [SUITE_ROOT] [WORKER_ADDRESS] [GTS_CREDENTIAL_FILE]
```

- `WORKER_ID`：Worker 唯一标识。
- `CONTROLLER_URL`：Controller HTTPS 地址。
- `TOKEN`：Worker Token。推荐传入 0600 Token 文件的路径而不是 Token 字符串本身（脚本会对小于 4096 字节的可读文件按路径读取），避免 Token 出现在远端进程 argv 中被同机用户通过 `ps` 看到。
- `CONTROLLER_CERT`：Controller CA 证书路径；传 `-` 表示不部署 CA 证书。
- `SUITE_ROOT`：GMS Suite 根目录，默认 `~/GMS-Suite`。
- `WORKER_ADDRESS`：Worker 地址，同时决定 noVNC 监听地址；`0.0.0.0` / `::` 会被拒绝，只允许私有、回环或 CGNAT 地址。
- `GTS_CREDENTIAL_FILE`：GTS service account 凭证文件（必填），安装时以 0600 权限安装到 `~/Software/gts-rockchip.json`。

脚本执行内容：

1. 安装 Host Tools 到 `~/Software`（`jdk-11`、`platform-tools`、`env.sh` / `verify.sh`），合并 JDK 模块分片，解包 scrcpy。
2. 配置 `~/.bashrc` 的 Host Tools 环境块，安装 adbproxy。
3. 安装桌面依赖（`x11vnc`、`xvfb`、`novnc`、`websockify`）与 usbip 工具，配置 `/usr/local/libexec/gms-worker-usbip` 与对应 sudoers 规则。
4. 部署 `worker_agent/`、`foundation/` 与原生工具到 `~/gms-worker-agent`，安装测试启动脚本到 Suite 根目录。
5. 写入 `~/.config/gms-worker/config.json`、`~/.config/gms-worker/token`（0600）与 `novnc-targets`。
6. 生成用户级 systemd 服务：`gms-worker-agent`，以及（检测到 noVNC web 根目录时）`gms-worker-xvfb`、`gms-worker-x11vnc`、`gms-worker-novnc`。
7. `systemctl --user daemon-reload`、`sudo loginctl enable-linger`（保证注销后继续运行），最后启动并显示 `gms-worker-agent` 状态。

### 通过集群页面部署

Controller 的集群部署流程（`features/cluster/deployment_api.py`）会通过 SSH 完成整个安装：

- 打包 `worker_agent`、`foundation`、安装脚本与 Host Tools 上传到远端 `/tmp/gms-worker-setup.tar.gz`。
- Worker Token 以 0600 权限上传为 `/tmp/gms-worker-token-<worker_id>`，并以**文件路径**传给安装脚本——Token 字符串不能出现在远端命令 argv 中。
- GTS 凭证同样以 0600 文件上传为 `/tmp/gms-worker-gts-<worker_id>.json`。
- 安装结束后清理远端临时文件；若部署失败会回滚已持久化的 Worker Token。
- 需要远程 sudo 时，sudo 密码经 stdin 传入，不写入命令行。

前提：Controller 侧需先构建 adbproxy 与原生工具分发包（`scripts/build_adbproxy_rs.sh`、`scripts/build_gms_worker_native.sh`），否则部署命令会提前报错退出。

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

也支持环境变量覆盖：

```bash
export GMS_WORKER_ID=ats-worker-01
export GMS_CONTROLLER_URL=https://controller.example.com:5001
export GMS_WORKER_TOKEN='...'
export GMS_WORKER_ADDRESS=192.0.2.20
export GMS_WORKER_SSH_USER=operator
export GMS_CONTROLLER_CA=/path/to/controller-ca.crt
```

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
