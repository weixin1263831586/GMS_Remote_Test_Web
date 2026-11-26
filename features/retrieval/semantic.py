"""Opt-in local embeddings and revision-bound derived vector storage."""

from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
import threading
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from foundation.config import config_manager


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


def local_embedding():
    """Return None unless the local configuration explicitly enables semantic retrieval."""
    config = (config_manager.load_config() or {}).get("retrieval") or {}
    if not config.get("enabled"):
        return None
    return _model(str(config["model_path"]), str(config["model_revision"]))


def _unit(vector):
    values = tuple(float(value) for value in vector)
    if not values or len(values) > MAX_DIMENSION or any(not math.isfinite(x) for x in values):
        raise ValueError("Invalid embedding")
    norm = math.sqrt(sum(x * x for x in values))
    if not norm or not math.isfinite(norm):
        raise ValueError("Invalid embedding norm")
    return tuple(x / norm for x in values)


class VectorIndex:
    """Store vectors only; callers supply the current authorized revision's manifest."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                "SELECT token FROM vectors WHERE namespace=? AND model=?", (namespace, model)
            ) if row[0] in manifest}

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
        for token, vector in zip(documents, vectors, strict=True):
            values = _unit(vector)
            rows.append((namespace, model, token, len(values), struct.pack(f"<{len(values)}f", *values)))
        with closing(self._connect()) as db, db:
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
