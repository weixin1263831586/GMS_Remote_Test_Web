# GMS Remote Test 文档

本目录是平台正式文档库。根 [README](../README.md) 只保留产品概览与快速上手；各主题的完整说明按读者与场景拆分如下。

## 目录

| 主题 | 入口 | 内容 |
|---|---|---|
| 架构 | [architecture/overview.md](architecture/overview.md) | 系统架构、模块边界、ADR 决策记录（Controller/Worker 边界、Feature/Foundation 分层、Agent Profile Store、SSH 执行边界、USB/IP 固件所有权） |
| 部署 | [deployment/quick-install.md](deployment/quick-install.md) | 快速安装、生产部署、配置体系、Worker 部署、升级排障 |
| USB/IP | [usbip/overview.md](usbip/overview.md) | 设备接入选型、固件烧写流程、ADB Proxy 对比、故障排除 |
| Agent | [agent/overview.md](agent/overview.md) | Agent Runtime 定位、安装 enrollment、安全模型、Profile 管理 |
| CLI | [cli/gms-rt.md](cli/gms-rt.md) | gms-rt 命令设计原则、[命令参考](cli/command-reference.md)（生成文档）、工作流示例 |
| 安全 | [security.md](security.md) | 认证双轨、scope/elevation、审批 Token、执行边界、秘密管理 |
| 开发 | [development.md](development.md) | 仓库布局、架构门禁、测试组织、文档政策、提交清单 |

## 文档维护规则

- `docs/cli/command-reference.md` 由 `python3 tools/generate_cli_docs.py` 生成，禁止手工编辑；新增 `gms-rt` 命令后必须重新生成（`tests/contract/test_cli_docs_drift.py` 会在漂移时失败）。
- 架构决策记录在 `docs/architecture/adr/`；源码注释引用架构决策时直接链接对应 ADR，不得引用不存在的评审文档编号。
- Agent 安装后使用视角的文档位于 `agent/gms-remote-test/docs/`；本目录 `docs/agent/` 只维护平台管理员 / 部署者视角，不复制命令表。
