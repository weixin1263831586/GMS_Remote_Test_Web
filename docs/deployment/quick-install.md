# 快速安装

本文面向首次在本机拉起 Controller 的使用者，覆盖开发模式与 `install.sh` 一键安装两条路径，以及安装后必须确认的服务状态。

## 前置条件

Controller 与 Worker 推荐环境：

- Ubuntu / Debian 系 Linux
- Python 3.10+
- systemd
- OpenSSH Client / Server
- ADB / Fastboot

`install.sh` 在支持 `apt-get` 的系统上会自动安装主要依赖：

```text
python3
python3-venv
python3-pip
rsync
curl
lsof
psmisc
openssl
openssh-client
openssh-server
sudo
iproute2
x11vnc
novnc
websockify
libudev1
```

并尝试安装可选组件（失败不阻断安装）：

```text
usbip
adb
fastboot
android-tools-adb
android-tools-fastboot
default-jre
```

网络方面，项目可工作在公司 LAN、VPN 或 Tailscale 等受控组网环境。生产环境不要通过未审核的远程脚本安装网络软件；安装器只会启用已经存在的 Tailscale，不会执行 `curl | bash` 类安装。

## 方式一：开发模式运行

```bash
git clone https://github.com/weixin1263831586/GMS_Remote_Test_Web.git
cd GMS_Remote_Test_Web

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

启动：

```bash
GMS_ENV=development python app.py
```

默认服务端口由项目 Settings 决定，标准安装流程默认使用 `5001`。

项目没有真实 `configs/local/config.json` 时会回退到示例配置 `configs/examples/config.example.json` 作为结构参考，但涉及 SSH、设备、Redmine、Gerrit、AI 等能力前仍需准备本地部署配置，详见[配置体系](configuration.md)。

> 不建议为了开发测试直接把 `configs/examples/runtime.example.json` 原样复制成运行时环境文件。该模板默认声明 `GMS_ENV=production`，而 production 模式要求完整的认证、HTTPS 和安全密钥配置。

## 方式二：一键安装

```bash
./install.sh
```

默认值：

```text
安装目录: /opt/gms-remote-test/web_app
systemd 服务: gms-web-app
HTTPS 端口: 5001
```

也可以显式指定：

```bash
./install.sh \
  --install-dir /opt/gms-remote-test/web_app \
  --service-name gms-web-app \
  --port 5001 \
  --user "$USER"
```

可用参数与对应环境变量：

```text
--install-dir <path>   安装目录（GMS_INSTALL_DIR）
--service-name <name>  systemd 服务名前缀（GMS_SERVICE_NAME）
--port <port>          FastAPI 监听端口（GMS_PORT）
--user <user>          运行服务的本机用户（GMS_RUN_USER）
--host-ip <ip>         手动指定本机 IP（GMS_HOST_IP），默认自动检测
```

安装器会负责：

- 安装系统依赖
- 创建 Python venv
- 安装 Python requirements
- 创建运行目录
- 创建 HTTPS 证书
- 创建 Secret Key
- 创建 Audit HMAC Key
- 创建 Metrics Token
- 创建 Automation Webhook Token
- 创建 Bootstrap Token
- 创建 Skill Signing Key
- 创建本地 Worker Token
- 写入 `configs/local/environment.json`
- 写入本地 Worker 配置
- 设置私密文件权限
- 安装并管理 systemd 服务
- 配置 noVNC / Worker 所需运行环境

> `install.sh` 需要完整的项目目录（含 `app.py` 和 `requirements.txt`）。它不再提供在线直装式打包；跨机分发请使用 `package` 子命令生成签名安装包，详见[生产部署](production.md)。

## 安装后确认

安装器结束时会输出访问地址、初始化令牌的读取命令与日志入口：

```text
访问地址: https://<HOST_IP>:<PORT>
本机访问: https://localhost:<PORT>
查看日志: sudo journalctl -u gms-web-app -f
```

### 首次创建管理员

首次打开页面需要创建平台管理员账号，密码由你在页面中设置。
`install.sh` 会自动生成 `GMS_BOOTSTRAP_TOKEN`，保存到实际安装目录的
`configs/secrets/environment.json`，用于防止其他访问者抢先创建首个管理员。
安装输出只提供读取命令，不直接打印令牌。

在 Controller 服务器上，以安装时 `--user` 指定的服务运行用户进入安装目录
（默认如下；自定义部署请替换路径），执行：

```bash
cd /opt/gms-remote-test/web_app
python3 -c 'import json; print(json.load(open("configs/secrets/environment.json"))["GMS_BOOTSTRAP_TOKEN"])'
```

把输出粘贴到页面的“初始化令牌”，填写账号、密码和显示名后提交。
如遇文件权限不足，请使用安装结束时输出的 `sudo -u ...` 命令。
必须读取正在运行的 Controller 的安装目录，而不是打包或下载源码的目录。
创建成功后使用平台账号登录，无需再次输入初始化令牌。

新版安装没有 `configs/runtime.json` 是正常情况；令牌已与非敏感配置分开保存。
旧部署可能仍使用该文件。手动部署时需自行配置至少 32 字符的随机
`GMS_BOOTSTRAP_TOKEN`；Controller 的真实进程环境变量优先于配置文件。
修改文件中的令牌后需重启 Controller 才能生效。

### 服务检查

生产安装完成后应使用 HTTPS 访问 Controller。依次确认以下内容：

1. **Controller URL**：浏览器打开 `https://<HOST_IP>:5001`，本机可用 `https://localhost:5001`。
2. **健康检查**：访问健康端点确认服务存活。

```bash
curl -sk https://localhost:5001/api/system/health/live
curl -sk https://localhost:5001/api/system/health/ready
curl -sk https://localhost:5001/api/system/health
```

3. **TLS**：安装器在证书目录生成自签证书（`configs/secrets/certs` 下的 `gms-local.crt` 与 `gms-local.key`）。浏览器首次访问会出现不受信任警告；如需受控信任，请把 `gms-local.crt` 分发给客户端，不要在公网关闭 TLS 校验。
4. **本地 Worker**：安装器同时创建本地 Worker 服务 `gms-web-app-local-worker.service`，配置位于 `<安装目录>/data/local-worker/config.json`。确认它处于运行状态：

```bash
systemctl status gms-web-app-local-worker.service
```

5. **Agent installer URL**：Controller 暴露 `https://<HOST_IP>:5001/api/agent/install.sh`，供远端 Agent 一行安装与配对，详见[Worker 部署](worker.md)。

## 相关文档

- [生产部署](production.md)
- [配置体系](configuration.md)
- [Worker 部署](worker.md)
- [常见部署问题](troubleshooting.md)
