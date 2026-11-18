# vpn-proxy 管理弹框（GMS 常用工具）

入口：**常用工具 → vpn-proxy**。这是对现有 [vpn-proxy](https://github.com/weixin1263831586/vpn-proxy) 的管理接入，不是跳转链接。

## 架构与数据来源

- `GET /api/tools/vpn-proxy/status`：本机 systemd 的 `is-active` 检查 + 已部署 `/etc/rk-vpn-proxy/config.json` + **仅回环** `127.0.0.1:<stats_port>/api/stats` 客户端统计。统计接口来自上游 `client-stats.sh web` 对应的 `scripts/client_stats.py --serve`，安装服务通常为 `rk-vpn-proxy-stats.service`。
- 客户端的“当前接入”来自代理端口 TCP ESTABLISHED 连接；“最近活跃”来自 Squid 日志最近 300 秒请求。两者不同；如果 `ss` 或日志不可读，UI 显示数据源不可用，**不能把失败当成无客户端**。
- `POST /api/tools/vpn-proxy/control`：需要管理员二次验证，只能 `start` / `stop`。不接受任意命令、服务名、Host/URL。启动先运行防火墙服务再运行代理；停止仅停止代理，**不停止 kill-switch**。
- 安装命令从已部署配置的 `advertise_host`、`installer_port`、`installer_enabled` 生成。前端只复制命令，不会直接在其他客户端执行。执行前必须把目标客户端 IP 纳入 `allowed_clients` 并重部署；内网 HTTP 脚本执行仅适用于可信网络。

## 主机部署前提

Controller 必须与 VPN 代理运行在**同一主机网络命名空间**，使 `127.0.0.1:17898` 指向 vpn-proxy 的统计服务；若 GMS Controller 在容器/另一台服务器，则状态会正确提示统计不可用，不会从浏览器直连统计端口。

建议 GMS Controller 使用专用的、非 root 系统账号，并用 `visudo` 为**该账号**配置最小可执行权限。假设 `/usr/bin/systemctl` 是实际路径（先用 `command -v systemctl` 核实），仅允许以下三个精确命令（将 `GMS_SERVICE_USER` 替换成服务账号）：

```sudoers
GMS_SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start rk-vpn-proxy-firewall.service, /usr/bin/systemctl start rk-vpn-proxy.service, /usr/bin/systemctl stop rk-vpn-proxy.service
```

使用 `sudo visudo -f /etc/sudoers.d/gms-vpn-proxy` 添加并检查权限，**不要**允许 `systemctl *`、整个 `sudo` 或任意脚本执行。没有 sudoers 时按钮会明确报错，不会静默成功。

系统服务必须已由上游 `sudo bash install.sh` 完整部署。本适配器不会安装 Squid、修改 VPN 路由、开放端口或更改白名单。

客户端统计使用项目已有的 `client-stats.sh json` / Web `/api/stats` 设计；本版优先复用只读 Web API，避免在 GMS HTTP 请求内执行 root Python 脚本。若需远程主机管理，后续应通过 Worker 安全能力或专门认证桥接服务，不能让 GMS 后端直连未经认证的任意 URL。
