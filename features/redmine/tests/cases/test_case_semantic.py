"""Owner isolation and invalidation of locally embedded historical cases."""

from features.redmine.cases import semantic
from features.redmine.cases.case_search import RedmineCaseSearch
from features.redmine.indexing import redmine_index_jobs
from features.redmine.knowledge.repository import RedmineKnowledgeDB


class Encoder:
    identity = "test-model"

    def encode(self, texts):
        return [[1.0, 0.0] if "memory" in text or "运行容量" in text else [0.0, 1.0]
                for text in texts]


def test_case_semantic_owner_and_content_bound(tmp_path, monkeypatch):
    first = RedmineKnowledgeDB(tmp_path / "one" / "knowledge.sqlite3")
    second = RedmineKnowledgeDB(tmp_path / "two" / "knowledge.sqlite3")
    monkeypatch.setattr(semantic, "local_embedding", lambda source: Encoder())
    monkeypatch.setattr(semantic, "local_reranker", lambda source: None)
    first.upsert_case_fact({"issue_id": 1, "subject": "memory capacity", "problem_summary": "memory"})
    second.upsert_case_fact({"issue_id": 1, "subject": "other owner", "problem_summary": "unrelated"})
    assert semantic.CaseSemantic(first).sync()["coverage_complete"]
    hits = RedmineCaseSearch(first).search_similar("运行容量")
    assert hits[0]["issue_id"] == 1
    assert "semantic" in hits[0]["retrieval_channels"]
    assert not semantic.CaseSemantic(second).search("运行容量", limit=10)
    first.upsert_case_fact({"issue_id": 1, "subject": "changed", "problem_summary": "unrelated"})
    assert not semantic.CaseSemantic(first).search("运行容量", limit=10)
    first.reset()
    assert not semantic.CaseSemantic(first).search("运行容量", limit=10)


def test_case_scheduler_indexes_only_retained_active_owner_cases(tmp_path, monkeypatch):
    from features.redmine import indexing

    monkeypatch.setattr(indexing, "owner_knowledge_db_path", lambda owner: tmp_path / owner / "knowledge.sqlite3")
    monkeypatch.setattr(indexing, "embedding_configuration_issue",
                        lambda scope: None if scope == "redmine_cases" else "source_disabled")
    monkeypatch.setattr(semantic, "local_embedding", lambda scope: Encoder())
    for owner in ("active-owner", "disabled-owner"):
        db = RedmineKnowledgeDB(indexing.owner_knowledge_db_path(owner))
        db.upsert_case_fact({"issue_id": 1, "subject": "memory"})

    assert redmine_index_jobs({"active-owner"}, {"auto_index_redmine": False}) == {}
    jobs = redmine_index_jobs({"active-owner", "without-archive"}, {})
    assert len(jobs) == 1
    inspect, sync = next(iter(jobs.values()))
    assert inspect()["indexed"] == 0
    assert sync(batch_size=1)["coverage_complete"]
    disabled_db = RedmineKnowledgeDB(indexing.owner_knowledge_db_path("disabled-owner"))
    assert not semantic.CaseSemantic(disabled_db).index.path.exists()
