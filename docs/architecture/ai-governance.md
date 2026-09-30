# AI 执行治理与诊断质量层

本页描述 GMS 平台的 AI 调用治理（execution receipts）与诊断质量工程
（golden corpus、failure identity、外部知识 chunk 检索），以及约束它们的
架构门禁。这些子系统回答全局代码审查指出的核心缺口：**AI / Agent /
知识快速成长，但缺少质量评测与 AI execution governance**。

设计原则（与 ADR-0013 / ADR-0014 一致）：

- **诊断质量不截断**：单次诊断不设 steps/token 预算，证据搜到哪跟到哪；
  治理发生在**操作层**（防重复调用、不确定态显式化、质量回归门禁）。
- **Wiki 永远是 background**：Android Internals 解释"Android 为什么这样
  工作"，不是 root-cause 证据（P4 级，见 ADR-0014 证据分级）。
- **确定性先于 AI**：指纹、关系分类、语料评分都是确定性函数；LLM judge
  只是后续增强层，不得越过确定性层直接合并/判定。

## 1. AI Execution Ledger（逻辑调用账本）

`features/redmine/ai_execution_ledger.py`。每次 AI 逻辑调用先登记
receipt，`logical_key = sha256(owner, issue, input_hash, prompt_version,
analyzer_version, model)`；同一逻辑键存在活跃 receipt 时拒绝重复发送。

```mermaid
flowchart TD
    Begin[service._analyze_one 开始] --> LedgerBegin["ai_ledger.begin()
    logical_key = sha256(owner, issue,
    input_hash, prompt_version, model)"]
    LedgerBegin -->|"活跃 receipt 存在"| Duplicate["record.status = failed
    error_type = ai_call_in_flight
    （页面重复点击 / worker 重试被挡）"]
    LedgerBegin -->|无活跃| Pending["receipt: pending"]
    Pending -->|请求发出| Received["mark_received()
    receipt: received"]
    Received -->|outcome 确定| Finish["ai_ledger.finish(ok)
    completed / failed（首终态不可改写）"]
    Received -->|取消 / 进程异常| Unknown["mark_unknown()
    unknown = 结果不确定（终态）"]
    Pending -->|租约 4h 超时
    （持有进程已死）| Expired["置 unknown + 放行新 attempt"]
    Finish --> Retry["同一逻辑键再次 begin()
    → 新 receipt（attempt+1）"]
    Unknown --> Retry
```

要点：

- `unknown` 是**一等终态**，不是 failed 的别名——后续重试据此知道前一次
  outcome 未知，而不是误以为干净的失败。
- 终态不可改写：迟到的 completed 不得覆盖已记录的 failed/unknown。
- per-owner SQLite（与 daily brief 同库），建表走 `BEGIN IMMEDIATE`，
  Web/Worker/CLI 并发迁移安全。
- `record_ai_execution` 的 per-attempt 轨迹审计照旧保留；ledger 是其上的
  **逻辑调用层**，两者不互相替代。

## 2. Failure Identity / Cluster（失败身份层）

`features/redmine/failure_identity.py`。相似检索（`analysis_similarity`，
retrieval score）与关系判定明确分层：score 高 ≠ 同一失败。

```mermaid
flowchart TD
    Failure[Test Failure] --> Identity["failure_identity()
    suite/module/testcase/assertion_class
    + normalized error signature
    + android_version/device_class"]
    Identity --> FP["fingerprint = sha256(canonical)"]
    FP --> Exact{"relation_class()"}
    Exact -->|"指纹一致"| Same["SAME_FAILURE"]
    Exact -->|"同 module+testcase，签名不同"| Diff["SAME_TEST_DIFFERENT_CAUSE
    （同名用例不同根因：禁止合 Cluster）"]
    Exact -->|"签名一致，用例不同"| Sim["SIMILAR_SYMPTOM
    （仅候选：合并需 relation judge 二次确认）"]
    Exact -->|其他| Unrel["UNRELATED"]
    Same --> Cluster["build_failure_clusters()
    仅 exact fingerprint 聚合"]
    Diff -.->|AI 后续层| Judge["LLM relation judge
    （SAME_ROOT_CAUSE / REGRESSION_OF …，低置信二次确认）"]
    Sim -.-> Judge
    Cluster --> Consumers["Redmine 案例 / 报告分析 / 每日晨报"]
```

