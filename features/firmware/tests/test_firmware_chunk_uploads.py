import asyncio
import json
import threading
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from fastapi import UploadFile

from features.firmware import chunk_uploads


def _state():
    return SimpleNamespace(
        firmware_upload_progress={},
        firmware_upload_progress_lock=threading.RLock(),
    )


def _chunk_form(content: bytes, **overrides):
    values = {
        "upload_id": "upload-1",
        "file_name": "update.img",
        "file_size": "8",
        "chunk_size": "4",
        "total_chunks": "2",
        "chunk_index": "0",
        "stage_only": "1",
        "file": UploadFile(file=BytesIO(content), filename="update.img"),
    }
    values.update(overrides)
    return values


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def test_staged_upload_is_resumable_and_owner_scoped():
    state = _state()
    with TemporaryDirectory() as root:
        first_response, merged = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"1234"), "alice", root, state, 3600
            )
        )
        final_response, merged = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"5678", chunk_index="1"),
                "alice",
                root,
                state,
                3600,
            )
        )

        assert _payload(first_response)["upload_complete"] is False
        final = _payload(final_response)
        assert final["upload_complete"] is True
        assert final["staged"] is True
        assert merged is None
        assert state.firmware_upload_progress == {}

        check_response, _ = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(
                    b"",
                    check_chunks="1",
                    file=None,
                    chunk_index=None,
                ),
                "alice",
                root,
                state,
                3600,
            )
        )
        assert _payload(check_response)["staged"] is True
        staged, error = chunk_uploads.load_staged_upload(root, "alice", "upload-1")
        assert error is None
        assert Path(staged["path"]).read_bytes() == b"12345678"
        assert Path(staged["path"]).name == "staged-update.img"
        other_staged, other_error = chunk_uploads.load_staged_upload(
            root, "bob", "upload-1"
        )
        assert other_staged is None
        assert "not found" in other_error.lower()


def test_short_chunk_is_rejected_without_publishing_partial_file():
    state = _state()
    with TemporaryDirectory() as root:
        response, merged = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"12"), "alice", root, state, 3600
            )
        )

        assert response.status_code == 400
        assert "size mismatch" in _payload(response)["error"].lower()
        assert merged is None
        session = Path(chunk_uploads.upload_session_dir(root, "alice", "upload-1"))
        assert not (session / "chunk_00000").exists()
        assert not list(session.glob("*.upload"))


def test_content_fingerprint_prevents_stale_resume_for_same_file_metadata():
    state = _state()
    first_fingerprint = "a" * 64
    second_fingerprint = "b" * 64
    with TemporaryDirectory() as root:
        first_response, _ = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"1234", content_fingerprint=first_fingerprint),
                "alice",
                root,
                state,
                3600,
            )
        )
        conflicting_response, merged = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(
                    b"abcd",
                    content_fingerprint=second_fingerprint,
                    chunk_index="0",
                ),
                "alice",
                root,
                state,
                3600,
            )
        )

        assert first_response.status_code == 200
        assert conflicting_response.status_code == 400
        assert "metadata does not match" in _payload(conflicting_response)["error"].lower()
        assert merged is None


def test_legacy_bin_staging_is_renamed_with_original_firmware_extension():
    with TemporaryDirectory() as root:
        session = Path(chunk_uploads.upload_session_dir(root, "alice", "upload-1"))
        session.mkdir(parents=True)
        (session / "upload_metadata.json").write_text(
            json.dumps({
                "file_name": "update.img",
                "total_chunks": 1,
                "file_size": 8,
            }),
            encoding="utf-8",
        )
        legacy = session / chunk_uploads.LEGACY_STAGED_FILENAME
        legacy.write_bytes(b"firmware")

        staged, error = chunk_uploads.load_staged_upload(
            root,
            "alice",
            "upload-1",
        )

        assert error is None
        assert Path(staged["path"]).name == "staged-update.img"
        assert Path(staged["path"]).read_bytes() == b"firmware"
        assert not legacy.exists()


def test_burn_lock_prevents_parallel_finalize_and_can_be_released():
    with TemporaryDirectory() as root:
        session = Path(chunk_uploads.upload_session_dir(root, "alice", "upload-1"))
        session.mkdir(parents=True)
        staged = {"session_dir": str(session)}

        first = chunk_uploads.acquire_burn_lock(staged)
        second = chunk_uploads.acquire_burn_lock(staged)
        assert first
        assert second is None

        chunk_uploads.release_burn_lock(first)
        assert chunk_uploads.acquire_burn_lock(staged)
