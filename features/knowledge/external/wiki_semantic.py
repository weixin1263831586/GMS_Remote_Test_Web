"""Local semantic recall of approved Android Internals Wiki sections."""

from __future__ import annotations

import hashlib
import json

from features.retrieval import VectorIndex, local_embedding

from .base import KnowledgeHit
from .frontmatter import _scalarize
from .ranking import extract_source_anchors, rerank_hits


MAX_CORPUS = 30000
MAX_BATCH = 128


class WikiSemantic:
    def __init__(self, provider, path, *, embedder=None):
        self.provider = provider
        self.embedder = embedder if embedder is not None else local_embedding("android_internals")
        self.index = VectorIndex(path.parent / "android_internals_vectors.sqlite3")
        self.namespace = hashlib.sha256(str(path.resolve()).encode()).hexdigest()

    def _manifest(self, conn):
        policy = self.provider.load_policy()
        meta = dict(conn.execute("SELECT key,value FROM wiki_meta").fetchall())
        revision = meta.get("source_revision")
        state = json.loads(meta.get("policy_state") or "{}")
        if (policy.degraded or not revision or revision != meta.get("approved_revision")
                or state.get("content_hash") != policy.content_hash):
            raise ValueError("Wiki revision or policy requires approved reindex")
        rows = conn.execute("""SELECT s.rowid_alias,s.path,s.start_line,s.end_line,s.content_hash
            FROM wiki_sections s JOIN wiki_pages p ON p.path=s.path
            ORDER BY s.rowid_alias LIMIT ?""", (MAX_CORPUS + 1,)).fetchall()
        if len(rows) > MAX_CORPUS:
            raise ValueError("Wiki corpus exceeds semantic index limit")
        manifest = {}
        for row in rows:
            token = hashlib.sha256(json.dumps([revision, *list(row)[1:]],
                                              ensure_ascii=False).encode()).hexdigest()
            manifest[token] = row[0]
        return revision, manifest

    @staticmethod
    def _status(total, indexed):
        return {"status": "ready" if total and indexed == total else "partial" if indexed else "empty",
                "coverage_complete": bool(total) and indexed == total, "indexed": indexed, "total": total}

    def status(self):
        if self.embedder is None:
            return {"status": "disabled", "coverage_complete": False, "indexed": 0, "total": 0}
        if self.provider.validate():
            return {"status": "unavailable", "coverage_complete": False, "indexed": 0, "total": 0}
        with self.provider._connect() as conn:
            conn.execute("BEGIN")
            _, manifest = self._manifest(conn)
        indexed = self.index.indexed(self.namespace, self.embedder.identity, manifest)
        return self._status(len(manifest), len(indexed))

    @staticmethod
    def _row(conn, rowid):
        return conn.execute("""SELECT s.path,s.heading,s.start_line,s.end_line,
            f.content,p.title,p.chapter,p.status,p.frontmatter
            FROM wiki_sections s JOIN wiki_sections_fts f ON f.rowid=s.rowid_alias
            JOIN wiki_pages p ON p.path=s.path WHERE s.rowid_alias=?""", (rowid,)).fetchone()

    def sync(self, *, batch_size=MAX_BATCH):
        if self.embedder is None or self.provider.validate():
            raise ValueError("Local Wiki embedding or source not configured")
        with self.provider._connect() as conn:
            conn.execute("BEGIN")
            _, manifest = self._manifest(conn)
            indexed = self.index.indexed(self.namespace, self.embedder.identity, manifest)
            batch = {}
            missing = [token for token in manifest if token not in indexed]
            for token in missing[:max(1, min(batch_size, MAX_BATCH))]:
                row = self._row(conn, manifest[token])
                if row:
                    batch[token] = "\n".join((row["title"], row["heading"], row["content"]))
        self.index.upsert(self.namespace, self.embedder.identity, batch, self.embedder)
        # Obsolete tokens are never queried. Recheck the current manifest before pruning.
        with self.provider._connect() as conn:
            conn.execute("BEGIN")
            _, current = self._manifest(conn)
            if current == manifest:
                self.index.prune(self.namespace, self.embedder.identity, current)
        return self.status()

    def search(self, query, *, limit=10, android_api_level=None):
        status = self.status()
        if not status["indexed"]:
            return [], status
        with self.provider._connect() as conn:
            conn.execute("BEGIN")
            _, manifest = self._manifest(conn)
        ranked = self.index.search(self.namespace, self.embedder.identity, manifest,
                                   self.embedder.encode([query])[0], limit=min(50, max(10, limit * 3)))
        hits = []
        with self.provider._connect() as conn:
            conn.execute("BEGIN")
            revision, current = self._manifest(conn)
            for token, score in ranked:
                if token not in current:
                    continue
                row = self._row(conn, current[token])
                if not row:
                    continue
                fm = self.provider._load_frontmatter(row["frontmatter"])
                hits.append(KnowledgeHit(
                    source="android_internals", title=str(fm.get("title") or row["title"]),
                    snippet=row["content"][:6000], source_path=row["path"],
                    chapter=str(fm.get("chapter") or row["chapter"]), source_revision=revision,
                    applicable_versions=_scalarize(fm.get("applicable_versions")),
                    confidence=_scalarize(fm.get("confidence")), last_verified=_scalarize(fm.get("last_verified")),
                    last_verified_against=_scalarize(fm.get("last_verified_against")),
                    license=self.provider._license_hint(), score=max(0.0, score),
                    source_anchors=extract_source_anchors(fm.get("sources"), revision),
                    extra={"status": row["status"], "section": {"heading": row["heading"],
                           "start_line": row["start_line"], "end_line": row["end_line"]}},
                ))
        by_path = {}
        for hit in rerank_hits(hits, query, android_api_level=android_api_level):
            by_path.setdefault(hit.source_path, hit)
        return list(by_path.values())[:limit], self.status()
