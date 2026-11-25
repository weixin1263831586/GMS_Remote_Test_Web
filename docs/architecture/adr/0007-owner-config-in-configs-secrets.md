# ADR 0007：用户配置存储在 configs/secrets

状态：已实施。

用户的 Redmine、Gerrit 等私密配置只保存在 `configs/secrets/<feature>/by_user/<owner>/config_runtime.json`，与运行数据目录分离。路径由 `foundation/config_paths.py` 统一解析，owner ID 经过安全清洗，写入时限制目录权限。

不提供旧目录回退和一次性导入工具。部署前确认规范配置及原身份密钥已就绪；保留正常的加密备份和恢复能力。

详见[配置体系](../../deployment/configuration.md)及 `tests/contract/test_config_paths.py`。
