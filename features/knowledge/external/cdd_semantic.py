"""Local semantic recall of current, enabled public CDD clauses only."""

from __future__ import annotations

import hashlib

from features.retrieval import VectorIndex, local_embedding, semantic_coverage


MAX_CORPUS = 30000
MAX_BATCH = 128


class CddSemantic:
    def __init__(self, provider, *, embedder=None):
        self.provider = provider
        self.embedder = embedder if embedder is not None else local_embedding()
        path = provider._path()
        self.index = VectorIndex(path.parent / "cdd_vectors.sqlite3")
        self.namespace = hashlib.sha256(str(path.resolve()).encode()).hexdigest()

    def _sources(self, source_id=None, version=None):
        return [row for row in self.provider.list_sources()
                if row["enabled"] and row["kind"] == "cdd" and row["revision"]
                and (source_id is None or row["id"] == source_id)
                and (version is None or row["android_version"] == version)]

    def _rows(self, source, tokens):
        if not tokens:
            return []
        placeholders = ",".join("?" for _ in tokens)
        with self.provider._db() as db:
            return [dict(row) for row in db.execute(
                """SELECT c.token,c.section,c.requirement_id,c.condition,c.original
                   FROM clauses c JOIN sources s ON c.source_id=s.id AND c.revision=s.revision
                   WHERE s.id=? AND s.enabled=1 AND s.kind='cdd' AND s.revision=?
                   AND c.token IN (""" + placeholders + ") ORDER BY c.token",
                (source["id"], source["revision"], *tokens)
            )]

    def _scope(self, source):
        return self.namespace + ":" + source["id"]

    def _manifest(self, source):
        with self.provider._db() as db:
            rows = db.execute("""SELECT c.token FROM clauses c JOIN sources s
                ON c.source_id=s.id AND c.revision=s.revision
                WHERE s.id=? AND s.enabled=1 AND s.kind='cdd' AND s.revision=?
                ORDER BY c.token LIMIT ?""", (source["id"], source["revision"], MAX_CORPUS + 1)).fetchall()
        tokens = [row[0] for row in rows]
        if len(tokens) > MAX_CORPUS:
            raise ValueError("CDD corpus exceeds semantic index limit")
        return tokens

    def status(self, source_id=None):
        if self.embedder is None:
            return {"status": "disabled", "coverage_complete": False, "indexed": 0, "total": 0}
        total = indexed = 0
        for source in self._sources(source_id):
            tokens = self._manifest(source)
            total += len(tokens)
            indexed += len(self.index.indexed(self._scope(source), self.embedder.identity, tokens))
        return semantic_coverage(total, indexed)

    def sync(self, source_id, *, batch_size=MAX_BATCH):
        if self.embedder is None:
            raise ValueError("Local embedding model not configured")
        sources = self._sources(source_id)
        if len(sources) != 1:
            raise ValueError("Enabled public CDD source required")
        source = sources[0]
        with self.index.sync_lock(self._scope(source)) as acquired:
            if not acquired:
                return {"status": "busy", "coverage_complete": False}
            current = self._sources(source_id)
            if not current:
                return {"status": "unavailable", "coverage_complete": False}
            return self._sync_current(current[0], batch_size=batch_size)

    def _sync_current(self, source, *, batch_size):
        source_id = source["id"]
        tokens = self._manifest(source)
        scope, model = self._scope(source), self.embedder.identity
        missing = self.index.pending_tokens(scope, model, tokens)[:max(1, min(batch_size, MAX_BATCH))]
        batch = {}
        for row in self._rows(source, missing):
            batch[row["token"]] = "\n".join(row[key] for key in
                                          ("section", "requirement_id", "condition", "original"))
        self.index.upsert(scope, model, batch, self.embedder)
        # Never purge a newer revision's derived data after a concurrent source update.
        current = self._sources(source_id)
        if current and current[0]["revision"] == source["revision"]:
            self.index.prune(scope, model, tokens)
        return self.status(source_id)

    def search(self, query, *, version=None, limit=20):
        if self.embedder is None:
            return [], self.status()
        sources = self._sources(version=version)
        manifest = [(source, self._manifest(source)) for source in sources]
        total = sum(len(tokens) for _, tokens in manifest)
        if total > MAX_CORPUS:
            raise ValueError("Combined CDD corpus exceeds semantic search limit")
        indexed = sum(len(self.index.indexed(self._scope(source), self.embedder.identity, tokens))
                      for source, tokens in manifest)
        status = semantic_coverage(total, indexed)
        if not indexed:
            return [], status
        query_vector = self.embedder.encode([query])[0]
        candidates = []
        for source, tokens in manifest:
            candidates.extend(self.index.search(self._scope(source), self.embedder.identity, tokens,
                                                query_vector, limit=limit))
        ranked = sorted(candidates, key=lambda row: (-row[1], row[0]))[:limit]
        hits = []
        # Recheck source enablement and current revision after vector search.
        with self.provider._db() as db:
            for token, score in ranked:
                row = db.execute("""SELECT c.*,s.name,s.android_version,s.license,s.kind,s.synced_at,s.error
                    FROM clauses c JOIN sources s ON c.source_id=s.id AND c.revision=s.revision
                    WHERE c.token=? AND s.enabled=1 AND s.kind='cdd'
                    AND (? IS NULL OR s.android_version=?)""", (token, version, version)).fetchone()
                if row:
                    hits.append((dict(row), score))
        return hits, status
