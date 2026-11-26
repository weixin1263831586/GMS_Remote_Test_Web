"""Owner-scoped semantic recall of ordinary personal documents."""

from __future__ import annotations

import hashlib
import json

from features.retrieval import VectorIndex, fuse_rankings, local_embedding
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock
from foundation.redaction import redact_sensitive_text

from .policy_imports import policy_import_filter
from .storage import search_terms


MAX_DOCS = 500
MAX_CHUNKS = 12000
MAX_DOC_CHARS = 240000
CHUNK_SIZE = 1200
CHUNK_STEP = 1000


def _body(doc):
    return str(doc.get("raw_content") or doc.get("content_md") or doc.get("summary") or "")


def _redacted_body(doc):
    return redact_sensitive_text(_body(doc))


def _revision(doc):
    return hashlib.sha256(json.dumps(["redacted-v1", doc.get("title"), doc.get("space_id"), _body(doc)],
                                     ensure_ascii=False).encode()).hexdigest()


def _chunk_token(doc_id, revision, start):
    return f"{doc_id}:{revision}:{start}"


class PersonalSemantic:
    """The authenticated owner is supplied by the existing personal knowledge boundary."""

    def __init__(self, store, owner, *, embedder=None):
        if not owner:
            raise ValueError("Personal index requires an owner")
        self.store = store
        self.owner = owner
        self.embedder = embedder if embedder is not None else local_embedding("personal_knowledge")
        self.namespace = hashlib.sha256((str(store.db_path.resolve()) + "\0" + owner).encode()).hexdigest()
        self.index = VectorIndex(store.db_path.with_name(store.db_path.stem + ".personal-vectors.sqlite3"))

    def _manifest(self, exclude, *, space_id=""):
        docs = self.store.list_docs(self.owner, space_id=space_id, limit=MAX_DOCS + 1)
        complete = len(docs) <= MAX_DOCS
        manifest = {}
        for doc in docs[:MAX_DOCS]:
            if exclude(doc):
                continue
            full = _redacted_body(doc)
            if len(_body(doc)) > MAX_DOC_CHARS or len(full) > MAX_DOC_CHARS:
                complete = False
            revision = _revision(doc)
            for start in range(0, min(len(full), MAX_DOC_CHARS), CHUNK_STEP):
                if len(manifest) >= MAX_CHUNKS:
                    return manifest, False
                token = _chunk_token(doc["doc_id"], revision, start)
                manifest[token] = (doc["doc_id"], revision, start)
        return manifest, complete

    def _status(self, manifest, complete):
        indexed = self.index.indexed(self.namespace, self.embedder.identity, manifest)
        covered = bool(manifest) and len(indexed) == len(manifest) and complete
        return {"status": "ready" if covered else "partial" if indexed else "empty",
                "indexed": len(indexed), "total": len(manifest), "coverage_complete": covered,
                "corpus_bounded": not complete, "scope": "owner_ordinary_documents"}

    def status(self):
        if self.embedder is None:
            return {"status": "disabled", "indexed": 0, "total": 0, "coverage_complete": False}
        return self._status(*self._manifest(policy_import_filter(self.owner)))

    def sync(self, *, batch_size=128):
        if self.embedder is None:
            raise ValueError("Personal embedding is not enabled")
        descriptor = try_acquire_sync_lock(self.index.path.with_suffix("." + self.namespace + ".lock"))
        if descriptor is None:
            return {"status": "busy", "coverage_complete": False}
        try:
            exclude = policy_import_filter(self.owner)
            manifest, _ = self._manifest(exclude)
            indexed = self.index.indexed(self.namespace, self.embedder.identity, manifest)
            documents = {}
            cache = {}
            for token, (doc_id, revision, start) in manifest.items():
                if token in indexed:
                    continue
                if doc_id not in cache:
                    cache[doc_id] = self.store.get_doc(self.owner, doc_id)
                doc = cache[doc_id]
                if doc and not exclude(doc) and _revision(doc) == revision:
                    # Redact before slicing; credentials may cross chunk boundaries.
                    text = redact_sensitive_text(str(doc.get("title") or "")) + "\n"
                    documents[token] = text + _redacted_body(doc)[start:start + CHUNK_SIZE]
                if len(documents) >= max(1, min(batch_size, 128)):
                    break
            self.index.upsert(self.namespace, self.embedder.identity, documents, self.embedder)
            current, complete = self._manifest(policy_import_filter(self.owner))
            self.index.prune(self.namespace, self.embedder.identity, current)
            return self._status(current, complete)
        finally:
            release_sync_lock(descriptor)

    def search(self, query, *, space_id="", limit=12):
        if self.embedder is None:
            return [], self.status()
        exclude = policy_import_filter(self.owner)
        manifest, complete = self._manifest(exclude, space_id=space_id)
        status = self._status(manifest, complete)
        if not status["indexed"]:
            return [], status
        ranked = self.index.search(self.namespace, self.embedder.identity, manifest,
                                   self.embedder.encode([redact_sensitive_text(query)])[0],
                                   limit=min(limit * 4, 100))
        results = []
        seen = set()
        # Reload the current owner document and its policy classification after recall.
        exclude = policy_import_filter(self.owner)
        for token, score in ranked:
            doc_id, revision, start = manifest[token]
            if score <= 0 or doc_id in seen:
                continue
            doc = self.store.get_doc(self.owner, doc_id)
            if not doc or exclude(doc) or _revision(doc) != revision or (space_id and doc["space_id"] != space_id):
                continue
            seen.add(doc_id)
            snippet = _redacted_body(doc)[start:start + CHUNK_SIZE]
            results.append({"doc_id": doc_id, "title": doc["title"], "space_id": doc["space_id"],
                            "summary": doc.get("summary") or "", "snippet": snippet,
                            "matched_terms": [term for term in search_terms(query) if term.lower() in snippet.lower()],
                            "tags": doc.get("tags") or [], "updated_at": doc.get("updated_at") or "",
                            "source_revision": revision, "reference": token,
                            "start_char": start, "end_char": start + len(snippet),
                            "offset_basis": "redacted_document",
                            "retrieval_channels": ["semantic"]})
            if len(results) >= limit:
                break
        return results, status