要点：

- 归一化剥掉地址/十六进制/时间戳/计数——同一根因两次失败得到同一指纹，
  异常类型/调用点不同不会被归一掉。
- 同名 CTS 用例在设备 A/B/C 上完全可能各有根因；`SAME_TEST_DIFFERENT_
  CAUSE` 从确定性层就禁止合并。
- 指纹随 AI 执行轨迹落库（`record_ai_execution` 的 `failure_identity`
  字段），跨 attempt / 跨 issue 聚合无需重算。

## 3. Diagnosis Golden Corpus（诊断质量回归）

`tests/quality/`（语料 + 契约测试）与 `tools/scripts/testing/
eval_diagnosis_quality.py`（评测脚本）。把 Wiki Golden Query 的质量门禁
思想扩展到 AI 诊断：**测该找的证据有没有找到、有没有漏查历史案例、
有没有拿 Wiki 当根因、有没有胡编声明**，不比较生成 Markdown 长相。

```mermaid
flowchart LR
    Corpus["tests/quality/*.jsonl
    development / holdout 分池"] --> Contract["契约测试
    （schema 封闭词表 / split 平衡）"]
    Corpus --> Eval["eval_diagnosis_quality.py
    --split development|holdout"]
    Result["DiagnosisReadModel 结果载荷
    history_checked / evidence_sources /
    root_cause_class / claims / evidence_refs"] --> Eval
    Eval --> Grade["grade_result() 确定性评分
    evidence_recall / history_searched /
    test_source_used / no_forbidden_claims /
    conclusion_anchored"]
    Grade -->|development| Dev["日常调 Prompt 只看开发集"]
    Grade -->|holdout| Hold["release candidate 才跑，
    防'对着考卷调参'"]
    Contract --> CI["CI：语料本身永远合法"]
```

要点：

- 根因类 / 禁止声明的**封闭词表**：语料不会漂移出无法统计的自由文本。
- `history_checked` 由运行时从工具轨迹注入，模型自报不算——防"编造查过
  历史"。

## 4. 外部知识 Section 级检索（schema v4）

`features/knowledge/external/android_internals.py`。消费方式从 page 级
升级为 Article → Section（heading chunk），对齐上游 Knowledge Pack 的
chunk 语义，但**继续本地 clone 自建索引，不打包再分发上游 pack**
（license 边界见 ADR-0014 第 5 节）。

```mermaid
flowchart TD
    Clone["管理员本地 clone
    （pinned revision，人工批准后才 reindex）"] --> Policy["load_policy()
    显式 distribution 选择：
    android_internals → smartperfetto 候选序
    未知 distribution → fail-closed"]
    Policy --> Eligible{"policy.is_eligible()"}
    Eligible -->|否| Skip["跳过（status 计数暴露）"]
    Eligible -->|是| Pages["wiki_pages（page 级，BM25+CJK bigram）"]
    Pages --> Sections["_split_sections()
    heading + start_line/end_line + content_hash"]
    Sections --> SFTS["wiki_sections_fts（schema v4）"]
    Query["search(query)"] --> PageHit["page 级 FTS"]
    Query --> SectionHit["section 级 FTS"]
    PageHit --> Merge["同页去重：保留最佳 section
    hit.extra.section = {heading, start_line, end_line}"]
    SectionHit --> Merge
    Merge --> Rerank["version-aware rerank"]
    Rerank --> Anchor["Agent 引用：
    mechanism @ revision path:line-range
    → 送 codesearch 验证"]
```

要点：

