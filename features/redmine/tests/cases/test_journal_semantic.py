"""Archived journal retrieval is scoped, revision-bound and explicitly incomplete."""

from features.redmine.knowledge import journal_semantic
from features.redmine.repository import RedmineAgentDB


class Encoder:
    identity = "test-model"

    def encode(self, texts):
        return [[1.0, 0.0] if "memory" in text or "运行容量" in text else [0.0, 1.0]
                for text in texts]


def _repo(tmp_path, name):
    return RedmineAgentDB(tmp_path / name / "issues.sqlite3", tmp_path / name / "docs")


def _issue(repo, notes):
    repo.upsert_issue({"issue_id": 42, "subject": "diagnosis", "journals_json": notes})


def test_journal_index_owner_revision_private_notes_and_reset(tmp_path, monkeypatch):
    first = _repo(tmp_path, "first")
    second = _repo(tmp_path, "second")
    monkeypatch.setattr(journal_semantic, "local_embedding", lambda source: Encoder())
    _issue(first, [{"id": 11, "notes": "memory diagnosis", "created_on": "2026-01-01"},
                   {"id": 12, "notes": "memory private", "private_notes": True}])
    _issue(second, [{"id": 11, "notes": "unrelated"}])
    index = journal_semantic.JournalSemantic(first)
    assert index.sync()["coverage_complete"]
    result = index.search("运行容量")
    assert result["retrieval"]["total"] == 1
    assert not result["coverage_complete"]
    assert result["results"][0]["journal_id"] == "11"
    assert result["results"][0]["snippet"] == "memory diagnosis"
    assert not journal_semantic.JournalSemantic(second).search("运行容量")["results"]
    _issue(first, [{"id": 11, "notes": "changed"}])
    assert not index.search("运行容量")["results"]
    index.sync()
    first.reset()
    assert not index.search("运行容量")["results"]
    assert index.status()["indexed"] == 0


def test_chunk_bounds_and_source_anchors(monkeypatch):
    monkeypatch.setattr(journal_semantic, "MAX_NOTE", 2000)
    chunks, complete = journal_semantic._chunks(42, [{"id": 7, "notes": "x" * 3000}])
    assert not complete
    assert [(chunk.start, chunk.end) for chunk in chunks.values()] == [(0, 1600), (1400, 2000)]
    assert all(chunk.journal_id == "7" for chunk in chunks.values())
