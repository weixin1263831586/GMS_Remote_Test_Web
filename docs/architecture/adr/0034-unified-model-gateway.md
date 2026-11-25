# ADR-0034: Unified Model Gateway under features/kkagent

- 状态：已采纳（Accepted）
- 关联代码：`features/kkagent/`（registry/http_client/analyzer/health、
  process/errors/sandbox/config/trace/session）、
  `features/redmine/analysis/kkagent/`（领域层：analyzer/evidence_gate/trace 等）、
  `features/assistant/kkagent_bridge.py`、`bootstrap/dependencies.py`

## 背景

历史上"调用大模型"存在两套并行体系：

1. **直连 HTTP**：`features/assistant/universal_ai.py`（995 行巨石：HTTP
   传输、provider failover、健康探针、GLM 思考参数兼容、测试失败分析域
   逻辑混在一个类里），配置读 `configs/local/config.json` 的 `ai_models`。
2. **kkagent headless 子进程**：`features/redmine/analysis/kkagent/`（晨报分析）与
   `features/assistant/kkagent_bridge.py`（对话桥接）各自实现进程树回收、
   身份 env 剥离（键位完全相同的两份 frozenset）、有界流读取、stream-json
   解析与临时 config.toml 机制；模型却读宿主 `~/.kkagent/config.toml` 的
   `[models]` 表。

两条模型配置链（`ai_models` vs 宿主 `config.toml`）靠人工保持同步，
漂移即静默换模型或失败——"假统一"。

## 决策

1. **新 feature `features/kkagent` 作为统一模型网关**：模型目录
   （registry）、直连 HTTP 通道（http_client/analyzer/health）与 kkagent
   headless 引擎（process/errors/sandbox/config/trace/session）同属一个
   职责面。依赖方向 `assistant → kkagent`、`redmine → kkagent`、`reports → kkagent`、
   `kkagent → foundation`，无环（架构测试锁定）。
2. **模型目录唯一来源**：模型定义只存在于 `ai_models`；
   `registry.ModelSpec` 物化每个 enabled provider，UI 模型列表、直连
   调用与 kkagent 临时配置共用同一目录。`KKAGENT_DEFAULT_MODEL` 语义
   保留（alias = provider.model）。
3. **kkagent 临时 config.toml 由目录生成**：`[models]`/`[providers]`/
   `default_model` 按 kkagent schema 从目录物化（api_key 经
   `GMS_MODEL_API_KEY` 环境变量转发，不落盘）；宿主配置只贡献非模型段
   （mcp_servers、subagent 等身份/策略节）。沙箱策略（deny-all 工具、
   plan mode）仍走已验证的 `kkagent --config <path> config set` 机制。
   宿主 `balance_model`/`fast_model`/`fallback_model`/`quality_model`/
   `compaction_model` 与 `goal.judge_model` 不随非模型策略继承，避免引用
   已被替换的宿主模型表；这些角色由 kkagent 按选中的默认模型解析。
   模型只从统一模型目录选择；宿主模型表不参与选择。
4. **引擎多态钩子**：stream-json 通用消费核在引擎 trace；Redmine 证据域
   （issue/artifact/snapshot 归属、源码取证语义、Evidence Ledger）经
   `new_tool_call`/`record_tool_result` 钩子继承接入，判定逻辑原样保留。
5. **领域分析与网关解耦**：通用 `UniversalAIAnalyzer` 仅负责模型选择、
   文本生成、故障切换和探针；测试失败 Prompt、结构化结果及 Android 源码
   检索归属 `features/reports/failure_analysis.py` 的 `ReportFailureAnalyzer`。
   bootstrap 分别注入通用分析器和报告分析器，源码检索 provider 保持不变。

## 后果

- Redmine 专用 KKAgent 分析位于 `features/redmine/analysis/kkagent/`，保留原有领域命名；通用引擎仍位于 `features/kkagent/`，不承载 Redmine 证据策略。

- `assistant/universal_ai.py`、`assistant/provider_health.py`、
  `assistant/kkagent_runtime.py`、`redmine/kkagent/{process,errors,
  context_budget}.py` 删除，不做兼容垫片；所有调用方走
  `features.kkagent` 公共面。
- 通用分析器保留文本生成、模型探针及协议解析；报告分析器复用相同网关，
  `ai_diagnosis` 的结果契约与报告源码检索机制不变。
- API/UI 契约不变：`/api/config/ai`（含 probe）、
  `list_daily_brief_model_options` 响应形状与历史一致，OpenAPI 快照无
  变化。
- 模型增删只需改 `configs/local/config.json`；kkagent 会话与晨报分析
  自动跟随目录，双链漂移不再可能。
