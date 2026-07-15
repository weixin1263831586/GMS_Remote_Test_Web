from __future__ import annotations

import hashlib
import json
import math
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import WorkerConfig


class ControllerClient:
    def __init__(self, config: WorkerConfig):
        self.config = config
        # Every request is pinned to the configured Controller URL. Prefer its
        # explicit CA; deployments without a CA commonly use an internal
        # self-signed certificate and must still be able to register.
        self.ssl_context = (ssl.create_default_context(cafile=config.controller_ca)
                            if config.controller_ca else ssl._create_unverified_context())

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None,
                timeout: int = 35) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(
            f"{self.config.controller_url}{path}", data=body, method=method,
            headers={"Authorization": f"Bearer {self.config.token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.ssl_context) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"controller HTTP {exc.code}: {detail}") from exc

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/cluster/workers/register", payload)

    def download(self, path: str, destination: Path) -> None:
        request = urllib.request.Request(f"{self.config.controller_url}{path}",
            headers={"Authorization": f"Bearer {self.config.token}"})
        with urllib.request.urlopen(request, timeout=3600, context=self.ssl_context) as response, destination.open("wb") as output:
            while block := response.read(4 * 1024 * 1024):
                output.write(block)

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/cluster/workers/{self.config.worker_id}/heartbeat", payload)

    def poll(self) -> list[dict[str, Any]]:
        result = self.request("POST", f"/api/cluster/workers/{self.config.worker_id}/commands/poll", {})
        return result.get("commands", [])

    def ack(self, command_id: str, status: str, result: dict | None = None, error: str = ""):
        return self.request(
            "POST", f"/api/cluster/workers/{self.config.worker_id}/commands/{command_id}/ack",
            {"status": status, "result": result or {}, "error": error},
        )

    def events(self, job_id: str, attempt_id: str, events: list[dict[str, Any]]):
        path = f"/api/cluster/jobs/{quote(job_id)}/events"
        body = {"attempt_id": attempt_id, "events": events}
        # The events endpoint additionally binds the authenticated token to an id.
        return self._request_with_worker_header("POST", path, json.dumps(body, separators=(",", ":")).encode(),
                                                "application/json")

    def upload_artifact(self, job_id: str, attempt_id: str, path, artifact_type: str = "file"):
        path = Path(path)
        chunk_size = 4 * 1024 * 1024
        size = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(chunk_size):
                digest.update(block)
        chunk_count = max(1, math.ceil(size / chunk_size))
        init_body = json.dumps({
            "attempt_id": attempt_id,
            "filename": path.name,
            "artifact_type": artifact_type,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "chunk_size": chunk_size,
            "chunk_count": chunk_count,
        }, separators=(",", ":")).encode()
        base = f"/api/cluster/jobs/{quote(job_id)}/artifacts/uploads"
        initialized = self._request_with_worker_header(
            "POST", base, init_body, "application/json"
        )
        upload = initialized["upload"]
        uploaded = set(initialized.get("uploaded_chunks", []))
        if upload.get("status") != "completed":
            with path.open("rb") as source:
                for index in range(chunk_count):
                    block = source.read(chunk_size)
                    if index in uploaded:
                        continue
                    block_digest = hashlib.sha256(block).hexdigest()
                    self._request_with_worker_header(
                        "PUT",
                        f"{base}/{quote(upload['id'])}/chunks/{index}",
                        block,
                        "application/octet-stream",
                        timeout=300,
                        extra_headers={"X-Chunk-SHA256": block_digest},
                    )
        complete = json.dumps({"chunk_count": chunk_count}, separators=(",", ":")).encode()
        return self._request_with_worker_header(
            "POST", f"{base}/{quote(upload['id'])}/complete", complete,
            "application/json", timeout=3600,
        )

    def upload_transfer(
        self,
        transfer_id: str,
        path,
        chunk_size: int = 4 * 1024 * 1024,
        filename: str = "",
    ):
        digest = hashlib.sha256()
        size = 0
        count = 0
        with path.open("rb") as source:
            while True:
                block = source.read(chunk_size)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                endpoint = f"/api/cluster/transfers/{quote(transfer_id)}/chunks/{count}"
                self._request_with_worker_header("PUT", endpoint, block, "application/octet-stream")
                count += 1
        if count == 0:
            # The transport requires non-empty chunks; represent an empty file
            # with one byte is incorrect, so reject it explicitly.
            raise ValueError("cannot upload an empty transfer")
        payload = json.dumps({"filename": filename or path.name, "size_bytes": size,
                              "sha256": digest.hexdigest(), "chunk_count": count},
                             separators=(",", ":")).encode()
        return self._request_with_worker_header("POST",
            f"/api/cluster/transfers/{quote(transfer_id)}/complete", payload, "application/json")

    def _request_with_worker_header(
        self,
        method: str,
        path: str,
        body,
        content_type: str,
        timeout: int = 60,
        extra_headers: dict[str, str] | None = None,
    ):
        headers = {"Authorization": f"Bearer {self.config.token}",
                   "X-GMS-Worker-ID": self.config.worker_id,
                   "Content-Type": content_type}
        headers.update(extra_headers or {})
        request = urllib.request.Request(
            f"{self.config.controller_url}{path}", data=body, method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout,
                context=self.ssl_context,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"controller HTTP {exc.code}: {detail}") from exc
