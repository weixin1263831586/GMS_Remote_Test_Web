# 生产部署

本文面向正式环境部署 Controller：运行入口、systemd 服务、`restart_services.sh`、日志与数据目录。

## 安装方式

推荐使用项目自带安装器，而不是手工启动 Uvicorn：

```bash
./install.sh
```

安装步骤与参数见[快速安装](quick-install.md)。

### 跨机分发安装包

`install.sh` 支持 `package` 子命令，生成可复制到其他电脑的签名安装包：

```bash
GMS_RELEASE_SIGNING_KEY=<GPG key ID> ./install.sh package \
  --dist-dir dist --package-name gms-web-app --version <version>
```

打包会输出 `.tar.gz` 及配套的 `.sha256`、`.sig` 文件。打包过程使用 `scripts/security/sanitize_release_config.py` 清除本机凭证与部署身份，并执行 `scripts/security/verify_release_tree.py` 校验发布树。目标电脑部署：

```bash
sha256sum -c <archive>.sha256
gpg --verify <archive>.sig <archive>
tar -xzf <archive>.tar.gz
cd <package-name>
./install.sh
```

> 发布打包必须设置 `GMS_RELEASE_SIGNING_KEY`（GPG signing key ID）。

## 运行入口

源码入口为：

```bash
python app.py
```

内部由 Uvicorn 启动 FastAPI：

```text
host               = settings.server_host
port               = settings.server_port
keep-alive         = 120s
limit_concurrency  = 500
```

使用 `python app.py` 启动时，生产环境默认关闭 Uvicorn access log，并在应用启动阶段执行 fail-closed 安全配置检查。安装器创建的 systemd 服务使用 `python -m bootstrap.server`，其 `--access-log` 显式开启访问日志、并采用 `--limit-concurrency 512`（不同于 `app.py` 的 500）。`restart_services.sh` 无 systemd 回退也采用 512 并显式开启访问日志。排查吞吐与日志时，以实际启动入口和启动参数为准。

## systemd 服务

`install.sh` 安装并管理以下 systemd 服务：

| 服务 | 说明 |
| --- | --- |
| `gms-web-app.service` | Controller 主服务，通过 `bootstrap.server` 启动 Uvicorn，监听 `0.0.0.0:<端口>` 并加载 HTTPS 证书 |
| `gms-web-app-local-worker.service` | 本地 Worker Agent（`python -m worker.app`），依赖主服务 |
| `gms-web-app-local-software.service` | oneshot 服务，重新配置本地 Worker 软件 |
| `gms-web-app-backup.service` / `.timer` | 每日 02:30（随机延迟 45 分钟内）执行加密备份 |

主服务关键配置：

```text
Environment=GMS_ENV=production
Environment=GMS_DATA_ROOT=<安装目录>/data
ExecStart=<安装目录>/.venv/bin/python -m bootstrap.server app:app \
  --host 0.0.0.0 --port <端口> --workers 1 --backlog 2048 \
  --limit-concurrency 512 --timeout-keep-alive 120 \
  --log-level info --access-log \
  --ssl-keyfile <证书目录>/gms-local.key --ssl-certfile <证书目录>/gms-local.crt
Restart=always
```

单元不设置 `--limit-max-requests`。请求数上限会周期性回收进程，中断
WebSocket 和正在进行的分块上传。保持连接超时与 `python app.py` 一样是 120 秒。
已安装环境需要重新执行安装脚本或手动改 unit 后 `daemon-reload`，才会换上这一组参数。

`bootstrap.server` 保留 Uvicorn CLI 参数，默认使用项目的 HTTP 协议实现。
停机时，Worker 空闲命令轮询在下一次检查（最多约 0.5 秒）正常返回，
不再领取新命令；已完成响应且应用写缓冲为空的 HTTPS 连接，等待 TLS
关闭握手 2 秒后释放。正在执行的请求和仍有数据待发送的响应继续排空。
显式 `--http h11` / `--http httptools` 会覆盖该默认协议。

常用运维命令：

```bash
sudo systemctl status gms-web-app
sudo systemctl restart gms-web-app
sudo journalctl -u gms-web-app -f
```

## restart_services.sh

