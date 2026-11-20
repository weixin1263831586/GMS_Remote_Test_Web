# 独立 GMS 平台：编译发布与首次部署

适用实验分支：ui/unify-page-edge-8px-20261120。
首版目标：Ubuntu 22.04 x86_64。此文档描述编译版独立部署链路，
不是已经完成全功能实机验证的生产发布证明。

## 一、保护边界

- 发布包不能含有项目后端 Python .py、.pyc、测试数据、源码映射、Git 历史、
  开发主机的账号和证书；构建脚本会执行 fail-closed 扫描。
- Nuitka Standalone 编译提高后端逆向成本，但无法保证绝对不可逆向。
  Web HTML/CSS/JS 必须下发到浏览器，无法隐藏。核心逻辑绝对不交付时，
  必须改为调用开发方持有的服务，与完全独立部署目标存在冲突。
- 动态 Python 插件、CLI/MCP 和外部工具尚需逐个通过运行测试，不能只验证
  native Controller 启动就认定 CTS/GTS、AI、Agent 分发等全部可用。

## 二、可信构建机编译

要求：Ubuntu 22.04 x86_64、Python 3.10、编译工具链（gcc）、
Nuitka、patchelf、与仓库匹配的 requirements.txt、受控的 GPG 发布签名私钥。
patchelf 必须在构建时可执行；自托管 Runner 可安装到临时 venv，
不必修改宿主系统的包管理状态。

~~~bash
python3 -m pip install -r requirements.txt
python3 -m pip install 'nuitka>=2.7,<3' ordered-set zstandard patchelf
GMS_RELEASE_SIGNING_KEY='<签名密钥ID>' \
    ./install.sh package --version 1.0.0 --dist-dir dist
~~~

目前 install.sh package 默认构建 compiled-v1：
Nuitka 编译 -> 静态资源白名单 -> 源码/私钥/内层档案检查 ->
native doctor -> 临时隔离环境 init 和 production smoke ->
生成清单 -> tar.gz、SHA256 和 GPG detached signature。

内部开发如需旧源码发布包，只允许显式 GMS_ALLOW_SOURCE_PACKAGE=true。
这种源码包不能交付给不可信主机。

## 三、目标主机签名验证与安装

先通过可信渠道获取发布者 GPG 公钥并核对指纹。三件套：

~~~text
gms-web-app-1.0.0-ubuntu22.04-x86_64.tar.gz
gms-web-app-1.0.0-ubuntu22.04-x86_64.tar.gz.sha256
gms-web-app-1.0.0-ubuntu22.04-x86_64.tar.gz.sig
~~~

~~~bash
PKG=gms-web-app-1.0.0-ubuntu22.04-x86_64.tar.gz
sha256sum -c "$PKG.sha256"
gpg --verify "$PKG.sig" "$PKG"
tar -xzf "$PKG"
cd gms-web-app
sudo ./install.sh
~~~

安装脚本要求 root/sudo，会确认服务用户、IP/域名和 HTTPS 端口，
校验内层 manifest 并安装系统依赖。离线且预装完整依赖时：

~~~bash
sudo ./install.sh --host 192.0.2.40 --port 5001 --offline
~~~

默认独立服务账号 gms-web，端口 5001。
安装程序会调用编译运行时的 init 功能：

1. 自动创建 HTTPS 自签证书、密钥、审计密钥、签名密钥、Worker Token 等；
2. 提示管理员名称、显示名、密码及二次确认，密码经 getpass 安全输入；
3. 可配置 Redmine URL 与 API Key 或用户名/密码，绑定到该管理员的
   按 owner 隔离的加密配置；无须 Redmine 可跳过；
4. 注册 Controller、Local Worker 和加密备份 systemd timer。

--yes 只跳过主机参数询问，不会代替管理员密码输入。明确指定
--no-admin-prompt 才可延后到 Web 的已有 Bootstrap Token 保护初始化流程。
Token 只保存在目标主机 root/service-user 可读的私有文件。
不要把密码、Token 写入 shell argv、工单或日志。

## 四、版本与持久化目录

~~~text
/opt/gms-remote-test/
  releases/<version>/runtime/    # 独立的 native 可执行文件与 Web
  current -> releases/<version>/runtime
/etc/gms-remote-test/configs/
  examples/  local/  secrets/     # 每台机器独立生成，密钥 0600
/var/lib/gms-remote-test/
  platform_auth.sqlite3
  local-worker/  reports/  uploads/  backups/
~~~

新发布复制到新版本目录。config/data 始终引用持久化路径。
同版本文件内容不一致时安装器拒绝静默覆盖；安装后校验
/api/auth/status，失败则尝试恢复旧版本的 current 链接。
业务数据、身份和备份密钥不在应用程序版本目录中。

## 五、必需配置和功能启用

**核心必需：** HTTPS、服务器地址/端口、服务账号、管理员、
系统安全密钥、本地 Worker Token。密钥尽量自动生成。

**Redmine 启用时必需：** URL、API Key 或账号密码，随后在系统设置
检查网络、TLS 与账号权限。安装时保存配置不表示真实连通已成功。
按用户隔离凭据，不把管理员个人凭据自动共享给其他用户。

**其他功能按需配置：** Gerrit REST/SSH、SMTP、AI 模型/API Key、
JDK/SDK/ADB/Fastboot、GTS 凭据、CTS/GTS/VTS/STS 套件、
USB/IP 设备、固件编译服务器和 Code Search。
配置缺失应使对应模块显示未就绪，不应绕过认证或生产密钥门禁。

## 六、运维和交付验收

~~~bash
sudo systemctl status gms-web-app.service
sudo systemctl status gms-web-app-local-worker.service
sudo systemctl status gms-web-app-backup.timer
sudo journalctl -u gms-web-app -n 100 --no-pager
curl -kfsS https://127.0.0.1:5001/api/auth/status
~~~

升级时使用新版本签名包重新安装；版本切换前备份数据。
只有数据库 schema 和密钥兼容时才能单纯回退旧程序，
否则需要离线恢复经验证的加密备份。
独立托管备份密钥，不能只靠备份文件恢复。

正式交付的独立实例需额外证明：
- 真实 Ubuntu 22.04 / x86_64 编译并通过 native production smoke；
- Controller 登录、权限、静态资源、WebSocket、报告和 APK 分析；
- Redmine/Gerrit/SMTP/AI/Agent 分发、远程工具与 CLI 实际联调；
- Local Worker 注册、设备租约、真实 CTS/GTS 和烧写流程；
- 可重复安装、升级、回退、备份恢复、无源码与无密钥泄漏。

静态契约测试可执行：

~~~bash
python3 -m unittest -v tests.test_compiled_release
bash -n scripts/release/install_compiled.sh
~~~

当前分支是实施和验证阶段；未实际生成经全流程测试的签名
二进制制品之前，不应声明可直接向外部环境生产交付。
