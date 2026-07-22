"""Persistence mixin for cluster file-transfer state."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClusterTransferRepositoryMixin:
    def create_transfer(
        self,
        worker_id: str,
        transfer_type: str = "suite_export",
        owner_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transfer_id = f"transfer-{uuid.uuid4().hex}"
        now = _utc_now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO cluster_transfers
                (id,worker_id,transfer_type,owner_id,metadata_json,status,filename,
                 relative_path,size_bytes,sha256,error,created_at,updated_at,completed_at)
                VALUES(?,?,?,?,?,'created','','',0,'','',?,?, '')""",
                (
                    transfer_id, worker_id, transfer_type, owner_id,
                    json.dumps(metadata or {}, separators=(",", ":")), now, now,
                ),
            )
        return self.get_transfer(transfer_id) or {}

    def get_transfer(self, transfer_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM cluster_transfers WHERE id=?", (transfer_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            result["metadata"] = {}
        return result

    def update_transfer(
        self,
        transfer_id: str,
        **values: Any,
    ) -> dict[str, Any] | None:
        allowed = {
            "status",
            "filename",
            "relative_path",
            "size_bytes",
            "sha256",
            "error",
            "completed_at",
            "owner_id",
            "metadata_json",
        }
        fields = [(key, value) for key, value in values.items() if key in allowed]
        if fields:
            assignments = ",".join(f"{key}=?" for key, _ in fields)
            with self.connect() as conn:
                conn.execute(
                    f"UPDATE cluster_transfers SET {assignments},updated_at=? WHERE id=?",
                    [value for _, value in fields] + [_utc_now(), transfer_id],
                )
        return self.get_transfer(transfer_id)
