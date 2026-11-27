"""Local semantic maintenance of existing, owner-scoped Redmine collections."""

from features.retrieval import embedding_configuration_issue, owner_index_job
from foundation.config_paths import sanitize_owner_id

from .cases.semantic import CaseSemantic
from .knowledge.journal_semantic import JournalSemantic
from .knowledge.repository import RedmineKnowledgeDB
from .repository import RedmineAgentDB
from .users import owner_db_path, owner_docs_dir, owner_knowledge_db_path


def redmine_index_jobs(owners, config):
    if not owners or config.get("auto_index_redmine", True) is not True:
        return {}
    cases_enabled = embedding_configuration_issue("redmine_cases") is None
    journals_enabled = embedding_configuration_issue("redmine_journals") is None
    jobs = []
    for owner in owners:
        if not owner or owner != sanitize_owner_id(owner):
            continue
        if cases_enabled and owner_knowledge_db_path(owner).is_file():
            jobs.append(owner_index_job("redmine_cases", owner, lambda owner=owner:
                                        CaseSemantic(RedmineKnowledgeDB(owner_knowledge_db_path(owner)))))
        if journals_enabled and owner_db_path(owner).is_file():
            jobs.append(owner_index_job("redmine_journals", owner, lambda owner=owner:
                                        JournalSemantic(RedmineAgentDB(owner_db_path(owner), owner_docs_dir(owner)))))
    return dict(jobs)
