from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse

from features.auth import (
    CurrentUser,
    authentication_required,
    get_authenticated_user,
    require_authenticated_user,
    require_permission_when_auth_required,
)
from features.users import owner_id_from_request

from .api import _authenticate, _require_cluster_enabled, service
from .config import configured_max_bytes
from .execution_spec import (
    build_argv_from_spec,
    build_default_argv,
    canonicalize_execution_spec,
)
from .models import ArtifactUploadComplete, ArtifactUploadInit, ClusterJobCreate, JobEventBatch
from .report_index import index_cluster_report
from .repository import utc_now


router = APIRouter()


def _request_owner_id(request: Request) -> str:
    user = get_authenticated_user(request)
    if user:
        return user.id
    if authentication_required():
        return require_authenticated_user(request).id
    return owner_id_from_request(request)


def _request_owner_username(request: Request) -> str:
    user = get_authenticated_user(request)
    if user:
        return user.username
    if authentication_required():
        return require_authenticated_user(request).username
    return ""


def _require_job_access(request: Request, job: dict) -> None:
    user = get_authenticated_user(request)
    if user is None:
        if authentication_required():
            require_authenticated_user(request)
        return
    if user.role != "admin" and str(job.get("owner_id") or "") != user.id:
        raise HTTPException(404, "job not found")


def _job_response(job: dict) -> dict:
    """Add a user-facing client identity while preserving access ownership."""
    from features.users import resolve_client_display_id

    item = dict(job or {})
    item["client_display_id"] = resolve_client_display_id(
        str(item.get("owner_id") or "")
    )
    return item


