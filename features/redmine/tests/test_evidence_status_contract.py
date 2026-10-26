"""status / derived_status / completeness 的语义契约（评审 P2）。

2026-10（000bd13 起）原件状态与派生状态分离：
- 附件原件保存成功 → ``status=ready``，与派生成败无关；
- 派生缺口只落在 ``derived_status``（partial/missing/empty）与快照的
  derive 阶段错误，拉低 ``completeness.derived_text``，绝不降级原件；
- 下载缺口把快照拉成 partial 并拉低 ``requested_downloads``；
- 快照 failed 时 completeness 的证据维度必须为 False。
本文件把三条语义钉进契约测试，防止后续改动让三者漂移。
"""

from features.redmine.evidence import snapshot_completeness
from features.redmine.evidence_store import EvidenceStore


def _ready_snapshot(store: EvidenceStore, **fields):
    snapshot = store.create_snapshot(issue_id=648526, download_policy="all")
    store.update_snapshot(
        snapshot["snapshot_id"],
        status="ready",
        content_sha256="a" * 64,
        **fields,
    )
    return store.get_snapshot(snapshot["snapshot_id"])


def test_derivation_gap_keeps_original_ready_and_flags_derived_dimension(tmp_path):
    store = EvidenceStore(tmp_path / "owner")
    snapshot = _ready_snapshot(store, error_json=[{"stage": "derive", "message": "OCR 失败"}])
    created = store.create_artifact(
        snapshot_id=snapshot["snapshot_id"], attachment_id="776657",
        filename="shot.png", original_filename="shot.png",
        content_type="image/png", kind="image",
    )
    store.update_artifact(
        created["artifact_id"],
        status="ready", derived_status="missing", derived_text_path="",
    )

    flags = snapshot_completeness(snapshot, store.list_artifacts(snapshot["snapshot_id"]))
    # 原件维度全绿；只有派生维度如实为缺口。
    assert flags["issue"] and flags["journals"] and flags["requested_downloads"]
    assert flags["derived_text"] is False
    artifact = store.get_artifact(created["artifact_id"])
    assert artifact["status"] == "ready"
    assert artifact["derived_status"] == "missing"
    assert artifact["text_available"] is False


def test_download_gap_pulls_snapshot_partial_and_requested_downloads_false(tmp_path):
    store = EvidenceStore(tmp_path / "owner")
    snapshot = store.create_snapshot(issue_id=648526, download_policy="all")
    store.update_snapshot(
        snapshot["snapshot_id"],
        status="partial",
        content_sha256="b" * 64,
        error_json=[{"stage": "download", "message": "附件 HTTP 500"}],
    )
    snapshot = store.get_snapshot(snapshot["snapshot_id"])

    flags = snapshot_completeness(snapshot, [])
    assert flags["issue"] and flags["journals"]
    assert flags["requested_downloads"] is False


def test_failed_snapshot_is_never_fully_complete(tmp_path):
    store = EvidenceStore(tmp_path / "owner")
    snapshot = store.create_snapshot(issue_id=648526, download_policy="none")
    store.update_snapshot(
        snapshot["snapshot_id"],
        status="failed",
        error_json=[{"stage": "issue", "message": "Redmine 不可达"}],
    )
    snapshot = store.get_snapshot(snapshot["snapshot_id"])

    flags = snapshot_completeness(snapshot, [])
    assert flags["issue"] is False and flags["journals"] is False
    assert not all(flags.values())


def test_derived_warning_only_lives_in_derived_dimension(tmp_path):
    """derived_status=partial（索引不完整）不改变原件与下载维度。"""
    store = EvidenceStore(tmp_path / "owner")
    snapshot = _ready_snapshot(store, error_json=[{"stage": "derive", "message": "成员超限"}])
    flags = snapshot_completeness(snapshot, [])

    assert flags["issue"] and flags["requested_downloads"]
    assert flags["derived_text"] is False
    # 对照：快照没有 derive 阶段错误时派生维度为 True。
    clean = _ready_snapshot(store)
    assert snapshot_completeness(clean, [])["derived_text"] is True
