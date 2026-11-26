"""Read-only retained snapshot RAG scopes and attachment changes."""
import json
import sqlite3

from features.redmine.evidence.intelligent import SnapshotCorpus


def fixture_snapshot(tmp_path, owner):
    root = tmp_path / "redmine" / "by_user" / owner
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "log.txt").write_text("kernel evidence: failure signature", encoding="utf-8")
    with sqlite3.connect(root / "evidence.sqlite3") as db:
        db.executescript("""
        CREATE TABLE redmine_evidence_snapshots(snapshot_id TEXT, status TEXT, content_sha256 TEXT, manifest_json TEXT);
        CREATE TABLE redmine_evidence_artifacts(snapshot_id TEXT, artifact_id TEXT, filename TEXT, kind TEXT,
           status TEXT, derived_status TEXT, stored_path TEXT, derived_text_path TEXT, sha256 TEXT);
        """)
        db.execute("INSERT INTO redmine_evidence_snapshots VALUES(?,?,?,?)",
                   ("snap", "ready", "rev", json.dumps({"subject": "Boot failure",
                    "description": "initial observation", "journals": [{"notes": "later resolution"}]})))
        db.execute("INSERT INTO redmine_evidence_artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                   ("snap", "a1", "log.txt", "log", "ready", "ready", "log.txt", "", "digest"))
    return root


def test_snapshot_local_retrieval_reads_attachment_and_excludes_future_journal(tmp_path):
    root = fixture_snapshot(tmp_path, "a")
    corpus = SnapshotCorpus("a", "snap", include_journals=False, data_root=tmp_path)
    assert "journal:0" not in corpus.documents
    hit = corpus.search("failure signature", 4).hits[0]
    assert hit.metadata["document"] == "artifact:a1"
    assert corpus.current(hit)
    assert not corpus.search("later resolution", 4).hits
    (root / "evidence" / "log.txt").write_text("changed", encoding="utf-8")
    assert not corpus.current(hit)


def test_snapshot_does_not_read_other_owner(tmp_path):
    import pytest

    fixture_snapshot(tmp_path, "a")
    with pytest.raises(sqlite3.Error):
        SnapshotCorpus("b", "snap", data_root=tmp_path)
    with pytest.raises(ValueError):
        SnapshotCorpus("../a", "snap", data_root=tmp_path)


def test_snapshot_revision_rechecked(tmp_path):
    root = fixture_snapshot(tmp_path, "a")
    corpus = SnapshotCorpus("a", "snap", data_root=tmp_path)
    hit = corpus.search("initial observation", 4).hits[0]
    assert corpus.current(hit)
    with sqlite3.connect(root / "evidence.sqlite3") as db:
        db.execute("UPDATE redmine_evidence_snapshots SET content_sha256='changed'")
    assert not corpus.current(hit)
