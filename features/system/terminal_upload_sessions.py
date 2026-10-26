"""Owner-bound terminal upload sessions and shared SFTP destination handling."""

from __future__ import annotations

import fcntl
import hashlib
import os
import posixpath
from pathlib import Path

from foundation.error_model import ApiError
from foundation.private_config import read_json_object, write_private_json
from foundation.uploads import remote_home_file_path, safe_upload_token


def bind_upload_session(root, owner_id, upload_id, binding) -> Path:
    """Pin non-secret target metadata before accepting or resuming chunks."""
    if not owner_id or not upload_id:
        raise ApiError.malformed_request("上传会话缺少账号或 upload_id")
    owner_key = hashlib.sha256(owner_id.encode()).hexdigest()
    directory = Path(root) / "terminal" / owner_key / safe_upload_token(upload_id)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / ".binding.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        metadata = directory / "target.json"
        if metadata.exists():
            try:
                previous = read_json_object(metadata)
            except ValueError as exc:
                raise ApiError.conflict("上传会话损坏，请重新上传") from exc
            if previous != binding:
                raise ApiError.conflict("上传会话的目标或文件已变化，请使用新的 upload_id")
        else:
            write_private_json(metadata, binding)
    finally:
        os.close(descriptor)
    return directory


def upload_binding(config, path, filename, total_chunks, file_size):
    return {
        "worker_id": config["worker_id"], "host": config["host"],
        "username": config["username"], "directory": str(path or "").strip(),
        "filename": filename, "total_chunks": total_chunks, "file_size": file_size,
    }


def push_terminal_upload(ssh, local_path, directory, username, optimize):
    """An explicit destination failure must not fall back to another directory."""
    filename = Path(local_path).name
    with ssh.open_sftp() as sftp:
        optimize(sftp)
        if directory and directory.strip():
            target = directory.strip().rstrip("/") or "/"
            try:
                sftp.stat(target)
            except FileNotFoundError:
                sftp.mkdir(target)
            remote = posixpath.join(target, filename)
        else:
            remote = remote_home_file_path(username, filename)
        sftp.put(str(local_path), remote, confirm=True)
    return remote