@router.post("/jobs")
def create_job(
    body: ClusterJobCreate,
    request: Request,
    _actor: CurrentUser | None = Depends(
        require_permission_when_auth_required("tests.execute")
    ),
):
    # argv 是服务端从 execution_spec 派生的数据，不是浏览器可提交的输入；
    # 接受 raw argv 会让 ExecutionSpec 校验（test_type/suite/设备绑定）被绕过。
    if body.argv:
        raise HTTPException(
            400,
            "raw argv is not accepted from browsers; supply execution_spec",
        )
    local_worker_id = service().config.local_worker_id
    _require_cluster_enabled(
        remote=body.worker_id not in {"auto", local_worker_id}
    )
    if (
        body.worker_id == local_worker_id
        and not service().has_command_agent(local_worker_id)
    ):
        raise HTTPException(503, "local Worker Agent is offline")
    data = body.model_dump()
    data["trace_id"] = str(getattr(request.state, "trace_id", "") or "")
    # 所有者必须取自认证账户，不接受浏览器传入值。
    data["owner_id"] = _request_owner_id(request)
    data["owner_username"] = _request_owner_username(request)
    if data["worker_id"] == "auto":
        try:
            data["worker_id"], selected_devices = service().select_worker(
                data["suite_key"],
                data["device_count"],
                require_agent=True,
            )
            if not data["devices"]:
                data["devices"] = selected_devices
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    spec = data.get("execution_spec")
    if spec and spec.get("test_type"):
        requested_path = str(spec.get("suite_path") or "")
        if data.get("suite_path") and data["suite_path"] != requested_path:
            raise HTTPException(
                409,
                "execution_spec suite_path does not match the job suite_path",
            )
        suites = [
            item
            for item in service().repository.list_suites(data["worker_id"])
            if item["tools_path"] == requested_path and item["available"]
        ]
        if data.get("suite_key"):
            suites = [
                item for item in suites if item["suite_key"] == data["suite_key"]
            ]
        if not suites:
            raise HTTPException(409, "suite is not available on worker")
        selected_suite = suites[0]
        data["suite_path"] = selected_suite["tools_path"]
        data["suite_key"] = selected_suite["suite_key"]
        data["execution_spec"] = canonicalize_execution_spec(
            spec,
            suite_path=data["suite_path"],
            suite_type=selected_suite["suite_type"],
            devices=data["devices"],
            worker_id=data["worker_id"],
        )
        data["argv"] = build_argv_from_spec(data["execution_spec"])
    else:
        (
            data["argv"],
            data["suite_path"],
            data["suite_key"],
        ) = build_default_argv(
            suite_path=data["suite_path"],
            suite_key=data["suite_key"],
            worker_id=data["worker_id"],
            local_worker_id=local_worker_id,
            available_suites=service().repository.list_suites(data["worker_id"]),
        )
    from features.devices import incompatible_test_devices

    inventory = {
        str(item.get("serial") or ""): item
        for item in service().repository.list_devices(data["worker_id"])
    }
    selected_inventory = []
    for device_id in data["devices"]:
        serial = str(device_id or "")
        prefix = f"{data['worker_id']}:"
        if serial.startswith(prefix):
            serial = serial[len(prefix):]
        if serial in inventory:
            selected_inventory.append(inventory[serial])
    incompatible, policy = incompatible_test_devices(
        selected_inventory,
        data["argv"],
        data.get("env") or {},
    )
    if incompatible:
        raise HTTPException(
            409,
            f"所选测试需要真实USB/Fastboot通道，不能使用ADB Proxy设备: "
            f"{', '.join(incompatible)}；请改用USB/IP或在设备来源Worker本地执行。"
            f"原因：{policy['reason']}",
        )
    try:
        job = service().repository.create_job_with_leases(data)
        request.state.device_lease_tokens = [
            {
                "lease_id": lease["id"],
                "device_id": lease["device_id"],
                "generation": lease["generation"],
                "attempt_id": lease["attempt_id"],
                "owner_id": data["owner_id"],
            }
            for lease in job.get("leases") or []
            if lease.get("status") == "active"
        ]
        command = service().repository.create_command({
            "worker_id": data["worker_id"],
            "command_type": "start_test",
            "job_id": job["id"],
            "attempt_id": job["current_attempt_id"],
            "operation_id": f"{job['current_attempt_id']}:start_test",
            "payload": {
                "worker_job_id": f"wj-{job['id']}",
                "argv": data["argv"],
                "execution_spec": data.get("execution_spec"),
                "env": data["env"],
                "devices": data["devices"],
                "trace_id": job.get("trace_id", ""),
                "lease_tokens": [
                    {
                        "lease_id": lease["id"],
                        "device_id": lease["device_id"],
                        "generation": lease["generation"],
                        "attempt_id": lease["attempt_id"],
                    }
                    for lease in job.get("leases") or []
                    if lease.get("status") == "active"
                ],
            },
        })
        service().repository.attach_command_to_job(job["id"], command)
        return {
            "success": True,
            "job": _job_response(service().repository.get_job(job["id"]) or {}),
            "command": command,
        }
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/jobs")
def list_jobs(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    user = get_authenticated_user(request)
    if user is None and authentication_required():
        user = require_authenticated_user(request)
    owner_id = user.id if user and user.role != "admin" else ""
    return {
        "success": True,
        "jobs": [
            _job_response(item)
            for item in service().repository.list_jobs(limit, owner_id=owner_id)
        ],
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    _require_job_access(request, job)
    return {"success": True, "job": _job_response(job)}


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str, request: Request):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    _require_job_access(request, job)
    if job["status"] not in {"completed", "failed", "cancelled"}:
        raise HTTPException(409, "only completed history can be deleted")
    if not service().repository.delete_job(job_id):
        raise HTTPException(409, "job could not be deleted")
    return {"success": True, "deleted": job_id}


@router.post("/jobs/{job_id}/events")
def add_job_events(
    job_id: str,
    body: JobEventBatch,
    worker_id: str = Header(alias="X-GMS-Worker-ID"),
    authorization: str | None = Header(default=None),
):
    _authenticate(worker_id, authorization)
    job = service().repository.get_job(job_id)
    if (
        not job
        or job["assigned_worker_id"] != worker_id
        or job["current_attempt_id"] != body.attempt_id
    ):
        raise HTTPException(404, "job attempt not found for worker")
    inserted = service().repository.add_events(
        job_id,
        body.attempt_id,
        [item.model_dump() for item in body.events],
    )
    return {"success": True, "inserted": inserted}


@router.get("/jobs/{job_id}/events")
def list_job_events(
    job_id: str,
    request: Request,
    after: int = Query(default=-1),
    limit: int = Query(default=500, le=2000),
):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    _require_job_access(request, job)
    return {
        "success": True,
        "events": service().repository.list_events(job_id, after, limit),
    }


def _artifact_root() -> Path:
    return service().repository.db_path.parent / "artifacts"


def _artifact_upload_root() -> Path:
    return service().repository.db_path.parent / "artifact-uploads"


def _artifact_job(job_id: str, attempt_id: str, worker_id: str) -> dict:
    job = service().repository.get_job(job_id)
    if (not job or job["assigned_worker_id"] != worker_id
            or job["current_attempt_id"] != attempt_id):
        raise HTTPException(404, "job attempt not found for worker")
    return job


def _safe_artifact_name(filename: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
    if not safe_name:
        raise HTTPException(400, "invalid filename")
    return safe_name


def _artifact_upload(upload_id: str, worker_id: str) -> dict:
    with service().repository.connect() as conn:
        row = conn.execute(
            "SELECT * FROM cluster_artifact_uploads WHERE id=? AND worker_id=?",
            (upload_id, worker_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "artifact upload not found for worker")
    return dict(row)


def _uploaded_chunk_indexes(upload_id: str) -> list[int]:
    chunk_dir = _artifact_upload_root() / upload_id / "chunks"
    result = []
    for path in chunk_dir.glob("*.part") if chunk_dir.is_dir() else []:
        try:
            result.append(int(path.stem))
        except ValueError:
            continue
    return sorted(result)


@router.post("/jobs/{job_id}/artifacts/uploads")
def init_artifact_upload(
    job_id: str,
    body: ArtifactUploadInit,
    worker_id: str = Header(alias="X-GMS-Worker-ID"),
    authorization: str | None = Header(default=None),
):
    _authenticate(worker_id, authorization)
    _artifact_job(job_id, body.attempt_id, worker_id)
    safe_name = _safe_artifact_name(body.filename)
    max_bytes = configured_max_bytes(
        "GMS_CLUSTER_ARTIFACT_MAX_BYTES", service().config.artifact_max_bytes
    )
    if body.size_bytes > max_bytes:
        raise HTTPException(413, "artifact is too large")
    expected_count = max(1, (body.size_bytes + body.chunk_size - 1) // body.chunk_size)
    if body.chunk_count != expected_count:
        raise HTTPException(400, "artifact chunk count does not match its declared size")
    now = utc_now()
    with service().repository.connect() as conn:
        existing = conn.execute(
            "SELECT * FROM cluster_artifact_uploads WHERE attempt_id=? AND filename=?",
            (body.attempt_id, safe_name),
        ).fetchone()
        if existing is not None:
            current = dict(existing)
            same = (current["worker_id"] == worker_id
                    and current["size_bytes"] == body.size_bytes
                    and current["sha256"] == body.sha256
                    and current["chunk_size"] == body.chunk_size
                    and current["chunk_count"] == body.chunk_count)
            if same:
                return {"success": True, "upload": current,
                        "uploaded_chunks": _uploaded_chunk_indexes(current["id"])}
            shutil.rmtree(_artifact_upload_root() / current["id"], ignore_errors=True)
            conn.execute("DELETE FROM cluster_artifact_uploads WHERE id=?", (current["id"],))
        upload_id = f"upload-{uuid.uuid4().hex}"
        conn.execute("""INSERT INTO cluster_artifact_uploads
            (id,job_id,attempt_id,worker_id,filename,artifact_type,size_bytes,sha256,
             chunk_size,chunk_count,status,created_at,updated_at,completed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,'uploading',?,?,'')""",
            (upload_id, job_id, body.attempt_id, worker_id, safe_name,
             body.artifact_type, body.size_bytes, body.sha256, body.chunk_size,
             body.chunk_count, now, now))
    upload = _artifact_upload(upload_id, worker_id)
    return {"success": True, "upload": upload, "uploaded_chunks": []}


@router.get("/jobs/{job_id}/artifacts/uploads/{upload_id}")
def get_artifact_upload(
    job_id: str,
    upload_id: str,
    worker_id: str = Header(alias="X-GMS-Worker-ID"),
    authorization: str | None = Header(default=None),
):
    _authenticate(worker_id, authorization)
    upload = _artifact_upload(upload_id, worker_id)
    if upload["job_id"] != job_id:
        raise HTTPException(404, "artifact upload not found for job")
    return {"success": True, "upload": upload,
            "uploaded_chunks": _uploaded_chunk_indexes(upload_id)}


@router.put("/jobs/{job_id}/artifacts/uploads/{upload_id}/chunks/{index}")
async def upload_artifact_chunk(
    job_id: str,
    upload_id: str,
    index: int,
    request: Request,
    worker_id: str = Header(alias="X-GMS-Worker-ID"),
    authorization: str | None = Header(default=None),
    chunk_sha256: str = Header(alias="X-Chunk-SHA256"),
):
    _authenticate(worker_id, authorization)
    upload = _artifact_upload(upload_id, worker_id)
    if upload["job_id"] != job_id or upload["status"] != "uploading":
        raise HTTPException(409, "artifact upload no longer accepts chunks")
    if index < 0 or index >= int(upload["chunk_count"]):
        raise HTTPException(400, "invalid artifact chunk index")
    expected_size = int(upload["chunk_size"])
    if index == int(upload["chunk_count"]) - 1:
        expected_size = int(upload["size_bytes"]) - index * int(upload["chunk_size"])
    chunk_dir = _artifact_upload_root() / upload_id / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    destination = chunk_dir / f"{index:08d}.part"
    temporary = chunk_dir / f".{index:08d}.{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    total = 0
    try:
        with temporary.open("wb") as output:
            async for block in request.stream():
                total += len(block)
                if total > expected_size:
                    raise HTTPException(413, "artifact chunk is too large")
                digest.update(block)
                output.write(block)
        if total != expected_size or digest.hexdigest() != chunk_sha256:
            raise HTTPException(409, "artifact chunk checksum or size mismatch")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    with service().repository.connect() as conn:
        conn.execute("UPDATE cluster_artifact_uploads SET updated_at=? WHERE id=?",
                     (utc_now(), upload_id))
    return {"success": True, "index": index, "size_bytes": total}


@router.post("/jobs/{job_id}/artifacts/uploads/{upload_id}/complete")
def complete_artifact_upload(
    job_id: str,
    upload_id: str,
    body: ArtifactUploadComplete,
    worker_id: str = Header(alias="X-GMS-Worker-ID"),
    authorization: str | None = Header(default=None),
):
    _authenticate(worker_id, authorization)
    upload = _artifact_upload(upload_id, worker_id)
    if upload["job_id"] != job_id:
        raise HTTPException(404, "artifact upload not found for job")
    if upload["status"] == "completed":
        artifacts = [item for item in service().repository.list_artifacts(job_id)
                     if item["attempt_id"] == upload["attempt_id"]
                     and item["filename"] == upload["filename"]]
        return {"success": True, "artifact": artifacts[0] if artifacts else None,
                "already_completed": True}
    if body.chunk_count != int(upload["chunk_count"]):
        raise HTTPException(409, "artifact chunks are incomplete")
    chunks = [_artifact_upload_root() / upload_id / "chunks" / f"{index:08d}.part"
              for index in range(body.chunk_count)]
    if not all(path.is_file() for path in chunks):
        raise HTTPException(409, "artifact chunks are incomplete")
    destination_dir = _artifact_root() / job_id / upload["attempt_id"]
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / upload["filename"]
    temporary = destination_dir / f".{upload['filename']}.{upload_id}.tmp"
    digest = hashlib.sha256()
    total = 0
    try:
        with temporary.open("wb") as output:
            for chunk in chunks:
                with chunk.open("rb") as source:
                    while block := source.read(4 * 1024 * 1024):
                        total += len(block)
                        digest.update(block)
                        output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if total != int(upload["size_bytes"]) or digest.hexdigest() != upload["sha256"]:
            raise HTTPException(409, "artifact checksum or size mismatch")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    artifact = service().repository.record_artifact({
        "job_id": job_id, "attempt_id": upload["attempt_id"],
        "worker_id": worker_id, "filename": upload["filename"],
        "relative_path": str(destination.relative_to(_artifact_root())),
        "artifact_type": upload["artifact_type"], "size_bytes": total,
        "sha256": upload["sha256"],
    })
    completed_at = utc_now()
    with service().repository.connect() as conn:
        conn.execute("""UPDATE cluster_artifact_uploads
            SET status='completed',updated_at=?,completed_at=? WHERE id=?""",
            (completed_at, completed_at, upload_id))
    shutil.rmtree(_artifact_upload_root() / upload_id, ignore_errors=True)
    if str(upload["artifact_type"]).startswith("report"):
        index_cluster_report(_artifact_job(job_id, upload["attempt_id"], worker_id),
                             destination_dir, artifact)
    return {"success": True, "artifact": artifact}


@router.put("/jobs/{job_id}/artifacts/{filename}")
async def upload_artifact(
    job_id: str,
    filename: str,
    request: Request,
    attempt_id: str = Query(...),
    artifact_type: str = Query(default="file"),
    worker_id: str = Header(alias="X-GMS-Worker-ID"),
    authorization: str | None = Header(default=None),
):
    _authenticate(worker_id, authorization)
    job = service().repository.get_job(job_id)
    if (
        not job
        or job["assigned_worker_id"] != worker_id
        or job["current_attempt_id"] != attempt_id
    ):
        raise HTTPException(404, "job attempt not found for worker")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
    if not safe_name:
        raise HTTPException(400, "invalid filename")
    max_bytes = configured_max_bytes(
        "GMS_CLUSTER_ARTIFACT_MAX_BYTES", service().config.artifact_max_bytes
    )
    destination_dir = _artifact_root() / job_id / attempt_id
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / safe_name
    temporary = destination_dir / f".{safe_name}.{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    total = 0
    try:
        with temporary.open("wb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, "artifact is too large")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    artifact = service().repository.record_artifact({
        "job_id": job_id,
        "attempt_id": attempt_id,
        "worker_id": worker_id,
        "filename": safe_name,
        "relative_path": str(destination.relative_to(_artifact_root())),
        "artifact_type": artifact_type,
        "size_bytes": total,
        "sha256": digest.hexdigest(),
    })
    if artifact_type.startswith("report"):
        index_cluster_report(job, destination_dir, artifact)
    return {"success": True, "artifact": artifact}


@router.get("/jobs/{job_id}/artifacts")
def list_artifacts(job_id: str, request: Request):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    _require_job_access(request, job)
    return {
        "success": True,
        "artifacts": service().repository.list_artifacts(job_id),
    }


@router.get("/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_artifact(job_id: str, artifact_id: str, request: Request):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    _require_job_access(request, job)
    artifacts = [
        item
        for item in service().repository.list_artifacts(job_id)
        if item["id"] == artifact_id
    ]
    if not artifacts:
        raise HTTPException(404, "artifact not found")
    artifact = artifacts[0]
    path = (_artifact_root() / artifact["relative_path"]).resolve()
    if not path.is_relative_to(_artifact_root().resolve()) or not path.is_file():
        raise HTTPException(404, "artifact file not found")
    return FileResponse(path, filename=artifact["filename"])
