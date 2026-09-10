# GMS Remote Test — Agent Playbook

面向 AI agent 的运维手册,收录真实踩坑经验。冷启动时先跑 `gms-rt-system-selfcheck`,
再按需查阅本页。人机通用;命令均为 `gms-rt-*` 全名(MCP 下亦同)。

## 1. 冷启动 / 环境自检

```bash
gms-rt-system-selfcheck --json
```

一次返回:凭据模式、认证身份与 scopes、server 健康、设备清单、本地可见套件路径、
可行动 hints。任何一项失败都带修复提示,优先按 hints 走。

自签名 TLS 部署需要:

```bash
export GMS_CURL_INSECURE=1        # 或 GMS_CURL_CA_CERT=/path/to/ca.crt
```

## 2. 凭据与会话

| 场景 | 处置 |
| --- | --- |
| agent token 过期 / 401 | Web UI 管理员铸造一次性配对码(5 分钟 TTL)→ `gms-rt-agent-enroll <CODE>` |
| token 文件 | 默认 `~/.local/state/gms-remote-test/<profile>.token`(0600),通过 `GMS_AUTH_TOKEN_FILE` 注入 |
| 人工会话 | `gms-rt-auth-login <user> --password-stdin`;提权 `gms-rt-auth-elevate` |
| 危险操作审批 | 人工会话下 `gms-rt-approval-create`;agent token 永远无法自审批(服务端强制) |

退出码语义(全部命令统一):`0` 成功,`2` 用法错误,`3` 未认证,
`4` 权限/需提权,`5` 冲突/设备忙,`6` 网络/超时,`7` 操作失败。

## 3. 升级流程

1. 备份:`cp runtime/gms-remote-test.sh runtime/gms-remote-test.sh.bak-$(date +%F)`
2. 替换 `agent/gms-remote-test/` 下的源文件(这是唯一手改源树)
3. 同步生成树:`python3 tools/sync_agent_package.py`(生成 `plugins/gms-remote-test/`)
4. 重新注册 MCP 配置(如 launcher 变更):检查 `~/.kkagent/config.toml` 的
   `[mcp_servers.gms]` 是否仍指向 `runtime/mcp_server.py`,env 中是否带
   `GMS_AUTH_TOKEN_FILE` 与 `GMS_CURL_INSECURE=1`
5. 验证:`gms-rt-system-version` + `gms-rt-system-selfcheck --json`

**坑**:只改 `agent/` 不同步 `plugins/` 会导致升级后行为漂移;sync 之后
`git status` 应只剩预期文件。

## 4. 测试执行要点

- 长任务(CTS 模块几十分钟)一律 `wait=false` 拿 job_id,然后
  `gms-rt-jobs-status` 每 20-30s 轮询;增量事件用 `gms-rt-jobs-events`(带 `after` 游标)。
- 跑 BYOD / CTS-V 前检查设备上是否有残留 work profile:
  `adb shell pm list users`,残留用户(如 10/14)先用
  `adb shell am broadcast -a android.intent.action.MANAGED_PROFILE_REMOVED` 或
  设置中移除,否则 Managed Provisioning 模块会假失败。
- 设备锁屏 PIN 会阻塞自动化;必要时人工解锁,或 `adb shell input text <PIN>`。

## 5. 设备问题速查

- `gms-rt-cluster-workers` → 多 worker 部署先拿 `worker_id`,再带它调设备类工具,
  避免 serial 歧义(exit 5)。
- `gms-rt-devices-logcat` 只支持 dump 模式;`logcat -c` 是人工操作(MCP 层拒绝)。
- 截图:`gms-rt-devices-screencap <serial>` 返回 base64 PNG;MCP 工具 `gms_rt_devices_screencap` 直接返回图片内容,无需 screencap/pull。
- 固件烧写、shell 变更类操作都要一次性 approval token,见 §2。

## 6. 日志与报告

- 测试日志是数百 MB 文本:先 `gms-rt-test-suites-result` 拿结果索引,
  再按 offset 窗口读需要的段落;`summary` 优先于全文。
- Redmine 证据:`gms-rt-redmine-issue-fetch` 全量快照 → journals 用
  `gms-rt-redmine-journals` 游标分页(无 2000 字截断)。
