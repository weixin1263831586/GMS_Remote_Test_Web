# Shared retrieval

`features/retrieval` is a domain-independent retrieval capability, not a second
business database. Providers retain authorization and source ownership. The
first dense adapter indexes enabled public CDD clauses only. Other feature
retrieval remains on its existing authorized paths.

Enable local semantic indexing through the local platform configuration:

```json
{
  "retrieval": {
    "enabled": true,
    "model_path": "/absolute/path/to/approved-local-embedding-model",
    "model_revision": "operator-pinned-model-revision"
  }
}
```

Install the optional `sentence-transformers` dependency in the Controller's
Python environment and provision the model locally before enabling. No
download, remote model code or external embedding API is used. Changing model
files requires a new model_revision.

An elevated administrator calls POST
`/api/knowledge/external/web-sources/{source_id}/semantic-index?batch_size=128`
to index one batch of the current CDD revision, and repeats until the returned
coverage is complete. GET on the same path reports index coverage. The
existing external search endpoint uses lexical plus dense RRF when the
semantic index is available; source status reports disabled, empty, partial
or error when it is not. No restricted Partner documents enter this index.

The derived SQLite index stores vectors and revision-bound citation tokens,
not document bodies. The provider validates current enabled sources and
versions before returning dense matches. Full-corpus recall is bounded.
This does not establish completeness of the upstream document.

Tests use deterministic synthetic encoders; real model quality and deployment
performance must be evaluated on the Controller.

## Owner-scoped Redmine case retrieval

Set `retrieval.enabled_sources` to `["cdd", "redmine_cases"]` to allow local
processing of historical case facts. The default remains public CDD only.
An authenticated human may use GET/POST
`/api/redmine-agent/search/semantic-index` to inspect or build a batch for
their own case repository. Existing similar-case search merges lexical,
semantic and business-feature rankings when vectors exist. Reset clears
derived vectors along with owner case data. Results are candidates, not
verified root causes.

An optional local cross encoder is configured with `reranker_path` and
`reranker_revision` in the same section; it ranks bounded case candidates
without network access. No GMS Partner documents, raw attachments or
live device data are included in these indexes.

### Android Internals Wiki

Add `android_internals` to `retrieval.enabled_sources` to opt in. Approve and reindex the Wiki using the existing workflow first. The external knowledge page provides a semantic-index action (also available through GET/POST `/api/knowledge/external/wiki-semantic-index`). Build bounded batches until coverage is complete. Semantic tokens are tied to the approved revision and section content; changed policy requires reindex. Existing lexical search remains available when semantic retrieval is disabled or unavailable.

### Personal knowledge

Add `personal_knowledge` to `retrieval.enabled_sources` to opt in. GET/POST
`/api/knowledge/semantic-index` is scoped to the current human owner. The
ordinary personal knowledge Q&A route fuses document FTS with semantic
chunk recall; original text and offsets are resolved from the current owner
repository. Restricted imports, including registered documents whose tags
change, are excluded before indexing and again before recall. Long documents
and total corpus size are bounded, and the returned coverage flag reflects
these bounds. Deleted or modified documents are not returned by stale vectors;
index synchronization removes obsolete derived rows.

### Diagnostic context

The project Agent includes bounded evidence provenance and source/coverage gaps
in its existing tool loop. Report diagnosis retrieves background candidates
before model inference and includes only current public CDD citations with
matching source anchors. Private historical cases, Wiki, SDK and Partner
material are not newly sent to model providers by this path. No candidate
establishes device applicability or a verified root cause.

### Archived Redmine replies

Add `redmine_journals` to `retrieval.enabled_sources` to opt in to local
embedding of archived reply notes. Explicitly private notes are excluded.
Authenticated human owners can inspect/build bounded batches with GET/POST
`/api/redmine-agent/search/journal-semantic-index` and search with POST
`/api/redmine-agent/search/journals` (JSON: query, limit).
Results cite issue, journal, revision and character offsets in the redacted
note (offset_basis=redacted_note). Redaction precedes chunking. Changed or removed replies cannot resolve from stale vectors,
and archive reset clears the derived namespace. The index covers only the
bounded local archive, not current remote access or complete Redmine history.
No new model-provider or Agent/MCP exposure is enabled by these endpoints.


### Existing diagnosis paths

