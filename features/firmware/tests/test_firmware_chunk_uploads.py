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
