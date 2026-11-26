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