The existing Redmine history search merges opt-in archived journal candidates
with lexical issue results through RRF, retaining only journal citation
coordinates (not reply text). The snapshot search retains its streaming
artifact/ZIP/description/journal search and reports scan coverage separately
from whole-snapshot source coverage. Its revision-bound candidates include
the snapshot, artifact or journal coordinate. Project Agent recognizes
batch-result pagination, missing sources and incomplete evidence and permits
one extra planning round within the existing tool budget. This is not proof
of a verified root cause or a guarantee that a missing source can be reached.


The evidence planner also preserves fixed SDK commit/file/line citations,
selected-repository limits, dynamic OpenGrok uncertainty and Gerrit pagination.
For exact `commit:<full SHA>` Gerrit queries the existing authorized dashboard
route requests the current patchset identity, without returning added commit
bodies. The evidence plan links only reproducible SDK evidence and the current
Gerrit patchset with the same project and full commit. It never infers device
integration from this link or treats an old patchset, a branch label or a
matching subject as proof.
Daily Brief prompts distinguish completed text scans from complete source
coverage and require independent verification of historical candidates and
patch applicability. Native Redmine MCP traces and controller reference preflight
use the shared evidence summarizer, including nested envelopes and grouped source
coverage. Official patch citations identify retained record revisions, not an
upstream Git commit. The Project Agent rejects repeated successful queries during
its bounded follow-up and retains unresolved evidence scope in answer metadata.
These are shared evidence rules, not a cross-source search service or unrestricted
access to protected documents.


### Restricted GMS policy lookup

The shared Partner scan has no verified per-account upstream entitlement and
cannot feed model context. Explicit GMS Requirements URLs, clause IDs and
GMS RAM certification questions select a human-only lookup in Project Agent.
The existing elevated-admin clauses endpoint accepts a source URL with its
fragment or `section:<keyword or clause ID>`. It returns the retained section,
related table rows, media references, scan revision and explicit limits.
Only the lookup query is persisted; the browser reads and displays the
protected result after authorization. No Partner grant or model route is
enabled, and no upstream URL is fetched by this lookup. Re-synchronize the
authorized scan to populate the section-anchor inventory for older indexes.

The Low RAM certification intent resolves the declaration section by its retained
anchor or GMS-10.0-001 clause, not broad title matches. Explicit preload questions
remain separate. The browser presents clause text and structured tables first;
section context and revision details are expandable. This is a human-readable
lookup, not an authorized model analysis of shared Partner content.

Owner-approved imported policy documents are bound to their content digest and
the ordered enabled model catalog, including the effective primary and fallback
routes, endpoint, model and protocol. The version-2 fingerprint invalidates
previous approvals; owners must explicitly authorize again. Credentials are not
included in the fingerprint. This does not establish upstream Partner entitlement.

### Clipboard images in Conversation Agent

Paste PNG, JPEG or WebP into the composer (at most three images, 4 MiB total).
The user can remove previews before sending; image-only messages are valid.
Image turns use the existing local model gateway with a configured, enabled
provider declaring `image_in` or `vision` in its capabilities. No external
provider fallback or operational tool execution is allowed. Images are validated,
stripped of metadata and retained as private session attachments for history
preview; the transcript contains attachment descriptors, not inline image bytes.
Text-only questions continue through the original Agent planning path.
Image turns with text additionally retrieve bounded public CDD background from
the local web index. The same revision-bound context policy is used by report
diagnosis. Image-only turns do not perform OCR-driven retrieval. The transcript
records candidate references and incomplete coverage, not retrieved text. No
private knowledge, Partner scan or operational tool is exposed by this path.

## Shared-layer invariants

Hybrid providers own their within-source ranking. Federation preserves that order,
compares ranks rather than unrelated scores, and retains revision and applicability
in evidence identity. Earlier unresolved retrieval gaps remain visible when a
later lookup succeeds. A vector model revision must maintain the same embedding
dimension across batches; changing dimensions requires a new revision.

CDD and Wiki batch synchronization use a shared namespace lock. Private corpus
locks retain their existing reset coordination. Personal semantic chunks redact
the entire document before slicing, with offsets relative to redacted text.
The token revision changes so old vectors are not recalled after this change;
rebuild the personal semantic index through its existing sync endpoint.

The existing external knowledge UI can build multiple bounded semantic batches
in one confirmed operation. It stops on completion, no progress, busy or error,
and can resume committed batches later. It is not a persistent background
scheduler; closing the page stops further client-side requests.

### Automatic CDD and approved Wiki index maintenance