def retrieve_personal_contexts(store, owner, query, *, space_id="", limit=8, exclude_document):
    lexical = store.retrieve_contexts(owner, query, space_id=space_id, limit=limit,
                                      exclude_document=exclude_document)
    try:
        dense, status = PersonalSemantic(store, owner).search(query, space_id=space_id, limit=limit)
    except Exception as exc:
        dense = []
        status = {"status": "error", "coverage_complete": False, "detail": type(exc).__name__}
    pool = {item["doc_id"]: item for item in dense}
    for item in lexical:
        identity = item["doc_id"]
        if identity in pool:
            pool[identity]["retrieval_channels"].append("lexical")
        else:
            pool[identity] = {**item, "retrieval_channels": ["lexical"]}
    ordered = fuse_rankings([[item["doc_id"] for item in lexical], [item["doc_id"] for item in dense]],
                             limit=limit)
    # Fresh access check also protects lexical candidates against changes between search and use.
    exclude = policy_import_filter(owner)
    contexts = []
    for key in ordered:
        doc = store.get_doc(owner, key)
        if (not doc or exclude(doc) or exclude_document(doc)
                or (space_id and doc["space_id"] != space_id)):
            continue
        hit = pool[key]
        revision = _revision(doc)
        if hit.get("source_revision") and hit["source_revision"] != revision:
            continue
        body = _redacted_body(doc) if hit.get("offset_basis") == "redacted_document" else _body(doc)
        if hit["snippet"] not in body:
            continue
        contexts.append({**hit, "source_revision": revision,
                         "reference": hit.get("reference") or f"{key}:{revision}"})
    return contexts, status
