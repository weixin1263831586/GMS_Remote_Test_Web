"""Explicitly enabled, owner-scoped semantic index of historical case facts."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing

from features.retrieval import VectorIndex, local_embedding, local_reranker
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock
from foundation.redaction import redact_sensitive_text


FIELDS = ("subject", "problem_summary", "error_signature", "root_cause", "solution",
          "android_version", "chip_platform", "module", "certification_type")
MAX_CORPUS = 30000
MAX_BATCH = 128


def _text(row):
    return "\n".join(f"{key}: {redact_sensitive_text(str(row[key] or ''))[:3000]}" for key in FIELDS)


def _token(row):
    digest = hashlib.sha256(json.dumps([row[key] for key in FIELDS],
                                       ensure_ascii=False, default=str).encode()).hexdigest()
    return f"{row['issue_id']}:{digest}"


class CaseSemantic:
    """The repository is already bound to an authenticated resource owner."""

    def __init__(self, db, *, embedder=None):
        self.db = db
        self.embedder = embedder if embedder is not None else local_embedding("redmine_cases")
        self.index = VectorIndex(db.db_path.with_name(db.db_path.stem + ".case-vectors.sqlite3"))
        self.namespace = hashlib.sha256(str(db.db_path.resolve()).encode()).hexdigest()

    def _manifest(self):
        with closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT issue_id," + ",".join(FIELDS) +
                                " FROM redmine_case_facts ORDER BY issue_id LIMIT ?", (MAX_CORPUS + 1,)).fetchall()
        if len(rows) > MAX_CORPUS:
            raise ValueError("Case corpus exceeds semantic index bound")
        return {_token(row): int(row["issue_id"]) for row in rows}

    def _status(self, manifest):
        indexed = self.index.indexed(self.namespace, self.embedder.identity, manifest)
        return {"status": "ready" if manifest and len(indexed) == len(manifest) else
                "partial" if indexed else "empty", "indexed": len(indexed), "total": len(manifest),
                "coverage_complete": bool(manifest) and len(indexed) == len(manifest)}

    def status(self):
        if self.embedder is None:
            return {"status": "disabled", "indexed": 0, "total": 0, "coverage_complete": False}
        return self._status(self._manifest())

    def sync(self, *, batch_size=MAX_BATCH):
        if self.embedder is None:
            raise ValueError("Local case embedding not enabled")
        descriptor = try_acquire_sync_lock(self.index.path.with_suffix(".lock"))
        if descriptor is None:
            return {"status": "busy", "coverage_complete": False}
        try:
            manifest = self._manifest()
            indexed = self.index.indexed(self.namespace, self.embedder.identity, manifest)
            missing = [token for token in manifest if token not in indexed][:max(1, min(batch_size, MAX_BATCH))]
            facts = self.db.get_case_facts_for_issue_ids([manifest[token] for token in missing])
            documents = {}
            for token in missing:
                row = facts.get(manifest[token])
                if row and _token(row) == token:
                    documents[token] = _text(row)
            self.index.upsert(self.namespace, self.embedder.identity, documents, self.embedder)
            current = self._manifest()
            self.index.prune(self.namespace, self.embedder.identity, current)
            return self._status(current)
        finally:
            release_sync_lock(descriptor)

    def search(self, query, *, limit):
        if self.embedder is None or not self.index.path.is_file():
            return []
        manifest = self._manifest()
        if not self.index.indexed(self.namespace, self.embedder.identity, manifest):
            return []
        ranked = self.index.search(self.namespace, self.embedder.identity, manifest,
                                   self.embedder.encode([redact_sensitive_text(query)])[0], limit=limit)
        facts = self.db.get_case_facts_for_issue_ids([manifest[token] for token, _ in ranked])
        # Hash current contents again; changed/deleted cases cannot be returned from stale vectors.
        return [(facts[manifest[token]], score) for token, score in ranked
                if score > 0 and manifest[token] in facts and _token(facts[manifest[token]]) == token]


def rerank_case_candidates(query, facts):
    model = local_reranker("redmine_cases")
    if model is None:
        return []
    return model.rank(redact_sensitive_text(query), {int(row["issue_id"]): _text(row) for row in facts})
