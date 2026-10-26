"""重启对账：无执行者的中间态证据快照必须进入 interrupted 终态（评审 P1）。"""

from pathlib import Path

from features.redmine.evidence import (
    _ACTIVE_FETCH_SNAPSHOTS,
    reconcile_interrupted_fetches,
    wait_for_snapshot,
)
from features.redmine.evidence_store import EvidenceStore


def _make_intermediate(owner_root: Path) -> str:
    store = EvidenceStore(owner_root)
    snapshot = store.create_snapshot(issue_id=648526, download_policy="all")
    store.update_snapshot(snapshot["snapshot_id"], status="downloading")
    return snapshot["snapshot_id"]


def test_orphaned_intermediate_snapshot_is_marked_interrupted(tmp_path):
    orphan_id = _make_intermediate(tmp_path / "owner-a")
    # 无 evidence.sqlite3 的杂散目录必须被忽略，不得因对账新建存储。
    (tmp_path / "owner-stray").mkdir()

    reconciled = reconcile_interrupted_fetches(tmp_path)

    assert reconciled == [{"owner": "owner-a", "snapshot_id": orphan_id}]
    row = EvidenceStore(tmp_path / "owner-a").get_snapshot(orphan_id)
    assert row["status"] == "interrupted"
    assert row["errors"][-1]["stage"] == "fetch"
    assert "重新发起抓取" in row["errors"][-1]["message"]
    # 幂等：再次对账没有新的中间态可标记。
    assert reconcile_interrupted_fetches(tmp_path) == []


def test_active_fetch_untouched_and_interrupted_is_terminal(tmp_path, monkeypatch):
    owner_root = tmp_path / "owner-a"
    active_id = _make_intermediate(owner_root)
    _ACTIVE_FETCH_SNAPSHOTS.add(("owner-a", active_id))
    try:
        assert reconcile_interrupted_fetches(tmp_path) == []
        assert (
            EvidenceStore(owner_root).get_snapshot(active_id)["status"]
            == "downloading"
        )
    finally:
        _ACTIVE_FETCH_SNAPSHOTS.discard(("owner-a", active_id))

    store = EvidenceStore(owner_root)
    monkeypatch.setattr(
        "features.redmine.evidence.owner_evidence_store",
        lambda _owner: store,
    )
    store.update_snapshot(active_id, status="interrupted")
    snapshot = wait_for_snapshot("owner-a", active_id, timeout_seconds=0.1)
    assert snapshot["status"] == "interrupted"


_LEGACY_ARTIFACTS_SCHEMA = """
CREATE TABLE redmine_evidence_artifacts (
    artifact_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    attachment_id TEXT DEFAULT '',
    filename TEXT DEFAULT '',
    original_filename TEXT DEFAULT '',
    content_type TEXT DEFAULT '',
    detected_content_type TEXT DEFAULT '',
    size_bytes INTEGER DEFAULT 0,
    declared_size INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    stored_path TEXT DEFAULT '',
    derived_text_path TEXT DEFAULT '',
    kind TEXT DEFAULT 'unknown',
    status TEXT DEFAULT 'metadata',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def _seed_legacy_partial_rows(owner_root):
    """构造旧语义库：partial 行 + 磁盘原件，检验迁移的原件重判定。"""
    import hashlib
    import sqlite3

    owner_root.mkdir(parents=True, exist_ok=True)
    evidence_dir = owner_root / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    good = b"attachment-bytes"
    (evidence_dir / "ok.bin").write_bytes(good)
    (evidence_dir / "nodigest.bin").write_bytes(b"123456")
    (evidence_dir / "truncated.bin").write_bytes(b"corrupted")

    rows = [
        # (artifact_id, stored_path, sha256, size, derived, 期望迁移后 status)
        ("art_ok", "ok.bin", hashlib.sha256(good).hexdigest(), len(good), "d/ok.txt", "ready"),
        ("art_baddigest", "truncated.bin", "0" * 64, 9, "", "partial"),
        ("art_nofile", "missing.bin", "1" * 64, 5, "", "partial"),
        ("art_nodigest", "nodigest.bin", "", 6, "", "ready"),
    ]
    conn = sqlite3.connect(owner_root / "evidence.sqlite3")
    conn.executescript(_LEGACY_ARTIFACTS_SCHEMA)
    for artifact_id, stored, digest, size, derived, _ in rows:
        conn.execute(
            "INSERT INTO redmine_evidence_artifacts (artifact_id, snapshot_id, status,"
            " stored_path, sha256, size_bytes, derived_text_path, error, created_at)"
            " VALUES (?, 'snap1', 'partial', ?, ?, ?, ?, '派生失败: TimeoutError', '2026-01-01T00:00:00')",
            (artifact_id, stored, digest, size, derived),
        )
    conn.commit()
    conn.close()
    return rows


def test_legacy_partial_originals_restored_only_after_file_verification(tmp_path):
    rows = _seed_legacy_partial_rows(tmp_path / "owner-a")

    store = EvidenceStore(tmp_path / "owner-a")

    for artifact_id, _stored, _digest, _size, _derived, expected in rows:
        artifact = store.get_artifact(artifact_id)
        assert artifact["status"] == expected, artifact_id
        # 派生告警与旧错误保留：恢复 ready 不吞掉派生缺口。
        if artifact_id == "art_ok":
            assert artifact["derived_status"] == "partial"
            assert "派生失败" in artifact["error"]

    # 幂等：重复打开不改变结果。
    again = EvidenceStore(tmp_path / "owner-a")
    assert again.get_artifact("art_ok")["status"] == "ready"
    assert again.get_artifact("art_baddigest")["status"] == "partial"
