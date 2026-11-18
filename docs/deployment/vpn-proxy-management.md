# vpn-proxy 管理卡片部署与验证

> 适用范围：GMS Controller 运行的本机 Linux 上已安装的
> [weixin1263831586/vpn-proxy](https://github.com/weixin1263831586/vpn-proxy)。
> 这不是对任意 Worker 或远端 IP 的通用管理接口。

## 现有能力与数据边界

“常用工具 → vpn-proxy”打开 GMS 的管理弹框，而不是 GitHub 外链。
服务状态通过固定的 `systemctl is-active` 查询；当前 TCP 接入、
最近活跃客户端、请求数、流量与错误等数据直接读取 vpn-proxy 原有
`client-stats.sh` / `scripts/client_stats.py` 提供的
`http://127.0.0.1:<stats_port>/api/stats`。
GMS 不再另写 `ss` 或 Squid `access.log` 解析器。

客户端数据为快照，不等于持续在线列表：`connected` 表示采集时刻的已建立 TCP
连接；`recent` 表示最近一个窗口内有过请求的客户端。
`sources.connections` 与 `sources.access_log` 分别表示两个采集来源是否可用；
任一来源不可用时，该板块必须显示“数据不可用”，不能当作“0 个客户端”。
快照缺少来源状态、采集失败或时间过期时，GMS 拒绝显示为实时统计。

Linux 和 Windows 复制命令依据**已部署**的
`/etc/rk-vpn-proxy/config.json` 生成，不从 GitHub 示例配置猜测实际运行地址；
必须先确认 `allowed_clients` 白名单及安装器的服务状态。
安装脚本通过内网 HTTP 下载并执行，应仅用于可信链路，执行前评估来源及完整性；
弹框只复制命令，不在服务器或客户端自动运行。

## 1. 准备服务器

确认 GMS Controller 与已部署的 vpn-proxy 在**同一台 Linux 主机**。
若实际 vpn-proxy 部署在其他 Worker 上，当前适配器不可跨机操作，不应将远端
`stats_port` 直接对公网开放或改为任意用户输入 URL。

```bash
# 在运行 GMS Controller 的 Linux 主机上执行
sudo test -r /etc/rk-vpn-proxy/config.json
systemctl is-active rk-vpn-proxy.service
systemctl is-active rk-vpn-proxy-firewall.service
systemctl is-active rk-vpn-proxy-stats.service
systemctl is-active rk-vpn-proxy-installer.service

# 通过现有统计服务验证采集接口，不需要对外开放 stats_port
curl --noproxy '*' --fail --max-time 5 http://127.0.0.1:17898/api/stats
```

其中端口 `17898` 为默认 `stats_port`。若部署配置修改了端口，需同步替换。
代理本身还需要 VPN 接口与路由、nftables 保护策略满足 vpn-proxy 项目的要求。
不要因为代理服务处于 `active` 就直接判断出口已连通。

## 2. 授权受限的服务启停

GET 状态遵循 GMS 已有登录权限。POST 启停要求**管理员二次验证**，不允许
客户端自行传入 unit、命令或工作目录；后端只执行如下固定命令：

```text
systemctl start rk-vpn-proxy-firewall.service
systemctl start rk-vpn-proxy.service
systemctl stop rk-vpn-proxy.service
```

停止代理不停止防火墙保护。GMS Controller 服务通常不以 root 运行，
因此需要为**实际运行 GMS Controller 的系统账号**设置最小 sudoers 权限。
先查账号与 systemctl 实际路径，避免照抄部署账号：

```bash
systemctl show gms-web-app.service -p User -p MainPID
command -v systemctl
```

然后由管理员用 `sudo visudo -f /etc/sudoers.d/gms-vpn-proxy` 配置。
以下为模板：将 `GMS_SERVICE_USER` 替换为真实 Controller 进程账号；
若后端 `SYSTEMCTL` 路径是 `/bin/systemctl`，也必须调整一致。

```sudoers
GMS_SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start rk-vpn-proxy-firewall.service, /usr/bin/systemctl start rk-vpn-proxy.service, /usr/bin/systemctl stop rk-vpn-proxy.service
```

保存后检查语法，并仅验证列出的命令是否被允许；不要用
`NOPASSWD: ALL`、`systemctl *` 或让浏览器提交任意 unit 名称。

```bash
sudo visudo -cf /etc/sudoers.d/gms-vpn-proxy
sudo -l -U GMS_SERVICE_USER
```

## 3. 真机验收顺序

1. 更新并重启 **GMS Controller**，检查 `GET /api/tools/vpn-proxy/status`
   返回 `success: true`，未登录用户应受 GMS 认证策略约束；无安装时不应伪造运行状态。
2. 使用有权限的账号进入“常用工具 → vpn-proxy”，确认四个 systemd 状态和
   代理地址与实际部署一致，统计端口故障时显示不可用而非零连接。
3. 使用管理员二次验证后手动测试“停止代理 / 启动代理”。
   停止后 `rk-vpn-proxy-firewall.service` 仍必须保持保护状态。
   在有真实设备任务或下载时**不要执行停机验证**。
4. 从获准接入的客户端使用代理发起 HTTPS 请求，再检查当前 TCP 连接或
   最近活跃记录。不要把单次 `curl` 请求结束后的 TCP 连接清零误判为统计失败。
5. 检查新客户端部署命令来自已部署的 `advertise_host` 与
   `installer_port`，安装器未启用或配置不可读时不生成命令；
   拷贝命令前需按实际来源 IP 更新白名单并重新部署 vpn-proxy。

## 安全提醒

- 保持 `stats` HTTP 服务绑定 `127.0.0.1`；GMS 后端负责中转只读数据，
  不增加公网统计端点。
- 不在 GMS 配置或 Git 仓库中保存 sudo 密码、root Token；
  `sudo -n` 授权不足应清楚失败，而不是要求 Web 用户提交服务器密码。
- 代理服务开关不等于 VPN 连接开关；vpn-proxy 不创建或自动重连 VPN。
- 当前方案只验证本机安装。跨 Controller/Worker 管理需要新增独立的
  Worker 授权命令与服务身份契约，不能直接通过用户输入的 IP 执行 SSH。


## 客户端使能 / 禁用（需要升级 vpn-proxy 服务）

GMS 弹框的“当前接入客户端”现显示**源 IP、连接数、访问状态、操作按钮**。
单个 IP 的禁用记录会持久化，断开连接后仍在表格中显示，便于重新启用。
禁用后阻止新的代理请求，但**不保证立即结束已有 TCP CONNECT 长连接**。
共享 NAT 地址的多个设备可能被一起限制；本功能按 IP 而不是设备身份授权。

必须先从 [vpn-proxy master 主线](https://github.com/weixin1263831586/vpn-proxy/tree/master)
安装带 `rk-vpn-proxy-client-control` 的新版服务（合并后仍需在代理服务器运行
`sudo bash install.sh` 才能替换部署副本）。GMS Controller 与代理仍需同机。
随后为 Controller 服务用户设置**固定 helper、固定动作**的最小 sudoers 权限（先核实实际用户，
不得赋予任意 `squid` 或 `systemctl` 控制权限）：

```sudoers
GMS_SERVICE_USER ALL=(root) NOPASSWD: /usr/local/libexec/rk-vpn-proxy-client-control enable *, /usr/local/libexec/rk-vpn-proxy-client-control disable *
```

helper 本身严格校验动作只有 `enable/disable`，客户端必须是授权网段内的准确单个 IP，
且拒绝回环地址、CIDR、命令拼接和多余参数。所有操作需要 GMS 管理员二次验证；
失败不显示为成功。仍需在低风险维护窗口实测禁用、恢复和重启后记录保留。
