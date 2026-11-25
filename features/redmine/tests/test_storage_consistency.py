import sqlite3

import pytest

from features.redmine.knowledge.repository import RedmineKnowledgeDB
from features.redmine.repository.storage import RepositoryStorageMixin


@pytest.mark.parametrize(
    "repository,table,payload",
    [
        (RepositoryStorageMixin(), "redmine_agent_issue_fts", {"issue_id": 1}),
        (RedmineKnowledgeDB.__new__(RedmineKnowledgeDB), "redmine_case_facts_fts",
         {"issue_id": 1, "subject": "new", "problem_summary": "",
          "error_signature": "", "root_cause": "", "solution": "", "keywords_json": "[]"}),
    ],
)
def test_failed_fts_replacement_rolls_back_delete(repository, table, payload):
    with sqlite3.connect(":memory:") as conn:
        conn.execute(f"CREATE TABLE {table} (issue_id INTEGER, subject TEXT)")
        conn.execute(f"INSERT INTO {table} VALUES (1, 'old')")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError):
            with conn:
                repository._replace_fts(conn, payload)
        assert conn.execute(f"SELECT subject FROM {table}").fetchone() == ("old",)


def test_issue_documents_use_atomic_publication(tmp_path):
    repository = RepositoryStorageMixin()
    repository.docs_dir = tmp_path
    path = repository.write_issue_doc(1, "first")
    assert path == str(tmp_path / "redmine-1.md")
    repository.write_issue_doc(1, "second")
    assert (tmp_path / "redmine-1.md").read_text(encoding="utf-8") == "second"
    assert list(tmp_path.iterdir()) == [tmp_path / "redmine-1.md"]
