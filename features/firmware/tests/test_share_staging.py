"""Shared firmware staging is bounded, atomic and isolated by ATS run."""

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from features.firmware import shares_api as api


@pytest.fixture
def share_source(tmp_path, monkeypatch):
    monkeypatch.setattr("foundation.config.settings", SimpleNamespace(data_root=tmp_path))
    monkeypatch.setattr(api, "runtime", SimpleNamespace(config_manager=SimpleNamespace(load_config=lambda: {})))
    monkeypatch.setattr(api, "firmware_share_record_snapshot", lambda share_id: {
        "host": "source", "user": "user", "path": "/firmware/update.img", "expired": False,
    })
    monkeypatch.setattr(api, "_find_record", lambda share_id: ([], {"id": share_id}))
    monkeypatch.setattr(api, "_stat_remote", lambda *args, **kwargs: {"size": 4, "filename": "update.img"})
    source = Mock(side_effect=lambda *args, **kwargs: iter([b"firm"]))
    monkeypatch.setattr(api, "_remote_file_iterator", source)
    return source


def test_same_named_images_do_not_overwrite_other_runs(tmp_path, share_source):
    first = api.resolve_firmware_share_record("share-1", run_id="ats_one")
    second = api.resolve_firmware_share_record("share-1", run_id="ats_two")
    assert first["ok"] and second["ok"]
    assert first["firmware_path"] != second["firmware_path"]
    assert first["sha256"] == hashlib.sha256(b"firm").hexdigest()
    assert sorted(path.read_bytes() for path in tmp_path.rglob("*.img")) == [b"firm", b"firm"]
    assert list(tmp_path.rglob("*.part")) == []


def test_interrupted_download_preserves_previous_image_and_removes_partial(tmp_path, share_source):
    first = api.resolve_firmware_share_record("share-1", run_id="ats_one")

    def interrupted(*args, **kwargs):
        yield b"fi"
        raise OSError("source disconnected")

    share_source.side_effect = interrupted
    with pytest.raises(OSError, match="source disconnected"):
        api.resolve_firmware_share_record("share-1", run_id="ats_one")
    assert first["ok"]
    assert next(tmp_path.rglob("*.img")).read_bytes() == b"firm"
    assert list(tmp_path.rglob("*.part")) == []


@pytest.mark.parametrize("payload", [b"fi", b"extra"])
def test_invalid_download_length_never_publishes_an_image(tmp_path, share_source, payload):
    share_source.side_effect = lambda *args, **kwargs: iter([payload])
    result = api.resolve_firmware_share_record("share-1", run_id="ats_one", max_bytes=4)
    assert result["ok"] is False
    assert list(tmp_path.rglob("*.img")) == []
    assert list(tmp_path.rglob("*.part")) == []


def test_oversized_share_is_rejected_before_download(share_source):
    result = api.resolve_firmware_share_record("share-1", run_id="ats_one", max_bytes=3)
    assert result["ok"] is False
    share_source.assert_not_called()


def test_stage_identifier_cannot_escape_the_artifact_root(share_source):
    result = api.resolve_firmware_share_record("share-1", run_id="../outside")
    assert result["ok"] is False
    share_source.assert_not_called()


def test_progress_reports_written_bytes_and_completes_without_source_secrets(share_source):
    share_source.side_effect = lambda *args, **kwargs: iter([b"fi", b"rm"])
    progress = []
    result = api.resolve_firmware_share_record("share-1", run_id="ats_one", on_progress=progress.append)
    assert result["ok"]
    assert [item["downloaded_bytes"] for item in progress] == [0, 2, 4]
    assert all(item["total_bytes"] == 4 and item["bytes_per_second"] >= 0 for item in progress)
    assert all(set(item) == {"downloaded_bytes", "total_bytes", "elapsed_seconds", "bytes_per_second"}
               for item in progress)


def test_progress_cancellation_removes_partial_file(tmp_path, share_source):
    def cancel(progress):
        if progress["downloaded_bytes"]:
            raise RuntimeError("run cancelled")

    with pytest.raises(RuntimeError, match="run cancelled"):
        api.resolve_firmware_share_record("share-1", run_id="ats_one", on_progress=cancel)
    assert list(tmp_path.rglob("*.img")) == []
    assert list(tmp_path.rglob("*.part")) == []
