"""Public website source configuration, revision storage and FTS5 retrieval."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from features.retrieval import fuse_rankings
from foundation.config import config_manager, settings
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock

from .base import KnowledgeHit, ProviderStatus, SourceAnchor, search_terms
from .document_index import document_revision, document_title, parse_document
from .web_fetch import fetch_html, validate_url


SOURCE = "web_documents"
API_TO_ANDROID = {35: 15, 36: 16, 37: 17}
MAX_REVISIONS_PER_SOURCE = 8

#: 排序用词表桥接：CDD 用 "memory available to the kernel and userspace" 表达
#: 内存门槛，用户和模型更常说 RAM、内存。桥接只影响**章内排序**，不扩大
#: FTS 匹配集——没命中的条款不会凭空出现。
_SPEC_SYNONYMS = {
    "ram": ("memory",),
    "内存": ("memory",),
    "运存": ("memory",),
    "storage": ("non-volatile",),
    "存储": ("non-volatile",),
}
#: 条款强度：带 MUST 的规范要求优先于 SHOULD，再优先于说明性文字。
_STRENGTH_RULES = (
    (re.compile(r"\bMUST NOT\b|\bMUST\b|\bSHALL\b|\bREQUIRED\b"), 3),
    (re.compile(r"\bSTRONGLY RECOMMENDED\b|\bSHOULD\b|\bRECOMMENDED\b"), 2),
)
_INDEXED_COVERAGE_TTL = 60.0
_INDEXED_COVERAGE_CACHE: dict[str, tuple[float, dict[str, int]]] = {}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 android_version INTEGER, license TEXT NOT NULL, enabled INTEGER NOT NULL,
 generation TEXT NOT NULL, revision TEXT NOT NULL DEFAULT '', synced_at TEXT NOT NULL DEFAULT '',
 etag TEXT NOT NULL DEFAULT '', modified TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS partner_model_grants (
 owner_id TEXT PRIMARY KEY, model_catalog_hash TEXT NOT NULL, approved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS owner_import_grants (
 owner_id TEXT NOT NULL, doc_id TEXT NOT NULL, content_hash TEXT NOT NULL,
 model_catalog_hash TEXT NOT NULL, approved_at TEXT NOT NULL,
 PRIMARY KEY(owner_id, doc_id)
);
CREATE TABLE IF NOT EXISTS revisions (
 source_id TEXT NOT NULL, revision TEXT NOT NULL, fetched_at TEXT NOT NULL,
 PRIMARY KEY(source_id, revision)
);
CREATE TABLE IF NOT EXISTS sync_attempts (
 source_id TEXT PRIMARY KEY, attempted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS semantic_sync_attempts (
 source_id TEXT PRIMARY KEY, attempted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cdd_insights (
 id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, source_id TEXT NOT NULL,
 revision TEXT NOT NULL, model_key TEXT NOT NULL, status TEXT NOT NULL,
 attempted_at TEXT NOT NULL, facts_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cdd_insights_source ON cdd_insights(source_id,revision,model_key);
CREATE VIRTUAL TABLE IF NOT EXISTS cdd_insight_fts USING fts5(search_text, tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS clauses USING fts5(
 source_id UNINDEXED, revision UNINDEXED, section, requirement_id, level UNINDEXED,
 condition, original, url UNINDEXED, token UNINDEXED, tokenize='unicode61'
);
"""


