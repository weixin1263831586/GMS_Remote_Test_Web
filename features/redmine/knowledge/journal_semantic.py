"""Owner-scoped, opt-in semantic recall of archived Redmine replies."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import dataclass

from features.retrieval import VectorIndex, local_embedding
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock
from foundation.redaction import redact_sensitive_text


MAX_ISSUES = 3000
MAX_CHUNKS = 12000
MAX_JOURNAL_JSON = 2000000
MAX_NOTE = 24000
CHUNK_SIZE = 1600
CHUNK_STEP = 1400


def journal_index_location(db_path):
    path = db_path.with_name(db_path.stem + ".journal-vectors.sqlite3")
    namespace = hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()
    return path, namespace


@dataclass(frozen=True)
class JournalChunk:
    issue_id: int
    journal_id: str
    created_on: str
    revision: str
    start: int
    end: int

    @property
    def token(self):
        return f"{self.issue_id}:{self.journal_id}:{self.revision}:{self.start}"


def _revision(journal_id, created_on, note):
    return hashlib.sha256(json.dumps(["redacted-v1", journal_id, created_on, note],
                                     ensure_ascii=False).encode()).hexdigest()


def _chunks(issue_id, journals):
    complete = True
    chunks = {}
    if not isinstance(journals, list):
        return chunks, False
    for journal in journals:
        if not isinstance(journal, dict):
            complete = False
            continue
        if journal.get("private_notes"):
            continue
        note = str(journal.get("notes") or "")
        if not note.strip():
            continue
        journal_id = str(journal.get("id") or "")
        if not journal_id.isdecimal():
            complete = False
            continue
        created_on = str(journal.get("created_on") or "")
        revision = _revision(journal_id, created_on, note)
        if len(note) > MAX_NOTE:
            complete = False
        # Redact the entire note before splitting; a credential can span a chunk boundary.
        note = redact_sensitive_text(note)
        if len(note) > MAX_NOTE:
            complete = False
        for start in range(0, min(len(note), MAX_NOTE), CHUNK_STEP):
            if len(chunks) >= MAX_CHUNKS:
                return chunks, False
            end = min(start + CHUNK_SIZE, len(note), MAX_NOTE)
            chunk = JournalChunk(issue_id, journal_id, created_on, revision, start, end)
            chunks[chunk.token] = chunk
            if end == min(len(note), MAX_NOTE):
                break
    return chunks, complete


class JournalSemantic:
    """The caller supplies the authenticated owner's existing issue repository."""

    def __init__(self, repository, *, embedder=None):
        self.repository = repository
        self.embedder = embedder if embedder is not None else local_embedding("redmine_journals")
        path, self.namespace = journal_index_location(repository.db_path)
        self.index = VectorIndex(path)

    def _manifest(self):
        manifest = {}
        complete = True
        with closing(self.repository.connect()) as conn:
            rows = conn.execute(
                """SELECT issue_id, CASE WHEN length(journals_json)<=? THEN journals_json END AS journals
                FROM redmine_agent_issues ORDER BY updated_on DESC, issue_id DESC LIMIT ?""",
                (MAX_JOURNAL_JSON, MAX_ISSUES + 1),
            )
            for ordinal, row in enumerate(rows):
                if ordinal >= MAX_ISSUES:
                    return manifest, False
                try:
                    journals = json.loads(row["journals"])
                except (TypeError, ValueError):
                    complete = False
                    continue
                chunks, issue_complete = _chunks(int(row["issue_id"]), journals)
                complete = complete and issue_complete
                remaining = MAX_CHUNKS - len(manifest)
                if len(chunks) > remaining:
                    manifest.update(list(chunks.items())[:remaining])
                    return manifest, False
                manifest.update(chunks)
        return manifest, complete

    def _status(self, manifest, complete):
        indexed = self.index.indexed(self.namespace, self.embedder.identity, manifest)
        covered = bool(manifest) and len(indexed) == len(manifest) and complete
        return {"status": "ready" if covered else "partial" if indexed else "empty",
                "indexed": len(indexed), "total": len(manifest),
                "coverage_complete": covered, "corpus_bounded": not complete,
                "scope": "owner_archived_non_private_journals"}

    def status(self):
        if self.embedder is None:
            return {"status": "disabled", "indexed": 0, "total": 0, "coverage_complete": False}
        return self._status(*self._manifest())

    def _resolve(self, chunk, cache):
        if chunk.issue_id not in cache:
            issue = self.repository.get_issue(chunk.issue_id)
            journals = (issue or {}).get("journals_json") or []
            current, _ = _chunks(chunk.issue_id, journals)
            notes = {str(journal.get("id")): str(journal.get("notes") or "")
                     for journal in journals if isinstance(journal, dict) and not journal.get("private_notes")}
            cache[chunk.issue_id] = current, notes
        current, notes = cache[chunk.issue_id]
        if current.get(chunk.token) != chunk:
            return None
        note = notes.get(chunk.journal_id, "")
        # Duplicate journal IDs in a damaged archive must not resolve to another reply.
        if _revision(chunk.journal_id, chunk.created_on, note) != chunk.revision:
            return None
        return redact_sensitive_text(note)[chunk.start:chunk.end]

    def sync(self, *, batch_size=128):
        if self.embedder is None:
            raise ValueError("Local journal embedding is not enabled")
        descriptor = try_acquire_sync_lock(self.index.path.with_suffix(".lock"))
        if descriptor is None:
            return {"status": "busy", "coverage_complete": False}
        try:
            manifest, _ = self._manifest()
            indexed = self.index.indexed(self.namespace, self.embedder.identity, manifest)
            documents = {}
            cache = {}
            for token, chunk in manifest.items():
                if token in indexed:
                    continue
                text = self._resolve(chunk, cache)
                if text is not None:
                    documents[token] = text
                if len(documents) >= max(1, min(batch_size, 128)):
                    break
            self.index.upsert(self.namespace, self.embedder.identity, documents, self.embedder)
            current, complete = self._manifest()
            self.index.prune(self.namespace, self.embedder.identity, current)
            return self._status(current, complete)
        finally:
            release_sync_lock(descriptor)

    def search(self, query, *, limit=10):
        if not query.strip() or len(query) > 8192 or not 1 <= limit <= 50:
            raise ValueError("Invalid journal retrieval query")
        if self.embedder is None:
            return self._response([], self.status())
        manifest, complete = self._manifest()
        status = self._status(manifest, complete)
        if not status["indexed"]:
            return self._response([], status)
        ranked = self.index.search(self.namespace, self.embedder.identity, manifest,
                                   self.embedder.encode([redact_sensitive_text(query)])[0],
                                   limit=min(limit * 4, 100))
        results = []
        cache = {}
        seen = set()
        for token, score in ranked:
            chunk = manifest[token]
            key = (chunk.issue_id, chunk.journal_id)
            if score <= 0 or key in seen:
                continue
            text = self._resolve(chunk, cache)
            if text is None:
                continue
            seen.add(key)
            results.append({"source": "redmine_journals", "reference": token,
                            "source_revision": chunk.revision, "issue_id": chunk.issue_id,
                            "journal_id": chunk.journal_id, "created_on": chunk.created_on,
                            "start_char": chunk.start, "end_char": chunk.end,
                            "offset_basis": "redacted_note",
                            "snippet": text, "evidence_level": "candidate",
                            "retrieval_channels": ["semantic"]})
            if len(results) >= limit:
                break
        return self._response(results, status)

    @staticmethod
    def _response(results, status):
        # A complete local index does not establish current remote Redmine access or coverage.
        return {"results": results, "retrieval": status, "coverage_complete": False,
                "scope": "owner_archive_only", "latest_confirmed": False}
