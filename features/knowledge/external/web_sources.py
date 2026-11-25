"""Public website source configuration, revision storage and FTS5 retrieval."""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from foundation.config import settings

from .base import KnowledgeHit, ProviderStatus, SourceAnchor, search_terms
from .document_index import document_revision, parse_document
from .web_fetch import fetch_html, validate_url

SOURCE = "web_documents"
API_TO_ANDROID = {35: 15, 36: 16, 37: 17}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 android_version INTEGER, license TEXT NOT NULL, enabled INTEGER NOT NULL,
 generation TEXT NOT NULL, revision TEXT NOT NULL DEFAULT '', synced_at TEXT NOT NULL DEFAULT '',
 etag TEXT NOT NULL DEFAULT '', modified TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS partner_grants (
 owner_id TEXT PRIMARY KEY, approved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
 source_id TEXT NOT NULL, revision TEXT NOT NULL, fetched_at TEXT NOT NULL,
 PRIMARY KEY(source_id, revision)
);
CREATE VIRTUAL TABLE IF NOT EXISTS clauses USING fts5(
 source_id UNINDEXED, revision UNINDEXED, section, requirement_id, level UNINDEXED,
 condition, original, url UNINDEXED, token UNINDEXED, tokenize='unicode61'
);
"""


def index_path():
    return Path(settings.data_root) / "knowledge/external/web_documents.sqlite3"


class WebKnowledgeProvider:
    source_id = SOURCE

    def __init__(self, path: Path | None = None):
        self.path = path

    def _path(self):
        return self.path or index_path()

    @contextmanager
    def _db(self, *, write=False):
        path = self._path()
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=10)
        else:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        with closing(conn), conn:
            conn.row_factory = sqlite3.Row
            if write:
                conn.executescript(_SCHEMA)
            yield conn

    def set_partner_grant(self, owner_id: str, enabled: bool):
        if not owner_id:
            raise ValueError('Owner identity required')
        with self._db(write=True) as db:
            if enabled:
                db.execute('INSERT OR REPLACE INTO partner_grants VALUES (?,?)',
                           (owner_id, datetime.now(timezone.utc).isoformat()))
            else:
                db.execute('DELETE FROM partner_grants WHERE owner_id=?', (owner_id,))

    def has_partner_grant(self, owner_id: str):
        if not owner_id or not self._path().is_file():
            return False
        try:
            with self._db() as db:
                return db.execute('SELECT 1 FROM partner_grants WHERE owner_id=?', (owner_id,)).fetchone() is not None
        except sqlite3.Error:
            return False

    def list_sources(self):
        if not self._path().is_file():
            return []
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,name,url,kind,android_version,license,enabled,revision,synced_at,error "
                "FROM sources ORDER BY name,id")]

    def save(self, *, name, url, kind, android_version, license, enabled=True, source_id=None):
        name, license = name.strip(), license.strip()
        host = validate_url(url)
        if not name or len(name) > 160 or not license or len(license) > 240:
            raise ValueError("Name and content-use license are required")
        if kind not in {"cdd", "document"}:
            raise ValueError("Unsupported source type")
        if kind == "cdd" and (host != "source.android.com" or not android_version or
                url.split("?")[0] != f"https://source.android.com/docs/compatibility/{android_version}/android-{android_version}-cdd"):
            raise ValueError("CDD URL and Android version must agree")
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            if source_id:
                old = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
                if old is None:
                    raise ValueError("Source not found")
                if (old["url"], old["kind"], old["android_version"], old["license"]) != (
                        url, kind, android_version, license):
                    db.execute("DELETE FROM clauses WHERE source_id=?", (source_id,))
                    db.execute("DELETE FROM revisions WHERE source_id=?", (source_id,))
                    db.execute("UPDATE sources SET revision='',synced_at='',etag='',modified='',error='' WHERE id=?",
                               (source_id,))
                db.execute("UPDATE sources SET name=?,url=?,kind=?,android_version=?,license=?,enabled=?,generation=? WHERE id=?",
                           (name, url, kind, android_version, license, int(enabled), uuid.uuid4().hex, source_id))
            else:
                if db.execute("SELECT count(*) FROM sources").fetchone()[0] >= 64:
                    raise ValueError("Source limit reached")
                source_id = uuid.uuid4().hex
                db.execute("INSERT INTO sources(id,name,url,kind,android_version,license,enabled,generation) VALUES (?,?,?,?,?,?,?,?)",
                           (source_id, name, url, kind, android_version, license, int(enabled), uuid.uuid4().hex))
        return source_id

    def delete(self, source_id):
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM clauses WHERE source_id=?", (source_id,))
            db.execute("DELETE FROM revisions WHERE source_id=?", (source_id,))
            db.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def sync(self, source_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM sources WHERE id=? AND enabled=1", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Source not found or disabled")
        try:
            body, etag, modified = fetch_html(row["url"], etag=row["etag"], modified=row["modified"])
            revision = document_revision(body) if body is not None else row["revision"]
            if not revision:
                raise ValueError("No usable revision")
            clauses = parse_document(body, row["url"], row["kind"], row["android_version"]) if body is not None else []
            stamp = datetime.now(timezone.utc).isoformat()
            with self._db(write=True) as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT generation,revision FROM sources WHERE id=?", (source_id,)).fetchone()
                if current is None or current["generation"] != row["generation"] or current["revision"] != row["revision"]:
                    raise ValueError("Source changed during synchronization")
                exists = db.execute("SELECT 1 FROM revisions WHERE source_id=? AND revision=?",
                                    (source_id, revision)).fetchone()
                if not exists:
                    if body is None:
                        raise ValueError("Cached revision missing")
                    db.execute("INSERT INTO revisions VALUES (?,?,?)", (source_id, revision, stamp))
                    for section, req, level, condition, original, url in clauses:
                        token = "POL-" + hashlib.sha256(
                            "\0".join((source_id, revision, section, req, original)).encode()).hexdigest()[:24]
                        db.execute("INSERT INTO clauses VALUES (?,?,?,?,?,?,?,?,?)",
                                   (source_id, revision, section, req, level, condition, original, url, token))
                db.execute("UPDATE sources SET revision=?,synced_at=?,etag=?,modified=?,error='' WHERE id=?",
                           (revision, stamp, etag, modified, source_id))
            return {"source_id": source_id, "revision": revision, "status": "ready", "unchanged": body is None}
        except Exception as exc:
            with self._db(write=True) as db:
                db.execute("UPDATE sources SET error=? WHERE id=? AND generation=? AND revision=?",
                           (type(exc).__name__, source_id, row["generation"], row["revision"]))
            raise

    def status(self):
        enabled = [r for r in self.list_sources() if r["enabled"]]
        ready = [r for r in enabled if r["revision"]]
        status = ("disabled" if not enabled else "error" if any(r["error"] for r in enabled)
                  else "ready" if len(ready) == len(enabled) else "empty")
        return ProviderStatus(SOURCE, bool(enabled), status, detail=f"{len(ready)}/{len(enabled)} sources indexed",
                              doc_count=len(ready), license="per-source",
                              last_sync_at=max((r["synced_at"] for r in ready), default=""))

    def search(self, query, *, limit=5, android_api_level=None):
        if not self._path().is_file():
            return []
        match = ' OR '.join('"' + term.replace('"', '""') + '"' for term in search_terms(query))
        if not match:
            return []
        version = API_TO_ANDROID.get(android_api_level)
        if android_api_level is not None and version is None:
            version = -1
        with self._db() as db:
            rows = db.execute(
                """SELECT c.*,s.name,s.android_version,s.license,s.kind,s.synced_at,s.error,bm25(clauses) AS rank
                   FROM clauses c JOIN sources s ON c.source_id=s.id AND c.revision=s.revision
                   WHERE clauses MATCH ? AND s.enabled=1 AND
                     (? IS NULL OR s.android_version IS NULL OR s.android_version=?)
                   ORDER BY rank,c.token LIMIT ?""", (match, version, version, min(max(limit, 1), 20))).fetchall()
        return [KnowledgeHit(source=SOURCE, title=f"{r['name']} / {r['section']} {r['requirement_id']}".strip(),
                             snippet=r["original"][:6000], source_path=r["token"], chapter=r["section"],
                             source_revision=r["revision"], applicable_versions=str(r["android_version"] or "unspecified"),
                             confidence="candidate; applicability unverified", last_verified=r["synced_at"],
                             license=r["license"], score=-r["rank"],
                             source_anchors=[SourceAnchor(url=r["url"], revision=r["revision"],
                                                          evidence_type="official" if r["kind"] == "cdd" else "document")],
                             extra={"section": r["section"], "requirement_id": r["requirement_id"],
                                    "requirement_level": r["level"], "applicability_context": r["condition"][:4000],
                                    "reference": r["token"], "source_url": r["url"],
                                    "source_kind": r["kind"], "status": "stale" if r["error"] else "indexed"})
                for r in rows]


def partner_policy_allowed(owner_id: str):
    return WebKnowledgeProvider().has_partner_grant(owner_id)


def policy_search_terms(query: str):
    return search_terms(query, limit=12)


def recall_web_references(query: str, *, android_version=None):
    """Local candidates only; missing hits never establish compliance."""
    provider = WebKnowledgeProvider()
    try:
        selected = [r for r in provider.list_sources() if r["enabled"] and
                    (android_version is None or r["android_version"] in (None, android_version))]
        if not selected:
            return None
        api = next((k for k, v in API_TO_ANDROID.items() if v == android_version), None)
        if android_version is not None and api is None:
            return {"status": "unavailable", "coverage_complete": False, "items": [],
                    "reason": "Android version mapping not configured"}
        hits = provider.search(query[:512], limit=5, android_api_level=api)
        items = [{"reference": h.source_path, "source_url": h.extra["source_url"],
                  "source_revision": h.source_revision, "title": h.title,
                  "requirement_id": h.extra["requirement_id"], "requirement_level": h.extra["requirement_level"],
                  "applicability_context": h.extra["applicability_context"][:2000],
                  "original_text": h.snippet[:3000], "android_version": h.applicable_versions,
                  "source_kind": h.extra["source_kind"], "status": h.extra["status"],
                  "fetched_at": h.last_verified, "license": h.license} for h in hits]
        return {"status": "matched" if items else "unavailable" if any(not r["revision"] or r["error"] for r in selected) else "no_match",
                "coverage_complete": False, "items": items,
                "reason": "Ranked local candidates only; verify applicability and current revision"}
    except (OSError, sqlite3.Error, ValueError):
        return {"status": "unavailable", "coverage_complete": False, "items": [],
                "reason": "Web knowledge index unavailable"}
