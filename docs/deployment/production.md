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

打包会输出 `.tar.gz` 及配套的 `.sha256`、`.sig` 文件。打包过程使用 `scripts/sanitize_release_config.py` 清除本机凭证与部署身份，并执行 `scripts/verify_release_tree.py` 校验发布树。目标电脑部署：

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

生产环境默认关闭 Uvicorn access log，并在应用启动阶段执行 fail-closed 安全配置检查。

## systemd 服务

`install.sh` 安装并管理以下 systemd 服务：

| 服务 | 说明 |
| --- | --- |
| `gms-web-app.service` | Controller 主服务，`ExecStart` 为 Uvicorn，监听 `0.0.0.0:<端口>` 并加载 HTTPS 证书 |
| `gms-web-app-local-worker.service` | 本地 Worker Agent（`python -m worker_agent.app`），依赖主服务 |
| `gms-web-app-local-software.service` | oneshot 服务，重新配置本地 Worker 软件 |
| `gms-web-app-backup.service` / `.timer` | 每日 02:30（随机延迟 45 分钟内）执行加密备份 |

主服务关键配置：

```text
Environment=GMS_ENV=production
Environment=GMS_DATA_ROOT=<安装目录>/data
ExecStart=<安装目录>/.venv/bin/python -m uvicorn app:app \
  --host 0.0.0.0 --port <端口> --workers 1 --backlog 2048 \
  --limit-concurrency 512 --limit-max-requests 100000 \
  --timeout-keep-alive 10 --log-level info --access-log \
  --ssl-keyfile <证书目录>/gms-local.key --ssl-certfile <证书目录>/gms-local.crt
Restart=always
```

常用运维命令：

```bash
sudo systemctl status gms-web-app
sudo systemctl restart gms-web-app
sudo journalctl -u gms-web-app -f
```

## restart_services.sh

`restart_services.sh` 是仓库根目录的服务管理脚本，执行流程：

1. 通过 `bootstrap.env_loader` 加载运行环境变量（与 `app.py` 相同的优先级）。
2. 清理 Python 字节码缓存（`__pycache__`、`.pyc`、`.pytest_cache`）。
3. 备份旧日志（`fastapi.log` 重命名为 `fastapi.log.backup.<时间戳>`）。
4. 停止旧服务：若存在 `/etc/systemd/system/gms-web-app.service` 则通过 systemd 停止，并清理端口残留进程。
5. 证书缺失时自动生成自签 HTTPS 证书。
6. 启动新服务：优先通过 systemd 重启 `gms-web-app.service`；unit 不存在时回退为 `nohup` 直接启动 Uvicorn，日志写入 `fastapi.log`、PID 写入 `fastapi.pid`。
7. 健康检查：轮询 `https://localhost:<端口>/api/system/health/live`，最多 20 次、每次间隔 3 秒（清理缓存后冷启动可能超过 20 秒）。
8. 若存在用户级 Worker unit（`~/.config/systemd/user/gms-worker-agent.service`），重启本地 Worker Agent。

```bash
./restart_services.sh
```

健康检查失败时，systemd 模式查看 `journalctl -u gms-web-app -n 30`，nohup 模式查看 `tail -30 fastapi.log`。

## 日志与数据目录

- 服务日志：systemd 模式使用 `journalctl -u gms-web-app -f`；nohup 模式使用项目根目录的 `fastapi.log`。
- 数据根目录：默认 `<安装目录>/data`（环境变量 `GMS_DATA_ROOT`），保存应用运行数据与 `data/secrets/` 下的密钥文件。
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
