"""Attachment aliases resolve by snapshot order, including same-second ties."""

import pytest

from features.redmine import evidence_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_store, "_now", lambda: "2026-10-27T10:00:00")
    return evidence_store.EvidenceStore(tmp_path)


def _artifact(store, snapshot, monkeypatch, artifact_id):
    monkeypatch.setattr(evidence_store, "new_artifact_id", lambda: artifact_id)
    return store.create_artifact(
        snapshot_id=snapshot["snapshot_id"], attachment_id="123",
        filename="log.txt", original_filename="log.txt",
        content_type="text/plain", kind="text",
    )


def test_default_lookup_uses_newest_snapshot_when_older_download_finishes_late(store, monkeypatch):
    old = store.create_snapshot(issue_id=1)
    new = store.create_snapshot(issue_id=1)
    newest = _artifact(store, new, monkeypatch, "art_a")
    older = _artifact(store, old, monkeypatch, "art_z")

    assert store.latest_snapshot_for_issue(1)["snapshot_id"] == new["snapshot_id"]
    assert store.get_artifact_by_attachment_id("123") == newest
    assert store.get_artifact_by_attachment_id("123", snapshot_id=old["snapshot_id"]) == older


def test_scoped_lookup_uses_insertion_order_for_same_second_rows(store, monkeypatch):
    snapshot = store.create_snapshot(issue_id=1)
    _artifact(store, snapshot, monkeypatch, "art_z")
    newest = _artifact(store, snapshot, monkeypatch, "art_a")

    assert store.get_artifact_by_attachment_id("123", snapshot_id=snapshot["snapshot_id"]) == newest
    assert store.get_artifact_by_attachment_id("123") == newest


def test_lookup_does_not_cross_attachment_or_snapshot_scope(store, monkeypatch):
    snapshot = store.create_snapshot(issue_id=1)
    _artifact(store, snapshot, monkeypatch, "art_a")

    assert store.get_artifact_by_attachment_id("456") is None
    assert store.get_artifact_by_attachment_id("123", snapshot_id="ev_missing") is None
