# 常见部署问题

本文汇总部署与运行阶段的常见故障及其排查步骤。

## 1. Production 启动时报安全配置错误

这是预期的 fail-closed 行为。不要通过改源码绕过检查，应补齐提示中缺少的：

```text
Authentication
Secure Cookie
Bootstrap Token
Metrics Token
Automation Webhook Token
Worker Token
Trusted Hosts
Allowed Origins
Secret / Audit Key
Agent Signing Key
```

对应配置项见[配置体系](configuration.md)。

## 2. 一键安装失败：系统依赖安装报错

`install.sh` 在 `apt-get update` 或依赖安装失败时会提示 DNS / apt 源问题。先修复目标机器外网 DNS 或 apt 源后重试，例如：

```bash
resolvectl status
ping cn.archive.ubuntu.com
```

未检测到 `apt-get` 时脚本会跳过系统依赖安装，需自行确认 `python3` / `venv` / `rsync` / `curl` / `ssh` 已安装。

## 3. 安装后 HTTPS 打不开或证书告警

- 安装器默认生成叶子自签证书，应经可信渠道分发 `gms-local.crt`；已有 CA 签发部署则分发对应 CA。集群页面 Worker 打包默认要求 `gms-local-ca.crt`，与单纯 Web HTTPS 可访问的条件不同，见 [Worker 部署](worker.md)。
- 证书缺失时 `restart_services.sh` 会自动重新生成。
- 确认端口与监听：

```bash
sudo systemctl status gms-web-app
lsof -i :5001
```

## 4. restart_services.sh 健康检查失败

脚本轮询 `https://localhost:<端口>/api/system/health/live` 最多 60 秒。失败时按启动方式查看日志：

```bash
sudo journalctl -u gms-web-app -n 30   # systemd 模式
tail -30 fastapi.log                    # nohup 模式
```

清理 Python 缓存后的冷启动可能超过 20 秒，属正常现象；端口被残留进程占用时脚本会自动清理后重试。

若日志显示 `load_cert_chain` 抛出 `FileNotFoundError`，检查已安装服务的
证书参数是否与 `configs/secrets/certs/` 一致。重新克隆仅恢复代码，原部署的
本机配置和身份密钥需要按[配置恢复范围](configuration.md#重新克隆后仅恢复必要配置)从实际旧部署恢复；必要文件可能仍被引用于旧数据根，不能只按目录名筛选。执行最新的
`./restart_services.sh` 会在停止服务前修正旧证书路径、备份原服务文件并重新加载
systemd。路径修正失败时脚本会直接报错，需要在操作者终端提供 sudo 权限。

## 5. Worker 注册失败

确认：

```text
worker_id
controller_url
worker token
Controller HTTPS CA
```

并检查：

```bash
curl -vk https://controller.example.com:5001/api/system/health
```

生产 Worker 不允许使用 HTTP Controller URL。

## 6. Worker Token 文件报权限错误

```bash
chmod 600 /path/to/worker.token
```

Token 文件权限不是 0600 时 Worker 会拒绝启动。

## 7. Agent 安装后无法访问 Controller

优先检查：

```text
GMS_REMOTE_TEST_SERVER
GMS_AUTH_TOKEN_FILE
Service Token 文件权限
Controller CA / GMS_CURL_CA_CERT
DNS / VPN / Tailscale 路由
```

执行：

```bash
gms-rt-system-selfcheck --json
```

不要通过长期设置 insecure TLS 来掩盖 CA、证书 SAN 或网络配置问题。

## 8. noVNC 页面打开但黑屏或无法连接

检查：

```bash
systemctl --user status gms-worker-xvfb.service
systemctl --user status gms-worker-x11vnc.service
systemctl --user status gms-worker-novnc.service
ss -ltn | grep -E ':5900|:6080'
```

仅端口监听并不代表 VNC 正常；Worker 还会进行 RFB Protocol Handshake 检测。注意 noVNC 监听地址只允许私有、回环或 CGNAT 地址，`0.0.0.0` / `::` 会被安装脚本拒绝。

## 9. USB/IP attach 后 `adb devices` 没有设备

依次检查：

```bash
usbip port
lsusb
adb kill-server
adb start-server
adb devices -l
```

同时检查：

- Windows 来源机 `usbipd list`
- Windows ADB 是否仍占用设备
- Linux `vhci_hcd` 是否加载
- TCP 3240 是否可达
- 设备端是否出现 ADB authorization
- Android 是否处于 Recovery / Fastboot 等非普通 ADB 状态

## 10. `ADB server version doesn't match this client`

确保执行测试的 Worker 上不要混用多个不兼容版本的 `adb`：

```bash
which -a adb
adb version
ps -ef | grep '[a]db'
```

CTS / VTS 套件自带 platform-tools 时尤其需要确认实际 PATH 与 ADB Server 来源。

## 11. 设备串口显示权限不足或无法打开

设备串口功能只访问 Controller 本机的 `/dev/ttyUSB*` 和 `/dev/ttyACM*`。确认运行 Web 服务的账号属于 `dialout` 组：

```bash
id
ls -l /dev/ttyUSB0
sudo usermod -aG dialout SERVICE_USER
```

加入组后需要重新登录或重启 Web 服务。若页面提示串口被占用，请先退出 `picocom`、`minicom` 等独占该端口的程序。

## 相关文档

- [快速安装](quick-install.md)
- [生产部署](production.md)
- [配置体系](configuration.md)
- [Worker 部署](worker.md)