- section 命中带**源文件行号锚点**（frontmatter 之后 body 的 1-based 行
  号），Binder/Zygote/LMKD 不再只返回上百 KB 文章的一个宽泛 snippet。
- 旧索引（user_version < 4）自动退化为 page 级检索，reindex 后启用
  section——不要求部署侧立即迁移。
- `policy_state_summary` 现记录 `policy_distribution` /
  `policy_projection_revision`："字典里的第一个"不再是内容信任边界；
  上游只声明未知消费方 distribution 时 fail-closed（不索引正文）。

## 5. Assistant 动态调用通道与门禁体系

Assistant 通过字符串引用平台能力（`executor_ref` /
`_fetch_router_json`），AST import 图看不见这条通道；专用门禁把它纳入
与普通 import 同等的边界约束。

```mermaid
flowchart TD
    Tools["tools.py 注册 128 个工具
    executor_ref = 'features.<feature>.<module>:<symbol>'"] --> Exec["executor.py
    importlib.import_module(ref)"]
    Exec --> Route["route_invocation.py 直调路由函数"]
    Tools -.-> GateA["test_assistant_dynamic_imports.py
    ①目标模块/符号必须存在且公开
    ②feature 白名单（shrink-only）
    ③禁 routers./core./modules. 通道
    ④facade 声明 ratchet（FACADE_PENDING_MODULES 只减不增）"]
    Exec -.-> GateA
    Normal["普通 Python import"] -.-> GateB["test_dependency_rules.py
    跨 feature 只准走 __init__ 公共表面"]
    Bridge["features/* ↔ worker_agent"] -.-> GateC["test_controller_worker_boundary.py
    文件 → 精确 module 两级 allowlist（双向 + stale 检查）"]
    All["全部响应出口"] -.-> GateD["test_api_error_semantics.py
    record_internal_error 契约：
    (logger, action, log_context[, context={...}])
    log_context 禁 % 占位符"]
    Gate["__all__ 逐个 getattr
    test_feature_public_surfaces.py"] -.-> GateB
```

## 6. 统一错误出口（record_internal_error 契约）

`foundation/error_model.py`。占位符错配（logging "not all arguments
converted"）与字面 `%s` 残留从机制上消除：

```mermaid
flowchart LR
    Route["路由 except 块"] --> Call["record_internal_error(logger,
    action, 'context text', context={'serial': s})"]
    Call --> Msg["message = '<action>失败：服务内部错误
    （request_id=…，已记录日志）'"]
    Call --> Log["logger.error('%s%s: %s', context, suffix, message,
    exc_info=True)
    固定 3 占位符：数量错配不可能"]
    Msg --> Resp["ApiError.internal(message).to_response()
    客户端只见动作短语 + request_id"]
    Log --> Server["完整 traceback 只进服务端日志
    （request_id 回查）"]
    Call -.-> Gate["架构门禁：新增位置参数 /
    log_context 带 % → CI 失败"]
```

## 7. DiagnosisReadModel（统一诊断读模型）

全局审查第二十节的落地：Web / CLI（`gms-rt-*`）/ MCP（`gms_rt_*`）/
Assistant 此前各自消费不同的诊断负载形状（reports 的平铺编排 dict、
Redmine 的 `IssueResult` schema），同一段诊断结论在每个消费面重复适配。
现在由 `foundation/diagnosis_read_model.py` 定义唯一 canonical 形状，
生产方各提供一个 serve-time 投影适配器，消费面只认一种结构：

```mermaid
flowchart LR
    subgraph producers["生产方（保留原生存储契约）"]
        R["reports 诊断编排<br/>diagnose_report_failure"]
        M["redmine IssueResult<br/>单号分析 / 晨报"]
    end
    subgraph adapters["serve-time 投影适配器"]
        AR["features/reports/<br/>diagnosis_read_model.py"]
        AM["features/redmine/<br/>diagnosis_read_model.py"]
    end
    CAN["foundation/diagnosis_read_model.py<br/>canonical 七节：subject / failure_identity / evidence /<br/>background / similar_cases / failure_cluster /<br/>conclusion + recommended_actions"]
    subgraph consumers["消费面（只认一种形状）"]
        WEB["Web UI"]
        CLI["gms-rt CLI"]
        MCP["gms_rt_* MCP"]
        AST["Assistant"]
    end
    R --> AR --> CAN
    M --> AM --> CAN
    CAN --> WEB
    CAN --> CLI
    CAN --> MCP
    CAN --> AST
```