def model_catalog_fingerprint(config=None):
    """Bind owner approval to the complete enabled model routing catalog."""
    if config is None:
        config = config_manager.get_ai_config()
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return ""
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return ""
    routes = []
    for name, provider in providers.items():
        if not isinstance(name, str) or not isinstance(provider, dict):
            return ""
        enabled = provider.get("enabled", False)
        if not isinstance(enabled, bool):
            return ""
        if not enabled:
            continue
        model, base_url = provider.get("model"), provider.get("base_url")
        if not isinstance(model, str) or not model.strip() or not isinstance(base_url, str) or not base_url.strip():
            return ""
        try:
            parsed = urlsplit(base_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                return ""
        except ValueError:
            return ""
        api_format = provider.get("api_format") or (
            "anthropic" if "anthropic" in base_url.lower() or base_url.endswith("/messages") else "openai"
        )
        if api_format not in {"anthropic", "openai"}:
            return ""
        routes.append({"name": name, "model": model, "base_url": base_url,
                       "api_format": api_format, "auth_required": provider.get("auth_required", True)})
    if not routes:
        return ""
    primary = config.get("primary_provider")
    if not isinstance(primary, str) and primary is not None:
        return ""
    names = [route["name"] for route in routes]
    primary = primary if primary in names else names[0]
    ordered = [route for route in routes if route["name"] == primary]
    ordered.extend(route for route in routes if route["name"] != primary)
    # Do not store credentials; a route/catalog change invalidates prior approval.
    payload = {"version": 2, "routes": ordered}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


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
                self._migrate_citations(conn)
            yield conn

    @staticmethod
    def _migrate_citations(conn):
        if conn.execute('PRAGMA user_version').fetchone()[0] >= 1:
            return
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('PRAGMA user_version').fetchone()[0] >= 1:
                return
            collisions = conn.execute(
                'SELECT token FROM clauses GROUP BY token HAVING count(*) > 1'
            ).fetchall()
            for (token,) in collisions:
                rows = conn.execute(
                    'SELECT rowid,source_id,revision,section,requirement_id,condition,original,url '
                    'FROM clauses WHERE token=?', (token,),
                ).fetchall()
                if len({tuple(row)[1:] for row in rows}) > 1:
                    conn.executemany('UPDATE clauses SET token=? WHERE rowid=?', [
                        (_clause_token(*tuple(row)[1:]), row['rowid']) for row in rows
                    ])
            conn.execute('PRAGMA user_version=1')

    def revoke_partner_grant(self, owner_id):
        if self._path().is_file():
            with self._db(write=True) as db:
                db.execute('DELETE FROM partner_model_grants WHERE owner_id=?', (owner_id,))

    def registered_imports(self, owner_id):
        if not self._path().is_file():
            return set()
        with self._db() as db:
            return {row['doc_id'] for row in db.execute(
                'SELECT doc_id FROM owner_import_grants WHERE owner_id=?', (owner_id,),
            )}

    def approve_import(self, owner_id, doc_id, content_hash, *, model_catalog_hash=None):
        fingerprint = model_catalog_fingerprint()
        if model_catalog_hash is not None and fingerprint != model_catalog_hash:
            raise ValueError('The model catalog changed during import')
        if not owner_id or not doc_id or not fingerprint:
            raise ValueError('Owner, document and configured model catalog required')
        with self._db(write=True) as db:
            db.execute('INSERT OR REPLACE INTO owner_import_grants VALUES (?,?,?,?,?)',
                       (owner_id, doc_id, content_hash, fingerprint, datetime.now(timezone.utc).isoformat()))

    def approved_imports(self, owner_id, *, model_catalog_hash=None):
        if not owner_id or not self._path().is_file():
            return {}
        try:
            fingerprint = model_catalog_fingerprint()
            if model_catalog_hash is not None and fingerprint != model_catalog_hash:
                return {}
            if not fingerprint:
                return {}
            with self._db() as db:
                return {row['doc_id']: row['content_hash'] for row in db.execute(
                    'SELECT doc_id,content_hash FROM owner_import_grants WHERE owner_id=? AND model_catalog_hash=?',
                    (owner_id, fingerprint),
                )}
        except (sqlite3.Error, OSError, ValueError, TypeError):
            return {}

    def save_url(self, *, url, name='', enabled=None, source_id=None, authorized_for_ai_processing=False):
        """Infer public document metadata; unknown sources require explicit content-use approval."""
        url, name = url.strip(), name.strip()
        host = validate_url(url)
        path = urlsplit(url).path
        match = re.fullmatch(r'/docs/compatibility/(\d+)/android-\1-cdd', path) if host == 'source.android.com' else None
        version = int(match.group(1)) if match else None
        kind = 'cdd' if version else 'document'
        old = None
        if source_id:
            old = next((row for row in self.list_sources() if row['id'] == source_id), None)
            if old is None:
                raise ValueError('Source not found')
            if old['url'] != url:
                raise ValueError('Add a new source when changing the URL')
        if old:
            # Preserve the recorded license and source classification on a rename or toggle.
            license = old['license']
            kind, version = old['kind'], old['android_version']
            if enabled is None:
                enabled = bool(old['enabled'])
            if not name:
                name = old['name']
        elif host == 'source.android.com' and path.startswith('/docs/'):
            license = 'CC-BY-4.0 (AOSP documentation, unless otherwise noted)'
        elif authorized_for_ai_processing:
            license = 'Administrator-approved content use; license not independently verified'
        else:
            raise ValueError('Unknown source requires content-use approval')
        return self.save(name=name, url=url, kind=kind, android_version=version,
                         license=license, enabled=True if enabled is None else enabled, source_id=source_id)

    def list_sources(self):
        if not self._path().is_file():
            return []
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT s.id,s.name,s.url,s.kind,s.android_version,s.license,s.enabled,s.revision,s.synced_at,s.error, "
                "(SELECT count(*) FROM clauses c WHERE c.source_id=s.id AND c.revision=s.revision) AS indexed_count "
                "FROM sources s ORDER BY s.name,s.id")]

    def save(self, *, name, url, kind, android_version, license, enabled=True, source_id=None):
        name, license = name.strip(), license.strip()
        host = validate_url(url)
        if len(name) > 160 or not license or len(license) > 240:
            raise ValueError("Invalid name or content-use license")
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
            db.execute("DELETE FROM sync_attempts WHERE source_id=?", (source_id,))
            db.execute("DELETE FROM semantic_sync_attempts WHERE source_id=?", (source_id,))
            db.execute("DELETE FROM cdd_insight_fts WHERE rowid IN (SELECT id FROM cdd_insights WHERE source_id=?)",
                       (source_id,))
            db.execute("DELETE FROM cdd_insights WHERE source_id=?", (source_id,))
            db.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def sync(self, source_id):
        if not re.fullmatch(r'[0-9a-f]{32}', source_id):
            raise ValueError('Invalid source identity')
        lock_path = self._path().parent / 'sync-locks' / (source_id + '.lock')
        descriptor = try_acquire_sync_lock(lock_path)
        if descriptor is None:
            return {'source_id': source_id, 'status': 'in_progress', 'coalesced': True}
        try:
            return self._sync(source_id)
        finally:
            release_sync_lock(descriptor)

    def _sync(self, source_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM sources WHERE id=? AND enabled=1", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Source not found or disabled")
        with self._db(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO sync_attempts SELECT id,? FROM sources WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), source_id),
            )
        try:
            body, etag, modified = fetch_html(row["url"], etag=row["etag"], modified=row["modified"])
            revision = document_revision(body) if body is not None else row["revision"]
            if not revision:
                raise ValueError("No usable revision")
            clauses = parse_document(body, row["url"], row["kind"], row["android_version"]) if body is not None else []
            title = (document_title(body) or urlsplit(row['url']).hostname or row['url'])[:160] if body is not None and not row['name'] else ''
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
                        token = _clause_token(source_id, revision, section, req, condition, original, url)
                        db.execute("INSERT INTO clauses VALUES (?,?,?,?,?,?,?,?,?)",
                                   (source_id, revision, section, req, level, condition, original, url, token))
                db.execute("UPDATE sources SET name=CASE WHEN name='' AND ?<>'' THEN ? ELSE name END, "
                           "revision=?,synced_at=?,etag=?,modified=?,error='' WHERE id=?",
                           (title, title, revision, stamp, etag, modified, source_id))
                old_revisions = db.execute(
                    "SELECT revision FROM revisions WHERE source_id=? AND revision<>? "
                    "ORDER BY fetched_at DESC, revision LIMIT -1 OFFSET ?",
                    (source_id, revision, MAX_REVISIONS_PER_SOURCE - 1),
                ).fetchall()
                db.executemany("DELETE FROM clauses WHERE source_id=? AND revision=?",
                               [(source_id, item['revision']) for item in old_revisions])
                db.executemany("DELETE FROM revisions WHERE source_id=? AND revision=?",
                               [(source_id, item['revision']) for item in old_revisions])
            return {"source_id": source_id, "revision": revision, "status": "ready", "unchanged": body is None}
        except Exception as exc:
            with self._db(write=True) as db:
                db.execute("UPDATE sources SET error=? WHERE id=? AND generation=? AND revision=?",
                           (type(exc).__name__, source_id, row["generation"], row["revision"]))
            raise

    def sync_due(self, *, now=None, max_sources=4):
        """Refresh enabled public sources at most daily; failures retry on the next tick."""
        now = now or datetime.now(timezone.utc)
        outcomes = []
        sources = self.list_sources()
        if not sources or max_sources < 1:
            return outcomes
        with self._db(write=True) as db:
            attempts = dict(db.execute("SELECT source_id,attempted_at FROM sync_attempts").fetchall())
        for source in sorted(sources, key=lambda row: (attempts.get(row["id"], ""), row["id"])):
            if not source['enabled']:
                continue
            stamp = source['synced_at']
            if stamp and not source['error']:
                try:
                    if now - datetime.fromisoformat(stamp) < timedelta(hours=24):
                        continue
                except ValueError:
                    pass
            try:
                outcomes.append(self.sync(source['id']))
            except (OSError, ValueError, sqlite3.Error):
                outcomes.append({'source_id': source['id'], 'status': 'error'})
            if len(outcomes) >= max_sources:
                break
        return outcomes

    def sync_due_semantic(self, *, now=None, max_sources=1, batch_size=128):
        """Resume explicitly enabled CDD and approved Wiki indexes on controller ticks."""
        from features.retrieval import embedding_configuration_issue, run_scheduled_index_batches

        config = (config_manager.load_config() or {}).get("retrieval")
        if not isinstance(config, dict) or max_sources < 1:
            return []
        jobs = {}
        if config.get("auto_index_public_cdd") is True and embedding_configuration_issue() is None:
            for row in self.list_sources():
                if row["enabled"] and row["kind"] == "cdd" and row["revision"] and not row["error"]:
                    source_id = row["id"]
                    jobs[source_id] = (
                        lambda source_id=source_id: self.semantic_status(source_id),
                        lambda source_id=source_id, **kwargs: self.sync_semantic(source_id, **kwargs),
                    )
        if (config.get("auto_index_approved_wiki") is True
                and embedding_configuration_issue("android_internals") is None):
            from .federation import federated_service

            wiki = federated_service().provider("android_internals")
            if wiki is not None:
                def inspect_wiki():
                    state = wiki.status()
                    if (state.status != "ready" or not state.source_revision
                            or state.source_revision != state.approved_revision
                            or state.available_revision != state.approved_revision):
                        return {"status": "stale", "coverage_complete": False}
                    return wiki.semantic_status()

                jobs["android_internals"] = (inspect_wiki, wiki.sync_semantic)
        if not jobs:
            return []
        descriptor = try_acquire_sync_lock(self._path().parent / "sync-locks" / "semantic-scheduler.lock")
        if descriptor is None:
            return []
        try:
            now = now or datetime.now(timezone.utc)
            with self._db(write=True) as db:
                attempts = dict(db.execute("SELECT source_id,attempted_at FROM semantic_sync_attempts").fetchall())

            def mark_attempt(source_id):
                # Persist before inference; committed vectors are the resume cursor.
                with self._db(write=True) as db:
                    db.execute("INSERT OR REPLACE INTO semantic_sync_attempts VALUES (?,?)",
                               (source_id, now.isoformat()))

            return run_scheduled_index_batches(jobs, attempts, mark_attempt,
                                               max_sources=max_sources, batch_size=batch_size)
        finally:
            release_sync_lock(descriptor)

    def semantic_schedule_status(self, source_id, *, wiki=False):
        """Read scheduling metadata without creating a database or loading models."""
        config = (config_manager.load_config() or {}).get("retrieval")
        flag = "auto_index_approved_wiki" if wiki else "auto_index_public_cdd"
        enabled = isinstance(config, dict) and config.get(flag) is True
        attempted_at = ""
        if self._path().is_file():
            try:
                with self._db() as db:
                    row = db.execute("SELECT attempted_at FROM semantic_sync_attempts WHERE source_id=?",
                                     (source_id,)).fetchone()
                    attempted_at = row[0] if row else ""
            except (sqlite3.Error, OSError):
                pass
        return {"auto_index_enabled": enabled, "last_auto_index_attempt": attempted_at}

    def read_requirement(self, token):
        """Read the full retained clause and its original revision, never a new revision."""
        if not re.fullmatch(r'POL-[0-9a-f]{24}', token) or not self._path().is_file():
            return None
        with self._db() as db:
            rows = db.execute(
                """SELECT DISTINCT c.*,s.name,s.android_version,s.kind,s.license,s.error,
                          s.revision AS current_revision,r.fetched_at
                   FROM clauses c JOIN sources s ON c.source_id=s.id
                   JOIN revisions r ON c.source_id=r.source_id AND c.revision=r.revision
                   WHERE c.token=? AND s.enabled=1 LIMIT 2""", (token,),
            ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def read_cdd_scope(self, token, *, mode, offset=0, limit=3, android_api_level=None, include_documents=False):
        """Read a current section; approved document readers may also select Web records."""
        if mode not in {'context', 'section'} or not 0 <= offset <= 30000:
            raise ValueError('Invalid scope or offset')
        limit = min(max(limit, 1), 3)
        result = {'scope': mode, 'reference': token, 'offset': offset, 'total': 0,
                  'next_offset': None, 'coverage_complete': False, 'status': 'unavailable',
                  'results': []}
        if not re.fullmatch(r'POL-[0-9a-f]{24}', token) or not self._path().is_file():
            return result
        version = API_TO_ANDROID.get(android_api_level, -1) if android_api_level is not None else None
        with self._db() as db:
            db.execute('BEGIN')
            anchors = db.execute(
                """SELECT DISTINCT c.*,s.android_version,s.error FROM clauses c JOIN sources s
                   ON c.source_id=s.id AND c.revision=s.revision
                   WHERE c.token=? AND s.enabled=1 AND (?=1 OR s.kind='cdd')
                   AND (? IS NULL OR s.android_version=?) LIMIT 2""", (token, int(include_documents), version, version)
            ).fetchall()
            if len(anchors) != 1:
                return result
            anchor = anchors[0]
            result.update(section=anchor['section'], source_revision=anchor['revision'],
                          android_version=anchor['android_version'], source_url=anchor['url'])
            if anchor['error']:
                result['status'] = 'stale'
                return result
            where = 'c.source_id=? AND c.revision=? AND c.section=? AND c.url=?'
            params = [anchor['source_id'], anchor['revision'], anchor['section'], anchor['url']]
            if mode == 'context':
                # Textual condition association is not proof of device applicability.
                where += ' AND c.token<>? AND instr(c.condition,?)>0'
                params.extend([token, anchor['original']])
            total = db.execute(f'SELECT count(*) FROM clauses c WHERE {where}', params).fetchone()[0]
            rows = db.execute(
                f"""SELECT c.*,s.name,s.android_version,s.kind,s.license,s.error,s.synced_at
                    FROM clauses c JOIN sources s ON c.source_id=s.id AND c.revision=s.revision
                    WHERE {where} ORDER BY c.rowid LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        next_offset = offset + len(rows) if rows and offset + len(rows) < total else None
        result.update(status='matched' if rows else 'no_match', total=total, next_offset=next_offset,
                      page_complete=next_offset is None,
                      results=[{
                          'source': SOURCE, 'reference': row['token'], 'source_revision': row['revision'],
                          'requirement_id': row['requirement_id'], 'requirement_level': row['level'],
                          'original': row['original'], 'condition': row['condition'],
                          'source_anchors': [{'url': row['url'], 'revision': row['revision'],
                                              'evidence_type': 'official'}],
                          'evidence_level': 'background',
                      } for row in rows])
        return result

    def compare_requirements(self, section, requirement_id, *, limit=10, android_api_level=None):
        """Compare current CDD revisions by exact section and clause, never bare ID."""
        if not self._path().is_file():
            return []
        version = API_TO_ANDROID.get(android_api_level, -1) if android_api_level is not None else None
        with self._db() as db:
            rows = db.execute(
                """SELECT c.*,s.name,s.android_version,s.kind,s.license,s.error,r.fetched_at
                   FROM clauses c JOIN sources s ON c.source_id=s.id AND c.revision=s.revision
                   JOIN revisions r ON c.source_id=r.source_id AND c.revision=r.revision
                   WHERE s.enabled=1 AND s.kind='cdd' AND c.section=? AND c.requirement_id=?
                   AND (? IS NULL OR s.android_version=?)
                   ORDER BY s.android_version,c.token LIMIT ?""", (section, requirement_id, version, version, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self):
        enabled = [r for r in self.list_sources() if r["enabled"]]
        ready = [r for r in enabled if r["revision"]]
        status = ("disabled" if not enabled else "error" if any(r["error"] for r in enabled)
                  else "ready" if len(ready) == len(enabled) else "empty")
        detail = f"{len(ready)}/{len(enabled)} sources indexed"
        coverage = self.indexed_coverage()
        if coverage:
            # 只报文档数会被读成“索引里只有 4 段内容”，进而误判覆盖不全。
            detail += f", {coverage['clauses']} clauses in {coverage['sections']} sections"
        return ProviderStatus(SOURCE, bool(enabled), status, detail=detail,
                              doc_count=len(ready), license="per-source",
                              last_sync_at=max((r["synced_at"] for r in ready), default=""))

    def indexed_coverage(self) -> dict[str, int]:
        """当前 revision 的条款数与章节数，供状态说明与 Agent 判断覆盖范围。"""
        if not self._path().is_file():
            return {}
        key = str(self._path())
        now = time.monotonic()
        cached = _INDEXED_COVERAGE_CACHE.get(key)
        if cached and now - cached[0] < _INDEXED_COVERAGE_TTL:
            return cached[1]
        try:
            with self._db() as db:
                row = db.execute(
                    """SELECT count(*) AS clauses, count(DISTINCT c.section) AS sections
                       FROM clauses c JOIN sources s ON c.source_id=s.id AND c.revision=s.revision
                       WHERE s.enabled=1"""
                ).fetchone()
        except (sqlite3.Error, OSError):
            return {}
        coverage = {"clauses": int(row["clauses"] or 0), "sections": int(row["sections"] or 0)}
        _INDEXED_COVERAGE_CACHE[key] = (now, coverage)
        return coverage

    def semantic_status(self, source_id=None):
        from .cdd_semantic import CddSemantic

        try:
            return CddSemantic(self).status(source_id)
        except Exception as exc:
            return {"status": "error", "coverage_complete": False, "detail": type(exc).__name__}

    def sync_semantic(self, source_id, *, batch_size=128):
        from .cdd_semantic import CddSemantic

        return CddSemantic(self).sync(source_id, batch_size=batch_size)

    def search(self, query, *, limit=5, android_api_level=None):
        hits, _ = self.search_with_status(query, limit=limit, android_api_level=android_api_level)
        return hits

    def search_with_status(self, query, *, limit=5, android_api_level=None, public_cdd_only=False):
        from . import cdd_intelligence
        from .cdd_semantic import CddSemantic

        limit = min(max(limit, 1), 20)
        lexical = self._lexical_search(query, limit=limit, android_api_level=android_api_level,
                                       public_cdd_only=public_cdd_only)
        version = API_TO_ANDROID.get(android_api_level)
        if android_api_level is not None and version is None:
            version = -1
        try:
            rows, status = CddSemantic(self).search(query, version=version, limit=max(limit, 20),
                                                    fresh_only=public_cdd_only)
        except Exception as exc:
            rows = []
            status = {"status": "error", "coverage_complete": False, "detail": type(exc).__name__}
        try:
            insights = [_clause_hit(row) for row in cdd_intelligence.search(self, query, version=version, limit=limit)]
        except Exception:
            insights = []
        dense = [_clause_hit(row, score=score) for row, score in rows]
        if not dense and not insights:
            return lexical, status
        if insights:
            status = {**status, "document_understanding_hits": len(insights)}
        pool = {hit.source_path: hit for hit in [*insights, *dense, *lexical]}
        exact = [key for key, hit in pool.items()
                 if (identifier := str(hit.extra.get("requirement_id") or "")) and
                 re.search(r"(?<![\w.-])" + re.escape(identifier) + r"(?![\w.-])", query, re.I)]
        keys = fuse_rankings([[hit.source_path for hit in lexical], [hit.source_path for hit in dense],
                              [hit.source_path for hit in insights]], limit=limit, exact_keys=exact)
        for rank, key in enumerate(keys):
            pool[key].score = -float(rank)
        return [pool[key] for key in keys], status

    def _lexical_search(self, query, *, limit=5, android_api_level=None, public_cdd_only=False):
        if not self._path().is_file():
            return []
        terms = _match_terms(search_terms(query))
        match = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
        if not match:
            return []
        version = API_TO_ANDROID.get(android_api_level)
        if android_api_level is not None and version is None:
            version = -1
        limit = min(max(limit, 1), 20)
        ranking_terms = _ranking_terms(terms)
        with self._db() as db:
            candidates = db.execute(
                """SELECT c.rowid AS rowid,c.source_id,c.revision,c.section,bm25(clauses) AS rank
                   FROM clauses c JOIN sources s ON c.source_id=s.id AND c.revision=s.revision
                   WHERE clauses MATCH ? AND s.enabled=1 AND
                     (? IS NULL OR s.android_version IS NULL OR s.android_version=?)
                     AND (?=0 OR (s.kind='cdd' AND s.error=''))
                   ORDER BY rank,c.token LIMIT ?""",
                (match, version, version, int(public_cdd_only), max(limit * 20, 60))).fetchall()
            # 命中按章节成组：表格/列表往往只有条件行或说明行命中，单独返回
            # 它们回答不了问题（问内存门槛却只拿到"声明 ram.low"这类标志位）。
            # 章节优先序沿用 bm25 的最佳名次，章内再按「MUST/SHOULD 强度 →
            # 原文顺序」补齐，让最相关的一章先填满配额，剩余名额才给下一章。
            sections: list[tuple[str, str, str]] = []
            for row in candidates:
                key = (row["source_id"], row["revision"], row["section"])
                if key not in sections:
                    sections.append(key)
            ranked_sections: list[list] = []
            for source_id, revision, section in sections[:max(2, limit)]:
                rows = db.execute(
                    """SELECT c.rowid AS rowid,c.*,s.name,s.android_version,s.license,s.kind,
                              s.synced_at,s.error
                       FROM clauses c JOIN sources s ON c.source_id=s.id AND c.revision=s.revision
                       WHERE c.source_id=? AND c.revision=? AND c.section=? ORDER BY c.rowid""",
                    (source_id, revision, section)).fetchall()
                ranked_sections.append(sorted(rows, key=lambda item: _strength_order(item, ranking_terms)))
        ordered: list = []
        for ranked in ranked_sections:
            if len(ordered) >= limit:
                break
            ordered.extend(ranked[:limit - len(ordered)])
        return [_clause_hit(dict(row), score=-float(position))
                for position, row in enumerate(ordered[:limit])]


def _match_terms(terms: list[str]) -> list[str]:
    """FTS 匹配词：丢掉 "14"/"16" 这类短版本号。

    版本文本由 ``android_api_level`` 精确过滤，再让纯数字参与 OR 匹配只会
    让 "3.8.14. Multi-windows" 这类含数字的章节标题凭空进结果集。命中集因
    此只会变精确，不会引入新条目。
    """
    filtered = [term for term in terms if not (term.isdigit() and len(term) <= 2)]
    return filtered or terms


def _ranking_terms(terms) -> tuple[str, ...]:
    """把检索词与其规范同义词合并，供章内排序使用（不影响 FTS 匹配集）。"""
    synonyms = [synonym for term in terms for synonym in _SPEC_SYNONYMS.get(term, ())]
    return tuple(dict.fromkeys([*terms, *synonyms]))


def _strength_order(row, terms) -> tuple[int, int, int]:
    """章内排序：先答得上问题的条款，再按 MUST/SHOULD 强度，最后原文顺序。

    覆盖度只算条款原文：condition 里是「Handheld device implementations:」
    这类模板前缀，计入会让每条条款都“沾边”。长度不足 3 的词（to、of…）
    也不计入，否则虚词会把无关条款拉到同一档。
    """
    text = row["original"].casefold()
    coverage = sum(1 for term in terms if len(term) >= 3 and term.casefold() in text)
    strength = max((weight for pattern, weight in _STRENGTH_RULES if pattern.search(row["original"])), default=0)
    return (-coverage, -strength, row["rowid"])


def _clause_hit(row, *, score=0.0):
    return KnowledgeHit(
        source=SOURCE, title=f"{row['name']} / {row['section']} {row['requirement_id']}".strip(),
        snippet=row["original"][:6000], source_path=row["token"], chapter=row["section"],
        source_revision=row["revision"], applicable_versions=str(row["android_version"] or "unspecified"),
        confidence="indexed clause; applicability to a specific device unverified",
        last_verified=row.get("fetched_at") or row.get("synced_at", ""), license=row["license"], score=score,
        source_anchors=[SourceAnchor(
            url=row["url"], revision=row["revision"],
            evidence_type="official" if row["kind"] == "cdd" else "document",
        )],
        extra={"section": row["section"], "requirement_id": row["requirement_id"],
               "requirement_level": row["level"], "applicability_context": row["condition"][:4000],
               "reference": row["token"], "source_url": row["url"],
               "source_kind": row["kind"], "status": "stale" if row["error"] else "indexed"},
    )


def serialize_clause(row):
    """Return standard search provenance and the full retained clause fields."""
    return {**row, **_clause_hit(row).to_dict()}


def _clause_token(source_id, revision, section, requirement, condition, original, url):
    value = "\0".join((source_id, revision, section, requirement, condition, original, url))
    return "POL-" + hashlib.sha256(value.encode()).hexdigest()[:24]


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
        hits, retrieval = provider.search_with_status(query[:512], limit=5, android_api_level=api)
        items = [{"reference": h.source_path, "source_url": h.extra["source_url"],
                  "source_revision": h.source_revision, "title": h.title,
                  "requirement_id": h.extra["requirement_id"], "requirement_level": h.extra["requirement_level"],
                  "applicability_context": h.extra["applicability_context"][:2000],
                  "original_text": h.snippet[:3000], "android_version": h.applicable_versions,
                  "source_kind": h.extra["source_kind"], "status": h.extra["status"],
                  "fetched_at": h.last_verified, "license": h.license} for h in hits]
        return {"status": "matched" if items else "unavailable" if any(not r["revision"] or r["error"] for r in selected) else "no_match",
                "coverage_complete": False, "items": items, "retrieval": retrieval,
                "reason": "Ranked local candidates only; verify applicability and current revision"}
    except (OSError, sqlite3.Error, ValueError):
        return {"status": "unavailable", "coverage_complete": False, "items": [],
                "reason": "Web knowledge index unavailable"}
