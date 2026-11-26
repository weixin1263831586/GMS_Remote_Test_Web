"""Owner-approved imported policy documents; never part of the public index."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .external.web_sources import WebKnowledgeProvider
from .storage import POLICY_IMPORT_SOURCE, POLICY_IMPORT_TAG, KnowledgeStore


IMPORT_TAG = POLICY_IMPORT_TAG
IMPORT_SUFFIXES = {'.pdf', '.html', '.htm'}
MAX_IMPORT_BYTES = 12 * 1024 * 1024


def content_hash(doc):
    fields = [str(doc.get(key) or '') for key in ('raw_content', 'content_md', 'title', 'source_file')]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def _approved_content(doc, digest):
    if content_hash(doc) == digest:
        return True
    raw = str(doc.get('raw_content') or '')
    return bool(raw and raw == doc.get('content_md') and hashlib.sha256(raw.encode()).hexdigest() == digest)


def is_policy_import(tags):
    parts = re.split(r'[,，;；\n]+', tags) if isinstance(tags, str) else tags or []
    return IMPORT_TAG in {str(part).strip() for part in parts}


def policy_import_filter(owner_id):
    """Exclude restricted documents from general-purpose model prompts."""
    protected_ids = WebKnowledgeProvider().registered_imports(owner_id)

    def excluded(doc):
        return (doc.get('source') == POLICY_IMPORT_SOURCE
                or doc.get('doc_id') in protected_ids
                or is_policy_import(doc.get('tags')))

    return excluded


def approve_policy_import(owner_id, doc, *, model_catalog_hash=None, provider=None):
    if not doc.get('doc_id') or Path(str(doc.get('source_file') or '')).suffix.lower() not in IMPORT_SUFFIXES:
        raise ValueError('Only PDF/HTML imports can be approved')
    (provider or WebKnowledgeProvider()).approve_import(
        owner_id, doc['doc_id'], content_hash(doc), model_catalog_hash=model_catalog_hash,
    )


def recall_owner_policy_imports(owner_id, query, *, model_catalog_hash=None, provider=None, store=None):
    grants = (provider or WebKnowledgeProvider()).approved_imports(owner_id, model_catalog_hash=model_catalog_hash)
    if not grants:
        return None
    terms = [term.casefold() for term in re.findall(r'[\w.-]{3,}', query)[:24]]
    if not terms:
        return {'status': 'no_match', 'coverage_complete': False, 'items': []}
    try:
        store = store or KnowledgeStore()
        docs = store.search(owner_id, ' '.join(terms), tag=IMPORT_TAG, limit=20)
    except (OSError, sqlite3.Error, ValueError):
        return {'status': 'unavailable', 'coverage_complete': False, 'items': []}
    items = []
    for doc in docs:
        identity = doc.get('doc_id')
        if identity not in grants or not _approved_content(doc, grants[identity]):
            continue
        text = str(doc.get('raw_content') or doc.get('content_md') or '')
        lower = text.casefold()
        positions = [lower.find(term) for term in terms if term in lower]
        if not positions:
            continue
        start = max(0, min(positions) - 300)
        digest = grants[identity]
        metadata_bound = content_hash(doc) == digest
        items.append({
            'reference': f'ref:owner-policy:{identity}:{digest[:16]}',
            'source_revision': digest,
            'title': (str(doc.get('title') or '') if metadata_bound else text.splitlines()[0])[:200],
            'original_text': text[start:start + 3000],
            'source_file': str(doc.get('source_file') or '')[:200] if metadata_bound else '',
            'status': 'imported_candidate',
            'fetched_at': doc.get('updated_at') or '',
            'source_kind': 'authorized_import',
        })
        if len(items) == 5:
            break
    return {'status': 'matched' if items else 'no_match', 'coverage_complete': False,
            'items': items, 'reason': 'Owner-approved document revision; original authority and applicability unverified'}


def owner_policy_imports_current(owner_id, items, model_catalog_hash):
    grants = WebKnowledgeProvider().approved_imports(owner_id, model_catalog_hash=model_catalog_hash)
    store = KnowledgeStore()
    try:
        for item in items:
            _, _, doc_id, _ = item['reference'].split(':')
            digest = item['source_revision']
            if grants.get(doc_id) != digest:
                return False
            doc = store.get_doc(owner_id, doc_id)
            if (not doc or doc.get('archived') or not is_policy_import(doc.get('tags'))
                    or not _approved_content(doc, digest)):
                return False
    except (OSError, sqlite3.Error, ValueError, KeyError):
        return False
    return bool(items)
