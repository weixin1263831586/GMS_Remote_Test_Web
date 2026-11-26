"""Incremental local LLM understanding of current public CDD passages."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from features.retrieval import understand_passages
from foundation.config import config_manager
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock
from foundation.redaction import redact_sensitive_text

from .base import search_terms


def enabled_key():
    from .web_sources import model_catalog_fingerprint

    config = (config_manager.load_config() or {}).get("retrieval")
    if not isinstance(config, dict) or config.get("auto_understand_public_cdd") is not True:
        return ""
    catalog = model_catalog_fingerprint()
    preferred = config.get("llm_provider", "")
    if not catalog or not isinstance(preferred, str):
        return ""
    return hashlib.sha256(json.dumps(["cdd-understanding-v1", catalog, preferred]).encode()).hexdigest()


def sync_batch(provider, model, key, *, batch_size=4):
    """Persist bounded passage results; a restart resumes the current model/source revision."""
    if not key or key != enabled_key():
        return {"status": "disabled", "operation": "document_understanding"}
    lock = try_acquire_sync_lock(provider._path().parent / "sync-locks" / "cdd-understanding.lock")
    if lock is None:
        return {"status": "busy", "operation": "document_understanding"}
    try:
        now = datetime.now(timezone.utc)
        retry_before = (now - timedelta(minutes=10)).isoformat()
        with provider._db(write=True) as db:
            # Derived hints never outlive the source revision or model identity.
            stale = """SELECT i.id FROM cdd_insights i LEFT JOIN sources s ON s.id=i.source_id
                       WHERE i.model_key<>? OR s.id IS NULL OR s.revision<>i.revision OR s.enabled=0"""
            db.execute(f"DELETE FROM cdd_insight_fts WHERE rowid IN ({stale})", (key,))
            db.execute(f"DELETE FROM cdd_insights WHERE id IN ({stale})", (key,))
            rows = db.execute(
                """SELECT c.token,c.source_id,c.revision,c.original,c.condition,c.section
                   FROM clauses c JOIN sources s ON s.id=c.source_id AND s.revision=c.revision
                   LEFT JOIN cdd_insights i ON i.token=c.token AND i.model_key=?
                   WHERE s.enabled=1 AND s.kind='cdd' AND s.error=''
                     AND (i.id IS NULL OR (i.status='error' AND i.attempted_at<?))
                   ORDER BY COALESCE(i.attempted_at,''),c.rowid LIMIT ?""",
                (key, retry_before, max(1, min(batch_size, 4))),
            ).fetchall()
        if not rows:
            return {"operation": "document_understanding", **status(provider)}
        passages = [{"id": row["token"], "text": redact_sensitive_text(row["original"]),
                     "context": redact_sensitive_text(row["section"] + "\n" + row["condition"])}
                    for row in rows]
        failed = False
        try:
            derived = understand_passages(passages, model)
        except Exception:
            derived, failed = {}, True
        if key != enabled_key():
            return {"status": "stale", "operation": "document_understanding"}
        written = 0
        with provider._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            for row in rows:
                current = db.execute(
                    """SELECT 1 FROM clauses c JOIN sources s ON s.id=c.source_id AND s.revision=c.revision
                       WHERE c.token=? AND c.source_id=? AND c.revision=?
                       AND s.enabled=1 AND s.kind='cdd' AND s.error=''""",
                    (row["token"], row["source_id"], row["revision"]),
                ).fetchone()
                if not current:
                    continue
                insight = derived.get(row["token"])
                state = "error" if failed else "ready" if insight else "no_supported_fact"
                db.execute("""INSERT INTO cdd_insights(token,source_id,revision,model_key,status,attempted_at,facts_json)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(token) DO UPDATE SET
                    source_id=excluded.source_id,revision=excluded.revision,model_key=excluded.model_key,
                    status=excluded.status,attempted_at=excluded.attempted_at,facts_json=excluded.facts_json""",
                    (row["token"], row["source_id"], row["revision"], key, state, now.isoformat(),
                     json.dumps(insight or {}, ensure_ascii=False)))
                identity = db.execute("SELECT id FROM cdd_insights WHERE token=?", (row["token"],)).fetchone()[0]
                db.execute("DELETE FROM cdd_insight_fts WHERE rowid=?", (identity,))
                if insight:
                    db.execute("INSERT INTO cdd_insight_fts(rowid,search_text) VALUES(?,?)",
                               (identity, insight["search_text"]))
                written += 1
        return {"operation": "document_understanding", "batch_processed": written, **status(provider)}
    finally:
        release_sync_lock(lock)


def status(provider, source_id=None):
    key = enabled_key()
    if not key:
        return {"status": "disabled", "processed": 0, "total": 0, "coverage_complete": False}
    if not provider._path().is_file():
        return {"status": "empty", "processed": 0, "total": 0, "coverage_complete": False}
    try:
        with provider._db() as db:
            row = db.execute("""SELECT count(*) AS total,
                count(CASE WHEN i.status IN ('ready','no_supported_fact') THEN 1 END) AS processed,
                count(CASE WHEN i.status='ready' THEN 1 END) AS enriched,
                count(CASE WHEN i.status='error' THEN 1 END) AS errors
                FROM clauses c JOIN sources s ON s.id=c.source_id AND s.revision=c.revision
                LEFT JOIN cdd_insights i ON i.token=c.token AND i.model_key=?
                WHERE s.enabled=1 AND s.kind='cdd' AND s.error=''
                AND (? IS NULL OR s.id=?)""", (key, source_id, source_id)).fetchone()
        result = dict(row)
    except (sqlite3.Error, OSError):
        return {"status": "unavailable", "processed": 0, "total": 0, "coverage_complete": False}
    complete = bool(result["total"] and result["processed"] == result["total"])
    return {**result, "status": "ready" if complete else "partial" if result["processed"] else "empty",
            "coverage_complete": complete, "scope": "current_retained_public_cdd_processing"}


def search(provider, query, *, version=None, limit=10):
    """Use model-generated synonyms for recall, then return only current raw clauses."""
    key = enabled_key()
    if not key or not provider._path().is_file():
        return []
    terms = search_terms(query)[:20]
    match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
    if not match:
        return []
    try:
        with provider._db() as db:
            rows = db.execute("""SELECT c.*,s.name,s.android_version,s.kind,s.license,s.error,s.synced_at,
                      bm25(cdd_insight_fts) AS rank
                FROM cdd_insight_fts JOIN cdd_insights i ON i.id=cdd_insight_fts.rowid
                JOIN clauses c ON c.token=i.token AND c.source_id=i.source_id AND c.revision=i.revision
                JOIN sources s ON s.id=c.source_id AND s.revision=c.revision
                WHERE cdd_insight_fts MATCH ? AND i.model_key=? AND i.status='ready'
                AND s.enabled=1 AND s.kind='cdd' AND s.error=''
                AND (? IS NULL OR s.android_version=?)
                ORDER BY rank,c.token LIMIT ?""", (match, key, version, version, max(1, min(limit, 20)))).fetchall()
        return [dict(row) for row in rows]
    except (sqlite3.Error, OSError):
        return []
