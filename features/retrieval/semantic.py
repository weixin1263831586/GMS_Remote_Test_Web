"""Opt-in local embeddings and revision-bound derived vector storage."""

from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import sqlite3
import struct
import threading
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path

from foundation.config import config_manager
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock


MAX_CORPUS = 30000
MAX_BATCH = 128
MAX_DIMENSION = 4096
MAX_TEXT = 6000


class LocalEmbedding:
    """Use only an administrator-installed local model; no remote code or downloads."""

    def __init__(self, model_path: Path, revision: str):
        self.path = Path(model_path).expanduser().resolve(strict=True)
        if not self.path.is_dir() or not revision.strip():
            raise ValueError("Local model directory and revision are required")
        self.identity = hashlib.sha256(f"{self.path}\0{revision}".encode()).hexdigest()
        self._model = None
        self._lock = threading.Lock()

    def encode(self, texts):
        if len(texts) > MAX_BATCH:
            raise ValueError("Embedding batch exceeds limit")
        if not self._lock.acquire(timeout=5):
            raise TimeoutError("Local embedding model busy")
        try:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(str(self.path), device="cpu",
                                                  local_files_only=True, trust_remote_code=False)
            rows = self._model.encode([str(text)[:MAX_TEXT] for text in texts], batch_size=16,
                                      normalize_embeddings=True, show_progress_bar=False)
            return [list(map(float, row)) for row in rows]
        finally:
            self._lock.release()


@lru_cache(maxsize=1)
def _model(path, revision):
    return LocalEmbedding(Path(path), revision)


def _source_config(source):
    config = (config_manager.load_config() or {}).get("retrieval") or {}
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return {}
    allowed = config.get("enabled_sources", ["cdd"])
    if not isinstance(allowed, list) or source not in allowed:
        return {}
    return config


def embedding_configuration_issue(source="cdd"):
    """Return a safe configuration diagnostic without loading or downloading a model."""
    try:
        config = (config_manager.load_config() or {}).get("retrieval", {})
        if not isinstance(config, dict):
            return "invalid_config"
        enabled = config.get("enabled", False)
        if not isinstance(enabled, bool):
            return "invalid_config"
        if not enabled:
            return "disabled"
        allowed = config.get("enabled_sources", ["cdd"])
        if not isinstance(allowed, list):
            return "invalid_config"
        if source not in allowed:
            return "source_disabled"
        path = config.get("model_path")
        revision = config.get("model_revision")
        if not isinstance(path, str) or not path.strip() or not isinstance(revision, str) or not revision.strip():
            return "model_unconfigured"
        model_dir = Path(path).expanduser()
        if not model_dir.is_dir() or not os.access(model_dir, os.R_OK | os.X_OK):
            return "model_missing"
        if importlib.util.find_spec("sentence_transformers") is None:
            return "dependency_missing"
        return None
    except (OSError, ValueError, ImportError, RuntimeError, TypeError):
        return "configuration_error"


def local_embedding(source="cdd"):
    """Opt in to each corpus; private data is never enabled by the public CDD setting."""
    config = _source_config(source)
    if not config:
        return None
    path, revision = config.get("model_path"), config.get("model_revision")
    if not isinstance(path, str) or not path.strip() or not isinstance(revision, str) or not revision.strip():
        raise ValueError("Local embedding path and revision are required")
    return _model(path, revision)


class LocalReranker:
    """Optional local cross encoder for a small already-authorized candidate pool."""

    def __init__(self, path, revision):
        self.path = Path(path).expanduser().resolve(strict=True)
        if not self.path.is_dir() or not revision.strip():
            raise ValueError("Local reranker directory and revision are required")
        self._model = None
        self._lock = threading.Lock()

    def rank(self, query, documents, *, limit=30):
        keys = list(documents)[:max(1, min(limit, 50))]
        if not keys:
            return []
        if not self._lock.acquire(timeout=5):
            raise TimeoutError("Local reranker busy")
        try:
            if self._model is None:
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(str(self.path), device="cpu", local_files_only=True,
                                           trust_remote_code=False)
            scores = self._model.predict([(query[:MAX_TEXT], documents[key][:MAX_TEXT]) for key in keys],
                                         batch_size=8, show_progress_bar=False)
            if len(scores) != len(keys) or any(not math.isfinite(float(score)) for score in scores):
                raise ValueError("Invalid reranker output")
            return [key for key, _ in sorted(zip(keys, scores, strict=True),
                                             key=lambda item: -float(item[1]))]
        finally:
            self._lock.release()


@lru_cache(maxsize=1)
def _reranker(path, revision):
    return LocalReranker(path, revision)


def local_reranker(source="cdd"):
    config = _source_config(source)
    path, revision = config.get("reranker_path"), config.get("reranker_revision")
    if not path:
        return None
    if not isinstance(path, str) or not path.strip() or not isinstance(revision, str) or not revision.strip():
        raise ValueError("Local reranker path and revision are required")
    return _reranker(path, revision)


def run_scheduled_index_batches(jobs, attempts, mark_attempt, *, max_sources=1, batch_size=128):
    """Rotate bounded index jobs using caller-owned persistent attempt records.

    Each job supplies its current coverage and synchronization callbacks.
    The caller owns the process lock, authorization and durable transaction.
    A failed status or batch cannot prevent another source from being scheduled.
    """
    if max_sources < 1:
        return []
    outcomes = []
    for source_id, (inspect, sync) in sorted(jobs.items(), key=lambda item: (attempts.get(item[0], ""), item[0])):
        try:
            status = inspect()
        except Exception as exc:
            status = {"status": "error", "coverage_complete": False, "detail": type(exc).__name__}
        if status.get("coverage_complete") is True:
            continue
        mark_attempt(source_id)
        if status.get("status") not in {"disabled", "unavailable", "stale", "busy"}:
            try:
                status = sync(batch_size=max(1, min(batch_size, MAX_BATCH)))
            except Exception as exc:
                status = {"status": "error", "coverage_complete": False, "detail": type(exc).__name__}
        outcomes.append({**status, "source_id": source_id, "operation": "semantic_index"})
        if len(outcomes) >= min(max_sources, 4):
            break
    return outcomes


