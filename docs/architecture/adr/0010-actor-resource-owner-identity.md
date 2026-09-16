# 0010. Actor 与 ResourceOwner 身份分离

- 状态: 已接受
- 日期: 2026-09-16
- 相关: ADR 0002（Feature/Foundation 边界）、ADR 0006（Agent Service 边界）

## 背景

平台同时存在两类主体：

- **人类用户**：`platform_users.id`（如 `hcq`）。
- **Agent Service Token**（ADR 0006）：认证后得到合成 principal id
  `agent:<token_id>`，其 token 记录里另有 `owner_user_id` 指回登记它的账号。

改造前，两处在“资源归属”语义上分裂：

| 位置 | 归属取值 |
| --- | --- |
| `owner_id_from_request()`（Redmine/KB 等 owner-scoped 数据） | `owner_user_id`（人类账号） |
| `principal_owner_id()`（Build/Cluster/Transfer 等） | `principal.id` = `agent:<token_id>` |

由此产生一个隐性危险：**Agent token 轮换会制造资源孤儿**。用 token A 创建的
Build 任务 owner 记为 `agent:agt_A`；轮换为 token B（`agent:agt_B`）后，同一
账号既看不到、也无法拥有该任务；审计日志里“谁创建的”与“谁拥有资源”也被混为一谈。

## 决策

把 principal 显式拆成两个身份，二者分别承载不同职责：

- `CurrentUser.id` / `actor_id` —— **acting principal**，审计归属身份，Agent 场景
  下是稳定的 `agent:<token_id>`（token 生命周期内不变）。
- `CurrentUser.resource_owner_id` —— **资源归属账号**，人类 principal 即其自身 id；
  Agent principal 即 token 的 `owner_user_id`。

配套规则：

1. `principal_owner_id(request)` 返回 `resource_owner_id`，供**新建资源的归属**使用。
2. 新增 `principal_actor_id(request)` 返回 `actor_id`，供**审计/溯源**使用。
3. `require_resource_owner*` 的归属比较改用 `resource_owner_id`：Agent token 可访问
   其登记账号拥有的资源，与 `owner_id_from_request()` 既有语义一致。
4. 人类 principal 的 `id == resource_owner_id`（dataclass 默认自填），行为不变。
5. 数据库里的归属列（`owner_user_id` / `owner_id` 等）继续存**账号 id**；Agent 的
   合成 id 只出现在审计字段，不进归属列。

## 后果

- Agent token 轮换/吊销后，账号仍拥有先前由其 Agent 创建的资源，不再产生孤儿。
- 审计与业务归属解耦：`created_by_actor_id`（审计）与 `owner_user_id`（业务）各司其职。
- 现有以人类账号 id 归属的数据不受影响；既有 `agent:<token_id>` 归属行仍可读，
  但新写入统一走账号 id。

## 测试

- `features/auth/tests/test_agent_tokens.py`：Agent principal 的
  `resource_owner_id == owner_user_id`、`actor_id == agent:<token_id>`。
- Build/Cluster toggle 测试：Agent 创建的资源 owner 为登记账号；轮换 token 后
  同账号仍可查询（ownership 不随 token 变化）。