Set `retrieval.auto_index_public_cdd` to the JSON boolean `true` in addition
to the existing local embedding configuration to enable bounded automatic
maintenance. The Controller's existing external-knowledge lifecycle tick
runs at most one batch (up to 128 vectors) by default, shared between CDD and
Wiki. Enable `retrieval.auto_index_approved_wiki` separately and allow
`android_internals` in `retrieval.enabled_sources`. The indexed, approved and
available Wiki revisions must match; the scheduler never approves or reindexes
an upstream revision. An attempt
timestamp is persisted before inference, so restarts and failures rotate to
other sources fairly and resume from committed vectors. Disabled or stale
sources are not embedded. The existing semantic-index status endpoints include
`auto_index_enabled` and `last_auto_index_attempt`; the knowledge source UI
shows this metadata without changing button geometry. Owner-scoped and restricted indexes are
not automatically scheduled by this option. The derived index still reflects
only the retained authorized manifest, not upstream coverage.

## Module boundary

The standalone retrieval module owns bounded fan-out, failure isolation and rank
fusion via `collect_ranked_sources`. Both the typed RetrievalService and the
external knowledge federation use that implementation. Domain adapters return
ordered RankedBatch objects and retain their authorization, source identity and
revision checks. The public feature exports are the only cross-feature entry;
architecture tests prohibit retrieval from importing any other business feature
or bootstrap. Do not move credentials, business persistence, model grants or
feature-specific query rules into retrieval.

Semantic adapters share VectorIndex.pending_tokens and coverage accounting.
The source owns its current authorized manifest, document resolution and reset
coordination. A complete vector manifest is not evidence of complete upstream
coverage. Semantic failure and omitted earlier observations remain visible to
the shared evidence planner.


### Paired evaluation

Use `RankingEvaluationCase` and `evaluate_ranking_comparison` with the same
historical queries, pinned corpus revision and human-labelled relevant
evidence for lexical-only and hybrid retrieval. Use
`evaluate_root_cause_comparison` with paired human review verdicts of
`correct`, `incorrect`, `abstained` or `unreviewed`. Unlabelled retrieval
cases and unreviewed root-cause pairs are counted separately; abstentions
remain in the root-cause accuracy denominator. These functions calculate
metrics from supplied labels, not model-generated ground truth. Do not
include the target issue's verified root cause or later replies in its query
or index snapshot. Run actual evaluations against the authorized local
Redmine archive; synthetic fixtures do not establish real quality gains.


## 本地 LLM 智能检索（Agentic RAG）

`IntelligentRAG` 是独立的只读编排器。模型先规划查询、改写检索词并选择授权文档，再主动读取命中原文或邻近段落，判断是否需要补证，生成逐条引用的陈述，最后通过单独的模型调用复核。程序验证原文引句、来源版本和访问权限；模型不得自行构造路径、网络地址或执行操作。复核不是独立人工认证，有限检索不能声称完整覆盖。

使用现有 AI provider 配置，不重复建设模型服务。可在 Controller 本地配置的 retrieval 段选择已经启用的本地 provider，例如：

```json
{
  "retrieval": {
    "llm_provider": "glm_local",
    "auto_understand_public_cdd": true,
    "llm_background_recall": true
  }
}
```

示例 provider 名称应替换为实际配置项。专用 LLM 路由仅接受 loopback、RFC1918 地址及 IPv6 ULA，禁止代理、重定向及远端 fallback。未指定 provider 时选择可用的本地路由。Embedding 仍沿用既有 enabled、enabled_sources 及模型目录配置，与生成式模型独立；没有向量索引也可通过文本检索和授权文档阅读进行智能问答。

- 个人知识问答：原有 `/api/knowledge/ask` 接入编排器；显式选择授权导入文档时校验当前本人身份、内容哈希及模型目录授权；通用 Agent 不可擅自扩大该范围。
- 公开 CDD 分析：原有 external search 使用 `analyze:` 前缀；后台召回开关用于单测诊断、Redmine 参考资料及图片文字查询，失败时保留原有授权检索。
- 公共 CDD 文档理解：自动任务每轮最多四条当前原文，使用本地模型提取有逐字引句支持的定义、条件、例外和术语；派生索引绑定原文版本及模型配置，缺失或无效输出可重试，派生结果只用于召回，不替代原文。语义索引状态接口分别返回文档理解覆盖情况。
- 单次完整问答最多五次模型调用、八次检索、六次主动阅读，限制证据数量和字符数。超预算、模型故障、授权变化或缺少可靠引句均不得伪造结论。
- 尚未实现所有私有语料的持久化 LLM 文档理解、图片专用视觉模型的版面理解及 SDK/Gerrit 全部资料的智能阅读适配。受限 Partner 扫描继续使用原有授权边界。
