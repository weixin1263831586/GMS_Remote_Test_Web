# 废弃/移除 API 迁移说明

本页记录已从 Controller 移除的 HTTP API，供外部脚本、定时任务与既有
客户端迁移使用。仓库内调用残留由架构与契约测试保证为空；本页面向
**仓库外**的调用方——接口删除后，外部调用只会得到 404，本页回答
"是永久移除还是临时故障、有没有替代"。

规则：接口先在快照与文档中标记移除，删除提交必须同步更新本页；
每条记录保留被删接口、删除版本（commit）、原用途、替代路径与调用示例。

## 2026-11-14（commit 4151b19b "Clean up unused APIs and harden SDK evidence handling"）

### `GET /api/apk/doctor` — 已移除，无直接替代

- **原用途**：JADX/Java 环境健康检查（jadx 路径、版本探测、修复建议），
  供部署排障使用。
- **移除原因**：仓库内无调用方；环境健康属于部署时一次性检查，常驻
  API 暴露内部环境细节的收益为负。
- **迁移**：
  - 环境问题现在在**创建反编译任务时**以任务错误形式暴露：先
    `POST /api/apk/upload`，JADX/Java 缺失或损坏时任务状态与错误信息
    会明确给出原因。
  - 部署期环境自检使用 Agent 包的验收路径：
    `gms-agent doctor --json`（见 [Agent 安装](agent/installation.md)），
    其中包含 kkagent/kkagent 运行时依赖检查。
  - APK 分析状态用 `GET /api/apk/tasks` 观察（每个任务含 status/error）。

### `GET /api/cluster/suite-library/{filename}` — 已移除，有直接替代

- **原用途**：按文件名下载 Controller 套件库压缩包。
- **移除原因**：该路径允许任意 `filename` 直接定位磁盘文件，存在路径
  遍历面；已被带双重文件名校验的下载端点取代。
- **替代**：`GET /api/cluster/suite-library-download/{safe_filename}/{filename}`
  （`safe_filename` 是文件名的 URL 编码形式，服务端校验两者一致后才
  允许下载）。

  ```bash
  # 旧（已 404）：
  curl -k "https://<controller>/api/cluster/suite-library/android-cts.zip"

  # 新：
  curl -k -O "https://<controller>/api/cluster/suite-library-download/android-cts.zip/android-cts.zip"
  ```

  列出可用压缩包仍用 `GET /api/cluster/suite-library`；完整性校验用
  `POST /api/cluster/suite-library/{filename}/fingerprint`。

### `GET /api/agent/capabilities` — 已移除，无直接 HTTP 替代

- **原用途**：返回对话 Agent 能力清单与完整工具目录（JSON），供外部
  概览 Agent 支持面。
- **移除原因**：仓库内无调用方；工具目录随 Agent 会话动态呈现，静态
  HTTP 快照与真实可用面漂移，容易被外部脚本当作稳定性契约。
- **迁移**：
  - 能力概览内置于**对话 Agent 会话**：在会话里问"你能做什么"，
    Agent 返回基于当前注册表的能力说明（含页面跳转入口）。
  - 程序化查询 Agent 状态用 `GET /api/agent/sessions`（会话列表）与
    `GET /api/agent/sessions/{session_id}`（单会话含消息与步骤）。
  - Agent 部署验收沿用 `gms-agent doctor --json`。

## 维护

新增移除记录时：在对应小节写入被删接口、删除 commit、原用途、替代
路径与 curl 示例；有直接替代的端点必须给出可执行的新旧对照命令。
