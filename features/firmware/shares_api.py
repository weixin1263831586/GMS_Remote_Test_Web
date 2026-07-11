from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import paramiko
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from features.users import get_client_username_from_request
from foundation.responses import error_response, success_response

from . import runtime


router = APIRouter()


class _AuthError(ValueError):
    """远端认证失败：前端可据此弹框让用户输入密码后重试（HTTP 401）。"""



_DEFAULT_STORE = Path(__file__).resolve().parents[2] / "data" / "firmware_shares.json"
_REMOTE_SPEC_RE = re.compile(r"^(?:(?P<user>[^@:/]+)@)?(?P<host>[^:]+):(?P<path>/.*)$")
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_DEFAULT_ALLOWED_PREFIXES = ("/home/", "/data/", "/mnt/")
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_records_lock = threading.RLock()


def _store_path() -> Path:
    configured = getattr(runtime, "firmware_share_store", None)
    return Path(configured) if configured else _DEFAULT_STORE


def _load_records() -> list[dict[str, Any]]:
    path = _store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_records(records: list[dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f'{path.suffix}.{uuid.uuid4().hex}.tmp')
    try:
        tmp.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    finally:
        with suppress(OSError):
            tmp.unlink()


def _parse_remote_spec(value: str) -> tuple[str, str | None, str]:
    text = str(value or "").strip()
    match = _REMOTE_SPEC_RE.match(text)
    if not match:
        raise ValueError("远端路径格式应为 user@host:/absolute/path")
    host = match.group("host").strip()
    user = (match.group("user") or "").strip() or None
    path = match.group("path").strip()
    if not host or not path.startswith("/"):
        raise ValueError("远端路径格式应为 user@host:/absolute/path")
    normalized_path = str(PurePosixPath(path))
    return host, user, normalized_path


def _allowed_prefixes(config: dict[str, Any]) -> tuple[str, ...]:
    configured = (
        (config.get("firmware_shares") or {}).get("allowed_prefixes")
        or config.get("firmware_share_allowed_prefixes")
        or _DEFAULT_ALLOWED_PREFIXES
    )
    prefixes = tuple(str(item).rstrip("/") + "/" for item in configured if str(item).strip())
    return prefixes or _DEFAULT_ALLOWED_PREFIXES


def _validate_remote_path(path: str, config: dict[str, Any]) -> None:
    parts = PurePosixPath(path).parts
    if ".." in parts:
        raise ValueError("远端路径不能包含 ..")
    if not any(path.startswith(prefix) for prefix in _allowed_prefixes(config)):
        raise ValueError(f"远端路径不在允许范围内: {path}")


def _safe_filename(path: str) -> str:
    name = PurePosixPath(path).name or "firmware.bin"
    return re.sub(r'[\r\n"/\\]', "_", name)


def _host_credentials(host: str, user: str | None, config: dict[str, Any]) -> dict[str, Any]:
    share_config = config.get("firmware_shares") or {}
    host_configs = share_config.get("hosts") or {}
    host_config = host_configs.get(host) or {}
    default_user = user or host_config.get("user") or config.get("firmware_share_user")
    if not default_user:
        local_server = str(config.get("local_server") or "")
        if "@" in local_server:
            default_user = local_server.rsplit("@", 1)[0]
    default_user = default_user or config.get("ubuntu_user") or "hcq"

    key_filename = host_config.get("key_filename") or share_config.get("key_filename") or config.get("firmware_share_key")
    password = (
        host_config.get("password")
        or share_config.get("password")
        or config.get("firmware_share_password")
        or config.get("firmware_share_pswd")
    )
    # ubuntu_pswd is a host-specific legacy credential. Never send it to an
    # arbitrary host supplied by an API caller.
    if not password and host == str(config.get("ubuntu_host") or ""):
        password = config.get("ubuntu_pswd")

    return {
        "hostname": host,
        "username": default_user,
        "password": password or None,
        "key_filename": os.path.expanduser(str(key_filename)) if key_filename else None,
        "port": int(host_config.get("port") or share_config.get("port") or 22),
    }


@contextmanager
def _sftp_client(host: str, user: str | None, config: dict[str, Any], password: str | None = None):
    creds = _host_credentials(host, user, config)
    # 前端临时输入的密码优先于 config 解析出的凭据。
    if password:
        creds["password"] = password
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {
        "hostname": creds["hostname"],
        "port": creds["port"],
        "username": creds["username"],
        "password": creds["password"],
        "timeout": 10,
        "banner_timeout": 10,
        "auth_timeout": 10,
        "allow_agent": True,
        "look_for_keys": not bool(creds["password"]),
    }
    if creds["key_filename"]:
        connect_kwargs["key_filename"] = creds["key_filename"]
    try:
        try:
            client.connect(**connect_kwargs)
        except paramiko.PasswordRequiredException:
            if not creds["password"]:
                raise
            connect_kwargs.pop("key_filename", None)
            connect_kwargs["allow_agent"] = False
            connect_kwargs["look_for_keys"] = False
            client.connect(**connect_kwargs)
        sftp = client.open_sftp()
        try:
            yield sftp, creds
        finally:
            with suppress(Exception):
                sftp.close()
    finally:
        with suppress(Exception):
            client.close()


def _stat_remote(host: str, user: str | None, path: str, config: dict[str, Any], password: str | None = None) -> dict[str, Any]:
    _validate_remote_path(path, config)
    try:
        with _sftp_client(host, user, config, password) as (sftp, creds):
            stat = sftp.stat(path)
            if stat.st_size is None or stat.st_size <= 0:
                raise ValueError("远端固件文件为空")
            return {
                "host": host,
                "user": creds["username"],
                "path": path,
                "filename": _safe_filename(path),
                "size": int(stat.st_size),
                "mtime": int(stat.st_mtime or 0),
            }
    except FileNotFoundError as exc:
        raise ValueError(f"远端固件不存在: {path}") from exc
    except paramiko.AuthenticationException as exc:
        raise _AuthError(f"远端认证失败（用户名/密码/密钥不匹配）: {exc}") from exc
    except (TimeoutError, ConnectionRefusedError, OSError) as exc:
        raise ValueError(f"无法连接到主机（网络不通或端口未开放）: {exc}") from exc
    except paramiko.SSHException as exc:
        raise ValueError(f"SSH连接错误: {exc}") from exc


def _list_remote_dir(host: str, user: str | None, path: str, config: dict[str, Any], password: str | None = None) -> dict[str, Any]:
    normalized_path = str(PurePosixPath(path or "/"))
    _validate_remote_path(normalized_path, config)
    try:
        with _sftp_client(host, user, config, password) as (sftp, creds):
            entries = []
            for attr in sftp.listdir_attr(normalized_path):
                name = attr.filename
                if name in {".", ".."}:
                    continue
                is_dir = bool(attr.st_mode and (attr.st_mode & 0o170000) == 0o040000)
                entries.append({
                    "name": name,
                    "type": "directory" if is_dir else "file",
                    "size": 0 if is_dir else int(attr.st_size or 0),
                    "mtime": int(attr.st_mtime or 0),
                })
            entries.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
            return {
                "host": host,
                "user": creds["username"],
                "path": normalized_path,
                "files": entries,
            }
    except FileNotFoundError as exc:
        raise ValueError(f"远端目录不存在: {normalized_path}") from exc
    except paramiko.AuthenticationException as exc:
        raise _AuthError(f"远端认证失败（用户名/密码/密钥不匹配）: {exc}") from exc
    except (TimeoutError, ConnectionRefusedError, OSError) as exc:
        raise ValueError(f"无法连接到主机（网络不通或端口未开放）: {exc}") from exc
    except paramiko.SSHException as exc:
        raise ValueError(f"SSH连接错误: {exc}") from exc


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "name",
        "host",
        "user",
        "path",
        "filename",
        "size",
        "mtime",
        "created_at",
        "created_by",
        "expires_at",
        "downloads",
        "last_downloaded_at",
    }
    public = {key: record.get(key) for key in allowed if key in record}
    public["has_password"] = bool(record.get("password"))
    return public


