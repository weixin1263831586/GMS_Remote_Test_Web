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
Daily Brief prompts distinguish completed text scans from complete source
coverage and require independent verification of historical candidates and
patch applicability. These are shared evidence rules, not a cross-source
search service or unrestricted access to protected documents.


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

### Clipboard images in Conversation Agent

Paste PNG, JPEG or WebP into the composer (at most three images, 4 MiB total).
The user can remove previews before sending; image-only messages are valid.
Image turns use the existing local model gateway with a configured, enabled
provider declaring `capabilities: ["vision"]`. No external provider fallback
or operational tool execution is allowed. Raw images are validated, stripped
of metadata and used only for the current analysis; the persisted transcript
contains the text, image count and resulting answer, not the image bytes.
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

### Automatic public CDD index maintenance

Set `retrieval.auto_index_public_cdd` to the JSON boolean `true` in addition
to the existing local embedding configuration to enable bounded automatic
maintenance. The Controller\x27s existing external-knowledge lifecycle tick
runs at most one public CDD batch (up to 128 vectors) by default. An attempt
timestamp is persisted before inference, so restarts and failures rotate to
other sources fairly and resume from committed vectors. Disabled, stale or
non-CDD sources are not scheduled. Owner-scoped and restricted indexes are
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
