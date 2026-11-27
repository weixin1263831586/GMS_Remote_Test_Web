"""Owner isolation and invalidation of locally embedded historical cases."""

from features.redmine.cases import semantic
from features.redmine.cases.case_search import RedmineCaseSearch
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