def _find_record(share_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    records = _load_records()
    for record in records:
        if record.get("id") == share_id:
            return records, record
    return records, None


def _parse_range(range_header: str | None, size: int) -> tuple[int, int, int] | None:
    if not range_header:
        return None
    match = _RANGE_RE.match(range_header.strip())
    if not match:
        raise HTTPException(status_code=416, detail="Invalid Range header")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise HTTPException(status_code=416, detail="Invalid Range header")
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    else:
        suffix = int(end_text)
        start = max(size - suffix, 0)
        end = size - 1
    if start >= size or end < start:
        raise HTTPException(status_code=416, detail="Range not satisfiable")
    end = min(end, size - 1)
    return start, end, end - start + 1


def _remote_file_iterator(
    host: str,
    user: str | None,
    path: str,
    config: dict[str, Any],
    start: int,
    length: int,
    password: str | None = None,
) -> Iterator[bytes]:
    with (
        _sftp_client(host, user, config, password=password) as (sftp, _creds),
        sftp.open(path, "rb") as remote_file,
    ):
        remote_file.seek(start)
        remaining = length
        while remaining > 0:
            chunk = remote_file.read(min(_DOWNLOAD_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/api/firmware-shares")
async def list_firmware_shares():
    now = int(time.time())
    records = [
        record for record in _load_records()
        if not record.get("expires_at") or int(record.get("expires_at") or 0) > now
    ]
    return success_response(data={"records": [_public_record(record) for record in records]})


@router.post("/api/firmware-shares")
async def create_firmware_share(request: Request):
    try:
        payload = await request.json()
        remote = str(payload.get("remote") or payload.get("remote_path") or "").strip()
        name = str(payload.get("name") or "").strip()
        expires_days = int(payload.get("expires_days") or 0)
        host, user, path = _parse_remote_spec(remote)
        password = payload.get("password") or None
        config = runtime.config_manager.load_config()
        info = _stat_remote(host, user, path, config, password=password)
        username = get_client_username_from_request(request)
        record = {
            "id": uuid.uuid4().hex[:12],
            "name": name or info["filename"],
            **info,
            "password": password,
            "created_at": int(time.time()),
            "created_by": username,
            "expires_at": int(time.time()) + expires_days * 86400 if expires_days > 0 else None,
            "downloads": 0,
            "last_downloaded_at": None,
        }
        with _records_lock:
            records = _load_records()
            records = [
                item
                for item in records
                if item.get("host") != host or item.get("path") != path
            ]
            records.insert(0, record)
            _save_records(records[:200])
        return success_response(data={"record": _public_record(record)}, message="固件分享已创建")
    except _AuthError as exc:
        return error_response(str(exc), 401)
    except ValueError as exc:
        return error_response(str(exc), 400)


@router.post("/api/firmware-shares/validate")
async def validate_firmware_share(request: Request):
    try:
        payload = await request.json()
        host, user, path = _parse_remote_spec(str(payload.get("remote") or "").strip())
        password = payload.get("password") or None
        config = runtime.config_manager.load_config()
        info = _stat_remote(host, user, path, config, password=password)
        return success_response(data=info)
    except _AuthError as exc:
        return error_response(str(exc), 401)
    except ValueError as exc:
        return error_response(str(exc), 400)


@router.post("/api/firmware-shares/browse")
async def browse_firmware_share_remote(request: Request):
    try:
        payload = await request.json()
        remote = str(payload.get("remote") or "").strip()
        if remote:
            host, user, path = _parse_remote_spec(remote)
        else:
            host = str(payload.get("host") or "10.10.10.206").strip()
            user = str(payload.get("user") or "hcq").strip() or None
            path = str(PurePosixPath(str(payload.get("path") or "/home/hcq")))
        password = payload.get("password") or None
        config = runtime.config_manager.load_config()
        info = _list_remote_dir(host, user, path, config, password=password)
        return success_response(data=info)
    except _AuthError as exc:
        return error_response(str(exc), 401)
    except ValueError as exc:
        return error_response(str(exc), 400)


@router.delete("/api/firmware-shares/{share_id}")
async def delete_firmware_share(share_id: str):
    with _records_lock:
        records = _load_records()
        kept = [record for record in records if record.get("id") != share_id]
        if len(kept) == len(records):
            return error_response("固件分享不存在", 404)
        _save_records(kept)
    return success_response(message="固件分享已删除")


@router.post("/api/firmware-shares/{share_id}/credentials")
async def update_firmware_share_credentials(share_id: str, request: Request):
    records, record = _find_record(share_id)
    if not record:
        return error_response("固件分享不存在", 404)
    try:
        payload = await request.json()
        password = str(payload.get("password") or "").strip()
        if not password:
            return error_response("SSH 密码不能为空", 400)
        config = runtime.config_manager.load_config()
        _stat_remote(record["host"], record.get("user"), record["path"], config, password=password)
        with _records_lock:
            records, current = _find_record(share_id)
            if not current:
                return error_response("固件分享不存在", 404)
            current["password"] = password
            _save_records(records)
        return success_response(message="远端凭据已更新")
    except _AuthError as exc:
        return error_response(str(exc), 401)
    except ValueError as exc:
        return error_response(str(exc), 400)


@router.get("/api/firmware-shares/{share_id}/check")
async def check_firmware_share_download(share_id: str):
    _records, record = _find_record(share_id)
    if not record:
        return error_response("固件分享不存在", 404)
    if record.get("expires_at") and int(record["expires_at"]) <= int(time.time()):
        return error_response("固件分享已过期", 410)
    try:
        config = runtime.config_manager.load_config()
        info = _stat_remote(record["host"], record.get("user"), record["path"], config, password=record.get("password"))
        return success_response(data=info)
    except _AuthError as exc:
        return error_response(str(exc), 401)
    except ValueError as exc:
        return error_response(str(exc), 400)


@router.get("/api/firmware-shares/{share_id}/download")
async def download_firmware_share(share_id: str, request: Request):
    records, record = _find_record(share_id)
    if not record:
        return error_response("固件分享不存在", 404)
    if record.get("expires_at") and int(record["expires_at"]) <= int(time.time()):
        return error_response("固件分享已过期", 410)

    config = runtime.config_manager.load_config()
    try:
        info = _stat_remote(record["host"], record.get("user"), record["path"], config, password=record.get("password"))
    except _AuthError as exc:
        return error_response(str(exc), 401)
    except ValueError as exc:
        return error_response(str(exc), 400)

    size = info["size"]
    range_info = _parse_range(request.headers.get("range"), size)
    if range_info:
        start, end, length = range_info
        status_code = 206
    else:
        start, end, length = 0, size - 1, size
        status_code = 200

    with _records_lock:
        records, current = _find_record(share_id)
        if current:
            current["downloads"] = int(current.get("downloads") or 0) + 1
            current["last_downloaded_at"] = int(time.time())
            current["size"] = size
            current["mtime"] = info["mtime"]
            _save_records(records)

    filename = _safe_filename(record.get("filename") or record["path"])
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "X-Firmware-Share-Id": share_id,
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    return StreamingResponse(
        _remote_file_iterator(
            record["host"],
            record.get("user"),
            record["path"],
            config,
            start,
            length,
            password=record.get("password"),
        ),
        status_code=status_code,
        media_type="application/octet-stream",
        headers=headers,
    )
