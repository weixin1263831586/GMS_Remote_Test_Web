"""Owner-approved imported policy documents; never part of the public index."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .external.web_sources import WebKnowledgeProvider, model_catalog_fingerprint
from .storage import KnowledgeStore


IMPORT_TAG = 'external-policy-ai-approved'
IMPORT_SUFFIXES = {'.pdf', '.html', '.htm'}
MAX_IMPORT_BYTES = 12 * 1024 * 1024


def content_hash(doc):
    text = str(doc.get('raw_content') or doc.get('content_md') or '')
    return hashlib.sha256(text.encode()).hexdigest()


def is_policy_import(tags):
    return IMPORT_TAG in {part.strip() for part in re.split(r'[,，;；\n]+', tags or '')}


def approve_policy_import(owner_id, doc, *, provider=None):
    if not doc.get('doc_id') or Path(str(doc.get('source_file') or '')).suffix.lower() not in IMPORT_SUFFIXES:
        raise ValueError('Only PDF/HTML imports can be approved')
    (provider or WebKnowledgeProvider()).approve_import(owner_id, doc['doc_id'], content_hash(doc))


def recall_owner_policy_imports(owner_id, query, *, provider=None, store=None):
    grants = (provider or WebKnowledgeProvider()).approved_imports(owner_id)
    if not grants:
        return None
    terms = [term.casefold() for term in re.findall(r'[\w.-]{3,}', query)[:24]]
    if not terms:
        return {'status': 'no_match', 'coverage_complete': False, 'items': []}
    store = store or KnowledgeStore()
    items = []
    for doc in store.search(owner_id, query, tag=IMPORT_TAG, limit=20):
        identity = doc.get('doc_id')
        if identity not in grants or grants[identity] != content_hash(doc):
            continue
        text = str(doc.get('raw_content') or doc.get('content_md') or '')
        lower = text.casefold()
        positions = [lower.find(term) for term in terms if term in lower]
        if not positions:
            continue
        start = max(0, min(positions) - 300)
        digest = grants[identity]
        items.append({
            'reference': f'ref:owner-policy:{identity}:{digest[:16]}',
            'source_revision': digest, 'title': str(doc.get('title') or '')[:200],
            'original_text': text[start:start + 3000],
            'source_file': str(doc.get('source_file') or '')[:200],
            'status': 'imported_candidate',
            'fetched_at': doc.get('updated_at') or '',
            'source_kind': 'authorized_import',
        })
        if len(items) == 5:
            break
    return {'status': 'matched' if items else 'no_match', 'coverage_complete': False,
            'items': items, 'reason': 'Owner-approved document revision; original authority and applicability unverified'}


def import_model_available():
    return bool(model_catalog_fingerprint())