关键语义：

- **读时投影，不落库**：canonical 模型是 serve 时刻的派生物，挂在响应
  负载的 `read_model` 键；持久层仍保存生产方原生结果，Web 继续用原生
  字段渲染完整展示面（`suggested_reply_*` 等 Redmine 沟通字段不进
  canonical 模型）。
- **证据分层不因投影改变**（ADR 0014）：内部案例与源码锚点进
  `evidence`，外部 Android 机制知识永远进 `background`——顶层
  `evidence_level` 是联邦层的 background 盖章（证据层级），锚点核验
  三态聚合在 `anchor_status`（path_matched / path_missing / unknown），
  不带根因语义。
- **Failure Cluster 保守化**：读取时刻没有聚合上下文，`failure_cluster`
  只暴露本条确定性指纹与显式已知成员；检索召回（SIMILAR_SYMPTOM 级）
  不得计入 members——合并判定是 relation judge 的职责。
- **conclusion 封闭词表**：`status` ∈ confirmed/likely/possible/unknown，
  未知值 fail-closed 归为 unknown；reports 的规则兜底结论最高只能标
  `possible`，不得冒充 AI 结论。
- **防御性归一化**：生产方数据来自 AI 输出与多路召回，归一化对非
  dict / 缺键 / 超长文本降级处理，投影异常不得阻断诊断主流程。

跨 feature 依赖方向合规：reports 适配器派生失败指纹经
`features.redmine` 公共面取 `failure_identity`（不触碰内部模块），
`foundation/` 不 import 任何 feature。

## 门禁一览

| 门禁 | 文件 | 防什么 |
| --- | --- | --- |
| Controller/Worker 精确边界 | `tests/architecture/test_controller_worker_boundary.py` | 文件级 allowlist 被内部蔓延；allowlist 僵尸条目 |
| Assistant 动态依赖 | `tests/architecture/test_assistant_dynamic_imports.py` | 字符串通道绕过跨 feature gate；死通道复辟 |
| 公共表面可解析 | `tests/contract/test_feature_public_surfaces.py` | 声明为 public ≠ 真正可 import |
| 错误出口契约 | `tests/architecture/test_api_error_semantics.py` | 异常文本泄漏；record_internal_error 旧用法回潮 |
| 语料契约 | `tests/quality/test_diagnosis_corpus_contract.py` | 语料本身非法导致质量门禁静默失效 |
| Ledger 状态机 | `features/redmine/tests/test_ai_execution_ledger.py` | 防重复发送/终态改写/租约僵尸语义回归 |
| 失败身份 | `features/redmine/tests/test_failure_identity.py` | 同名用例误合并；归一化过度/不足 |
| 统一读模型 | `foundation/test_diagnosis_read_model.py` + 两个适配器测试 | canonical 章节漂移；封闭词表被绕过；投影破坏原生负载 |
| Wiki golden query | `features/knowledge/tests/test_external_provider.py::GoldenQueryCorpusTests` | 知识召回质量漂移 |

## 相关文档

- [ADR-0013 Daily Brief batch matches single-issue diagnosis](adr/0013-daily-brief-batch-matches-single-issue-diagnosis.md)
  （诊断不设预算的原则边界）
- [ADR-0014 External knowledge federation](adr/0014-external-knowledge-federation.md)
  （证据分级 P0–P5、license 边界）
- [Redmine Daily Brief](../redmine-daily-brief.md)（编排全景）
- [架构总览](overview.md)
