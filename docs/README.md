# GMS Remote Test 文档

这里维护当前实现的使用与部署说明。根 [README](../README.md)提供产品概览；
架构取舍及被替代的决策保留在 ADR，历史测试数字不作为当前能力承诺。

## 使用与运维

| 主题 | 入口 | 范围 |
| --- | --- | --- |
| Controller 部署 | [快速安装](deployment/quick-install.md) | [生产服务](deployment/production.md)、[配置与迁移](deployment/configuration.md)、[Worker](deployment/worker.md)、[排障](deployment/troubleshooting.md) |
| GMS ATS | [运行管理](automation.md) | 创建、测试计划、团队可读权限、追踪、重跑、设备预约释放 |
| AOA 工作台 | [用户指南](aoa/user-guide.md) | 命令编辑、回放、调试、类别认证和语义模板 |
| USB/IP 与固件 | [接入选型](usbip/overview.md) | [ADB Proxy 对比](usbip/adb-proxy-vs-usbip.md)、[烧写](usbip/firmware-flashing.md)、[故障处理](usbip/troubleshooting.md) |
| Bootloader | [OEM 命令与恢复](devices/bootloader-oem-commands.md) | 版本判据、执行路径及失败边界 |
| Redmine 晨报 | [每日晨报](redmine-daily-brief.md) | 账号配置、后台 Worker、诊断、进度和取消 |
| Agent | [概览](agent/overview.md) | [安装](agent/installation.md)、[profile](agent/profiles.md)、[安全边界](agent/security-model.md)、[排障](agent/troubleshooting.md) |
| CLI | [约定与示例](cli/examples.md) | [生成的命令参考](cli/command-reference.md) |
| 支持环境 | [Python / Ubuntu 矩阵](platform-support-matrix.md) | 当前 CI 基线、OCR 限制及迁移条件 |

## 架构与维护

- [架构总览](architecture/overview.md)、[ADR 索引](architecture/adr/README.md)。
- [AOA 原理与实现](aoa-mechanism-and-architecture.md)：协议、坐标、持久化、接口与刷机接续。
- [AI 治理与诊断质量](architecture/ai-governance.md)：执行账本、证据、失败身份、质量门禁。
- [平台安全模型](security.md)与[开发指南](development.md)。

## 维护规则

- 操作路径、配置和能力以当前源码为准；修改相关 UI、权限、接口或加载顺序时同步说明。
- `cli/command-reference.md` 由 `python3 tools/scripts/docs/generate_cli_docs.py` 生成，禁止手改；
  `tests/contract/test_cli_docs_drift.py` 检查漂移。
- Agent 包的手工维护源位于 `agent/gms-remote-test/`；插件副本通过 sync 工具生成。
  安装包使用说明与开发源码流程分别维护，生成路径见 [Agent 概览](agent/overview.md)。
- 新 ADR 加入索引；决策被替代时保留原决策和后继指针，操作指南只介绍当前行为。
- 文档不记录本机库存、临时截图或无提交/证据锚点的“本次测试数”，也不把模拟回归当真机验收。