`restart_services.sh` 是仓库根目录的服务管理脚本，执行流程：

1. 通过 `bootstrap.env_loader` 加载运行环境变量（与 `app.py` 相同的优先级）。
   环境配置读取或校验失败时立即退出，不停止现有服务。
   停止服务前准备 HTTPS 证书，并更新已安装的 Controller、Worker、晨报定时任务、工具安装和备份服务入口。
   更新保留 unit 中的启动参数、运行身份和环境设置。仅在 unit 需要重新载入时执行 `daemon-reload`，
   系统级修改按需由 sudo 在操作者终端提权。需要更新入口的 Worker 或晨报 unit 若缺少当前模块，
   重启会在停止服务前退出，需先部署该工作目录。
2. 默认保留 Python 字节码缓存，避免全仓扫描和重复冷启动。显式传入 `--clean-cache`
   时，单次遍历项目源码目录清理 `__pycache__`、`.pyc`、`.pyo`、`.pytest_cache`，
   跳过运行数据、配置、虚拟环境和第三方工具目录。
3. 备份旧日志（`fastapi.log` 重命名为 `fastapi.log.backup.<时间戳>`）。
4. 停止旧服务：若存在 `/etc/systemd/system/gms-web-app.service` 则通过 systemd 停止，并清理端口残留进程。
5. 使用 `configs/secrets/certs/` 中的证书启动，配置迁移与备份恢复使用规范目录。
6. 启动新服务：优先通过 systemd 重启 `gms-web-app.service`；unit 不存在时回退为 `nohup` 直接启动 Uvicorn，日志写入 `fastapi.log`、PID 写入 `fastapi.pid`。
7. 健康检查：立即探测 `https://localhost:<端口>/api/system/health/live`，失败后间隔 1 秒重试，
   总时限约 60 秒，单次请求上限 2 秒。脚本显示配置准备、六个阶段和整体的耗时。
8. 重启已安装的晨报 Worker、已启用或正在运行的系统级设备 Worker，以及已安装的用户级设备 Worker。

```bash
./restart_services.sh
# 需要清理项目源码缓存时：
./restart_services.sh --clean-cache
```

首次使用新脚本时，旧进程仍按原协议退出；新进程启动后，后续重启才会
使用优化后的 TLS 停机行为。

健康检查失败时，systemd 模式查看 `journalctl -u gms-web-app -n 30`，nohup 模式查看 `tail -30 fastapi.log`。

## 日志与数据目录

- 服务日志：systemd 模式使用 `journalctl -u gms-web-app -f`；nohup 模式使用项目根目录的 `fastapi.log`。
- 数据根目录：默认 `<安装目录>/data`（环境变量 `GMS_DATA_ROOT`），包含持久账号、任务、AOA 资产及证据。安装器仍将部分身份密钥放在 `data/secrets/`；密钥实际路径由配置决定，迁移与清理范围见[配置主文](configuration.md#身份密钥与-data-的保留范围)。
- 证书目录：`configs/secrets/certs/`（`gms-local.crt` / `gms-local.key`）。
- 备份目录：`/var/backups/gms-web-app`，加密密钥为 `/etc/gms-web-app/backup.key`（root-only，需另行托管副本）。手动触发：

```bash
sudo systemctl start gms-web-app-backup.service
```

## 生产安全基线

`GMS_ENV=production` 时应用启动会执行 fail-closed 安全配置检查，至少应确保：

```text
GMS_ENV=production
GMS_AUTH_REQUIRED=true
GMS_SECURE_COOKIES=true
```

并配置 `TRUSTED_HOSTS`、`GMS_ALLOWED_ORIGINS` 与各类 Token，详见[配置体系](configuration.md)。

## 相关文档

- [快速安装](quick-install.md)
- [配置体系](configuration.md)
- [Worker 部署](worker.md)
- [常见部署问题](troubleshooting.md)

### Controller 单进程约束

GMS 更新监控与 Mainline 同步采用进程内互斥及状态。
生产必须保持 `--workers 1`；如增加 Uvicorn Worker，会引起重复同步或进程间状态不一致。
多 Controller 部署必须先迁移到持久化任务队列和跨进程租约。