def _unit(vector):
    values = tuple(float(value) for value in vector)
    if not values or len(values) > MAX_DIMENSION or any(not math.isfinite(x) for x in values):
        raise ValueError("Invalid embedding")
    norm = math.sqrt(sum(x * x for x in values))
    if not norm or not math.isfinite(norm):
        raise ValueError("Invalid embedding norm")
    return tuple(x / norm for x in values)


def semantic_coverage(total: int, indexed: int, *, corpus_complete: bool = True) -> dict:
    """Coverage of the authorized manifest, not of the upstream source."""
    if not 0 <= indexed <= total:
        raise ValueError("Invalid semantic index counts")
    complete = bool(total) and indexed == total and corpus_complete
    return {"status": "ready" if complete else "partial" if indexed else "empty",
            "coverage_complete": complete, "indexed": indexed, "total": total}


class VectorIndex:
    """Store vectors only; callers supply the current authorized revision's manifest."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def sync_lock(self, namespace):
        """Serialize batch build and pruning per namespace across processes."""
        key = hashlib.sha256(namespace.encode()).hexdigest()
        descriptor = try_acquire_sync_lock(self.path.with_suffix("." + key + ".lock"))
        try:
            yield descriptor is not None
        finally:
            if descriptor is not None:
                release_sync_lock(descriptor)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Restrict newly created derived databases, including owner-scoped vectors.
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("""CREATE TABLE IF NOT EXISTS vectors (
            namespace TEXT NOT NULL, model TEXT NOT NULL, token TEXT NOT NULL,
            dimensions INTEGER NOT NULL, vector BLOB NOT NULL,
            PRIMARY KEY(namespace,model,token))""")
        return conn

    @staticmethod
    def _manifest(tokens):
        manifest = set(tokens)
        if len(manifest) > MAX_CORPUS:
            raise ValueError("Semantic corpus exceeds configured bound")
        return manifest

    def indexed(self, namespace, model, tokens):
        manifest = self._manifest(tokens)
        if not self.path.is_file():
            return set()
        with closing(self._connect()) as db:
            return {row[0] for row in db.execute(
                """SELECT token FROM vectors WHERE namespace=? AND model=?
                   AND dimensions BETWEEN 1 AND ? AND length(vector)=dimensions*4""",
                (namespace, model, MAX_DIMENSION)
            ) if row[0] in manifest}

    def coverage(self, namespace, model, tokens, *, corpus_complete=True):
        manifest = self._manifest(tokens)
        return semantic_coverage(len(manifest), len(self.indexed(namespace, model, manifest)),
                                 corpus_complete=corpus_complete)

    def pending_tokens(self, namespace, model, tokens):
        """Preserve manifest order while excluding valid committed vectors."""
        ordered = list(dict.fromkeys(tokens))
        indexed = self.indexed(namespace, model, ordered)
        return [token for token in ordered if token not in indexed]

    def upsert(self, namespace, model, documents, embedder):
        """Compute one bounded batch; publish atomically or leave previous vectors."""
        if len(documents) > MAX_BATCH:
            raise ValueError("Embedding batch exceeds limit")
        if not documents:
            return 0
        vectors = embedder.encode(list(documents.values()))
        if len(vectors) != len(documents):
            raise ValueError("Embedding count mismatch")
        rows = []
        dimensions = None
        for token, vector in zip(documents, vectors, strict=True):
            values = _unit(vector)
            if dimensions is not None and len(values) != dimensions:
                raise ValueError("Inconsistent embedding dimensions")
            dimensions = len(values)
            rows.append((namespace, model, token, len(values), struct.pack(f"<{len(values)}f", *values)))
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT DISTINCT dimensions FROM vectors WHERE namespace=? AND model=?",
                (namespace, model),
            ).fetchall()
            if any(row[0] != dimensions for row in existing):
                raise ValueError("Embedding dimensions changed; update model_revision")
            db.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?)", rows)
        return len(rows)

    def search(self, namespace, model, tokens, query_vector, *, limit=20):
        allowed = self._manifest(tokens)
        if not self.path.is_file() or not allowed:
            return []
        query = _unit(query_vector)
        ranked = []
        with closing(self._connect()) as db:
            for token, dimensions, blob in db.execute(
                "SELECT token,dimensions,vector FROM vectors WHERE namespace=? AND model=?",
                (namespace, model),
            ):
                if token not in allowed or dimensions != len(query) or len(blob) != dimensions * 4:
                    continue
                vector = struct.unpack(f"<{dimensions}f", blob)
                score = sum(a * b for a, b in zip(query, vector, strict=True))
                if math.isfinite(score):
                    ranked.append((token, score))
        return sorted(ranked, key=lambda item: (-item[1], item[0]))[:max(1, min(limit, 100))]

    def clear(self, namespace):
        if not self.path.is_file():
            return
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM vectors WHERE namespace=?", (namespace,))

    def prune(self, namespace, model, tokens):
        """Discard derived data outside the caller's current corpus and model."""
        allowed = self._manifest(tokens)
        if not self.path.is_file():
            return
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT model,token FROM vectors WHERE namespace=?", (namespace,)).fetchall()
            db.executemany("DELETE FROM vectors WHERE namespace=? AND model=? AND token=?",
                           [(namespace, old_model, token) for old_model, token in rows
                            if old_model != model or token not in allowed])
