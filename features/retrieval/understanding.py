"""Persistent, scope-bound LLM-derived retrieval hints for authorized passages."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone

from foundation.process_sync import release_sync_lock, try_acquire_sync_lock

from .intelligence import understand_passages
from .service import RetrievalHit

_SCHEMA = """
CREATE TABLE IF NOT EXISTS insights(
 token TEXT PRIMARY KEY, reference TEXT NOT NULL, revision TEXT NOT NULL,
 model_key TEXT NOT NULL, status TEXT NOT NULL, attempted_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""


def passage_token(hit):
    return hashlib.sha256(json.dumps([hit.source, hit.reference, hit.revision, hit.text],
                                     ensure_ascii=False).encode()).hexdigest()


class PassageInsightIndex:
    """Each instance is bound by its caller to one authorization scope."""

    def __init__(self, path, model_key):
        self.path = path
        self.model_key = model_key

    @contextmanager
    def _db(self, *, write=False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Scope files may contain private derived facts.
            self.path.touch(mode=0o600, exist_ok=True)
            self.path.chmod(0o600)
            conn = sqlite3.connect(self.path, timeout=5)
        else:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        with closing(conn), conn:
            conn.row_factory = sqlite3.Row
            if write:
                conn.executescript(_SCHEMA)
            yield conn

    def _states(self):
        if not self.path.is_file() or not self.model_key:
            return {}
        try:
            with self._db() as db:
                return {row["token"]: dict(row) for row in db.execute(
                    "SELECT * FROM insights WHERE model_key=?", (self.model_key,))}
        except sqlite3.Error:
            return {}

    def status(self, manifest, *, complete):
        states = self._states()
        processed = sum(states.get(token, {}).get("status") in {"ready", "no_supported_fact"}
                        for token in manifest)
        enriched = sum(states.get(token, {}).get("status") == "ready" for token in manifest)
        errors = sum(states.get(token, {}).get("status") == "error" for token in manifest)
        done = bool(manifest) and processed == len(manifest)
        return {"status": "disabled" if not self.model_key else "ready" if done and complete
                else "bounded" if done else "partial" if processed else "empty",
                "processed": processed, "total": len(manifest), "enriched": enriched, "errors": errors,
                "coverage_complete": done and complete, "corpus_bounded": not complete}

    def sync(self, manifest, current, model, *, complete, batch_size=4):
        if not self.model_key:
            return self.status(manifest, complete=complete)
        lock = try_acquire_sync_lock(self.path.with_suffix(".lock"))
        if lock is None:
            return {"status": "busy", "coverage_complete": False}
        try:
            states = self._states()
            now = datetime.now(timezone.utc)
            retry = (now - timedelta(minutes=10)).isoformat()
            pending = [(token, hit) for token, hit in manifest.items()
                       if token not in states or (states[token]["status"] == "error"
                                                   and states[token]["attempted_at"] < retry)]
            batch = [(token, hit) for token, hit in pending[:max(1, min(batch_size, 4))]
                     if current(hit)]
            if not batch:
                return self.status(manifest, complete=complete)
            try:
                result = understand_passages([{"id": token, "text": hit.text,
                    "context": str(hit.metadata.get("title") or "")} for token, hit in batch], model)
            except Exception:
                result = {}
            with self._db(write=True) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM insights WHERE model_key<>?", (self.model_key,))
                if complete:
                    for token in states.keys() - manifest.keys():
                        db.execute("DELETE FROM insights WHERE token=?", (token,))
                for token, hit in batch:
                    if not current(hit):
                        continue
                    derived = result.get(token)
                    state = "error" if derived is None else "ready" if derived["facts"] else "no_supported_fact"
                    db.execute("INSERT OR REPLACE INTO insights VALUES(?,?,?,?,?,?,?)",
                               (token, hit.reference, hit.revision, self.model_key, state,
                                now.isoformat(), json.dumps(derived or {}, ensure_ascii=False)))
            return {"batch_processed": len(batch), **self.status(manifest, complete=complete)}
        finally:
            release_sync_lock(lock)

    def search(self, terms, resolve, *, limit=8):
        """Return current raw hits, never generated statements as evidence."""
        terms = [term.casefold() for term in terms if term][:20]
        if not terms:
            return []
        ranked = []
        for token, row in self._states().items():
            if row["status"] != "ready":
                continue
            try:
                text = json.loads(row["payload"])["search_text"].casefold()
            except (ValueError, KeyError, AttributeError):
                continue
            score = sum(term in text for term in terms)
            if score:
                ranked.append((-score, token, row))
        hits = []
        for _, token, row in sorted(ranked)[:max(limit * 4, 12)]:
            hit = resolve(row["reference"], row["revision"])
            if isinstance(hit, RetrievalHit) and passage_token(hit) == token:
                hits.append(hit)
                if len(hits) >= limit:
                    break
        return hits
