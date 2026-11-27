"""Schedule existing owner documents through the personal retrieval adapter."""

from functools import partial

from features.retrieval import embedding_configuration_issue, owner_index_job

from .personal_semantic import PersonalSemantic
from .storage import DB_PATH, KnowledgeStore


def sync_approved_wiki(config):
    if config.get("auto_index_approved_wiki", True) is not True:
        return []
    from .external.federation import federated_service

    wiki = federated_service().provider("android_internals")
    if wiki is None:
        return []
    state = wiki.status()
    if (not state.approved_revision or state.approved_revision != state.available_revision
            or state.approved_revision == state.source_revision):
        return []
    return [{"source_id": "android_internals", "operation": "lexical_index",
             **wiki.reindex(approved_only=True)}]


def personal_index_jobs(owners, config):
    scope = "personal_knowledge"
    if (not owners or config.get("auto_index_owner_knowledge", True) is not True
            or embedding_configuration_issue(scope) is not None or not DB_PATH.is_file()):
        return {}
    store = KnowledgeStore()
    return dict(owner_index_job(scope, owner, partial(PersonalSemantic, store, owner))
                for owner in owners.intersection(store.document_owners()))
