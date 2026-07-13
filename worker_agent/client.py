from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
import hashlib
from urllib.parse import quote, urlencode
from typing import Any
from pathlib import Path

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
        query = urlencode({"attempt_id": attempt_id, "artifact_type": artifact_type})
        endpoint = f"/api/cluster/jobs/{quote(job_id)}/artifacts/{quote(path.name)}?{query}"
        return self._request_with_worker_header("PUT", endpoint, path.read_bytes(), "application/octet-stream")

    def upload_transfer(self, transfer_id: str, path, chunk_size: int = 4 * 1024 * 1024):
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
        payload = json.dumps({"filename": path.name, "size_bytes": size,
                              "sha256": digest.hexdigest(), "chunk_count": count},
                             separators=(",", ":")).encode()
        return self._request_with_worker_header("POST",
            f"/api/cluster/transfers/{quote(transfer_id)}/complete", payload, "application/json")

    def _request_with_worker_header(self, method: str, path: str, body: bytes, content_type: str):
        request = urllib.request.Request(
            f"{self.config.controller_url}{path}", data=body, method=method,
            headers={"Authorization": f"Bearer {self.config.token}",
                     "X-GMS-Worker-ID": self.config.worker_id, "Content-Type": content_type},
        )
        with urllib.request.urlopen(request, timeout=60, context=self.ssl_context) as response:
            return json.loads(response.read().decode("utf-8"))
