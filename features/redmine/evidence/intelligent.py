"""Owner-scoped, read-only snapshot passages for local intelligent retrieval."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from features.retrieval import IntelligentSource, RetrievalHit, RetrievalResult
from foundation.redaction import redact_sensitive_text

from ..users import owner_redmine_root

PAGE = 2400
MAX_ARTIFACTS = 24
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BYTES = 8 * 1024 * 1024


class SnapshotCorpus:
    """Pin a retained snapshot. No network fetch, repair, or evidence DB writes."""

    def __init__(self, owner_id, snapshot_id, *, include_journals=True, data_root=None):
        if not owner_id or not snapshot_id:
            raise ValueError("Owner and snapshot required")
        if data_root is None:
            self.root = owner_redmine_root(owner_id)
        else:
            from foundation.config_paths import sanitize_owner_id

            if sanitize_owner_id(owner_id) != owner_id:
                raise ValueError('Invalid evaluation owner identity')
            self.root = Path(data_root) / 'redmine' / 'by_user' / owner_id
        self.snapshot_id = snapshot_id
        self.include_journals = include_journals
        self.documents = {}
        self.bounded = False
        self.remaining_chars = MAX_BYTES
        self.revision, manifest, artifacts = self._load()
        self._add("issue", "工单标题及描述", str(manifest.get("subject") or "") + "\n"
                  + str(manifest.get("description") or ""))
        if include_journals:
            journals = manifest.get("journals") or []
            self.bounded |= len(journals) > 100
            for index, journal in enumerate(journals[:100]):
                if isinstance(journal, dict):
                    self._add(f"journal:{index}", f"历史回复 {index + 1}", str(journal.get("notes") or ""))
        remaining = MAX_BYTES
        self.bounded |= len(artifacts) > MAX_ARTIFACTS
        for row in artifacts[:MAX_ARTIFACTS]:
            relative = row["derived_text_path"] or (row["stored_path"] if row["kind"] in {"log", "text"} else "")
            if not relative or row["status"] != "ready":
                self.bounded = True
                continue
            base = (self.root / "evidence").resolve()
            file = (base / relative).resolve()
            if not file.is_relative_to(base) or not file.is_file() or remaining <= 0:
                self.bounded = True
                continue
            budget = min(remaining, MAX_FILE_BYTES)
            stamp = self._stamp(file)
            with file.open("rb") as handle:
                raw = handle.read(budget + 1)
            if self._stamp(file) != stamp:
                self.bounded = True
                continue
            self.bounded |= len(raw) > budget or row["derived_status"] in {"partial", "missing"}
            remaining -= min(len(raw), budget)
            self._add("artifact:" + row["artifact_id"], row["filename"], raw[:budget].decode("utf-8", "replace"),
                      file=file, stamp=stamp)

    @staticmethod
    def _stamp(file):
        stat = file.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _load(self):
        db = self.root / "evidence.sqlite3"
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM redmine_evidence_snapshots WHERE snapshot_id=?",
                               (self.snapshot_id,)).fetchone()
            if row is None or row["status"] != "ready" or not row["content_sha256"]:
                raise ValueError("Ready retained snapshot required")
            manifest = json.loads(row["manifest_json"])
            if not isinstance(manifest, dict):
                raise ValueError("Invalid snapshot manifest")
            artifacts = conn.execute("""SELECT artifact_id,filename,kind,status,derived_status,stored_path,
                derived_text_path,sha256 FROM redmine_evidence_artifacts WHERE snapshot_id=? ORDER BY artifact_id LIMIT ?""",
                (self.snapshot_id, MAX_ARTIFACTS + 1)).fetchall()
            revision = hashlib.sha256(json.dumps([row["content_sha256"], row["manifest_json"],
                [list(a) for a in artifacts]], ensure_ascii=False).encode()).hexdigest()
        return revision, manifest, artifacts

    def _add(self, key, title, body, **extra):
        # Redact whole bounded text before slicing to avoid splitting secrets.
        body = redact_sensitive_text(body)
        cap = min(MAX_FILE_BYTES, self.remaining_chars)
        self.bounded |= len(body) > cap
        body = body[:cap]
        self.remaining_chars -= len(body)
        if body.strip():
            self.documents[key] = {"title": redact_sensitive_text(str(title))[:200], "body": body, **extra}

    def _passage(self, key, start):
        doc = self.documents.get(key)
        if not doc or not 0 <= start < len(doc["body"]):
            return None
        return RetrievalHit("redmine_snapshot", f"{self.snapshot_id}:{key}:{start}", self.revision,
                            doc["body"][start:start + PAGE], "candidate",
                            {"document": key, "start": start, "title": doc["title"],
                             "snapshot_id": self.snapshot_id, "offset_basis": "redacted_retained_text",
                             "total_chars": len(doc["body"]), "bounded": self.bounded})

    def current(self, hit):
        try:
            if self._load()[0] != self.revision or hit.revision != self.revision:
                return False
            doc = self.documents[hit.metadata["document"]]
            if "file" in doc and self._stamp(doc["file"]) != doc["stamp"]:
                return False
            return self._passage(hit.metadata["document"], hit.metadata["start"]) == hit
        except (OSError, sqlite3.Error, ValueError, KeyError):
            return False

    def search(self, query, limit):
        terms = list(dict.fromkeys(re.findall(r"[\w.:-]{2,}", query.casefold())))[:16]
        candidates = []
        for key, doc in self.documents.items():
            # Bounded text scan; the LLM chooses terms and reads neighboring windows.
            text = doc["body"]
            for start in range(0, len(text), PAGE // 2):
                window = text[start:start + PAGE].casefold()
                score = sum(term in window for term in terms)
                if score:
                    candidates.append((-score, key, start))
        hits = [self._passage(key, start) for _, key, start in sorted(candidates)[:limit]]
        return RetrievalResult(hits, "matched" if hits else "no_match", False,
                               "bounded retained snapshot; not full attachment coverage")

    def read(self, hit, page):
        start = 0 if page == 0 else max(0, hit.metadata["start"] + page * PAGE)
        fresh = self._passage(hit.metadata["document"], start)
        return RetrievalResult([fresh] if fresh and self.current(fresh) else [], "matched" if fresh else "no_match")

    def source(self):
        def inventory():
            return [self._passage(key, 0) for key in list(self.documents)[:8]]

        return IntelligentSource("redmine_snapshot", "当前账号固定工单快照：描述、历史回复及可读取附件",
                                 self.search, self.current, self.read, inventory)
