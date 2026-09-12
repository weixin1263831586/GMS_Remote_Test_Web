# ADR-0007: 按用户隔离的配置存放在 configs/secrets，而不是 data/

- 状态：已采纳（Accepted）
- 关联代码：`foundation/config_paths.py`（`owner_config_path` / `sanitize_owner_id` /
  `ensure_owner_config_dir`）、`features/redmine/users.py`（`owner_runtime_config_path`）、
  `features/redmine/config.py`、`features/gerrit/settings.py`、
  `scripts/migrate_config_layout.py`（`migrate_owner_configs`）、
  `tests/contract/test_config_paths.py`

## 背景

Redmine / Gerrit 看板支持多用户（owner）隔离配置：每 owner 一份
`config_runtime.json`，内含 `redmine_auth`（加密口令/API Key）与看板偏好。
历史上该文件放在 `data/<feature>/by_user/<owner>/config_runtime.json`。

`data/` 的定位是可再生运行数据（`configs/README.md`：「`GMS_DATA_ROOT`
继续控制运行时设置与设备状态的存储根目录；**配置与凭证仍位于项目的
`configs/`**」）。把加密凭据放进 `data/` 造成两类事故：

1. 清理 `data/`（用户合理假设只清运行数据）会连带丢掉所有用户的
   Redmine/Gerrit 凭据与看板配置；
2. `data/secrets/master.key` 与凭据同树，备份/清理策略无法区别对待。

## 决策

1. **canonical 位置**：`configs/secrets/<feature>/by_user/<owner>/config_runtime.json`
   （`<feature>` ∈ `redmine` | `gerrit`）。它属于配置/凭证，随部署持久，
   权限 0600（目录 0700），与 `worker_tokens.json`、`runtime_credentials.json`
   同级管理。
2. **可再生数据不动**：`data/<feature>/by_user/<owner>/` 继续存放
   Redmine 镜像库、快照、附件、knowledge、daily_brief 等 SQLite/文件——
   这些可从 Redmine 重新同步，留在 `data/` 是正确分类。
3. **迁移期回落**：`owner_config_path()` 沿用 canonical 缺失 → legacy
   回退模式（与 `static_config_path` 等一致）；写入永远走 canonical。
   依据 `configs/README.md` 的 0.19/0.20 节奏，legacy 回退属迁移期兼容层。
4. **离线迁移**：`scripts/migrate_config_layout.py` 新增
   `migrate_owner_configs()`，与主布局迁移解耦（主迁移在已迁移部署上可能
   因遗留文件拒绝执行，owner 迁移必须仍可独立完成）。搬移前先备份到
   `data/config-migration-backups/owner-configs-*`，校验逐字节一致后删除
   原文件；幂等（canonical 已存在则跳过）。
5. **owner 目录名清洗**：统一 `sanitize_owner_id()`（原有各处内联清洗
   收口到 `foundation`，空值回退 `anonymous`，杜绝路径穿越）。

## 后果

- 删除 `data/` 不再影响任何用户配置/凭据；删除 `configs/` 之前必须意识到
  其中现在含 per-owner 凭据（目录 0700/0600 已约束）。
- `master.key` 仍在 `data/secrets/`（既有加密身份，README 明确保持原引用）；
  若 `data/` 被删，主密钥丢失会使 per-owner 加密凭据不可解密——需重新录入。
  这是既有行为，本 ADR 不改变加密身份位置。
- 0.20 收敛时删除 legacy 回退分支，并在 `scripts/migrate_config_layout.py`
  保留搬移能力直到确认无存量部署。

## 佐证

- `tests/contract/test_config_paths.py`：canonical 默认、legacy 回退、
  canonical 优先、owner 清洗。
- `features/gerrit/tests/test_config.py`：per-owner 路径期望已切到
  `configs/secrets/gerrit/by_user/`。
- `tests/contract/test_config_migration.py`：迁移备份/回滚契约继续成立。
