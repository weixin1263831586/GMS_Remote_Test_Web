"""Account-owned class certificates issued from completed, fenced Worker receipts."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from foundation.aoa_certification import build_certificate, validate_certificate
from foundation.error_model import ApiError
from foundation.sqlite_schema import ensure_schema


class AoaCertificationStore:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = root / "aoa_class_certificates.sqlite3"
        self.connect().close()

    @staticmethod
    def initialize(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS certificates (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, label TEXT NOT NULL,
            certificate TEXT NOT NULL, command_ids TEXT NOT NULL,
            created_at TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT '')""")
        conn.execute("CREATE INDEX IF NOT EXISTS cert_owner ON certificates(owner_id)")
        conn.execute("PRAGMA user_version=1")

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            ensure_schema(conn, self.db, self.initialize)
        except Exception:
            conn.close()
            raise
        return conn

    def issue(self, owner_id, label, command_ids, repository):
        if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 128
                or not isinstance(command_ids, list) or not 2 <= len(command_ids) <= 16
                or any(not isinstance(value, str) or not value for value in command_ids)
                or len(set(command_ids)) != len(command_ids)):
            raise ApiError.invalid_semantics("类别认证需要名称和 2..16 个独立参考验证回执")
        evidence = []
        for command_id in command_ids:
            command = repository.get_command(command_id) or {}
            payload, result = command.get("payload") or {}, command.get("result") or {}
            if (payload.get("owner_id") != owner_id or command.get("status") != "completed"
                    or command.get("command_type") != "device_action" or payload.get("action") != "aoa_record_input"
                    or (payload.get("aoa_record") or {}).get("action") != "verify_direct"
                    or result.get("success") is not True or not payload.get("lease_tokens")):
                raise ApiError.conflict("参考验证回执无效、未完成或不属于当前账号")
            item = result.get("direct_reference_evidence")
            prefix = f"{command.get('worker_id')}:"
            devices = payload.get("devices") or []
            if (not isinstance(item, dict) or len(devices) != 1
                    or devices[0].removeprefix(prefix) != item.get("serial")
                    or payload.get("usb_port_path") != item.get("usb_port_path")):
                raise ApiError.conflict("参考验证证据与原设备、物理端口不一致")
            evidence.append(item)
        certificate_id = uuid.uuid4().hex
        try:
            certificate = build_certificate(evidence, certificate_id=certificate_id,
                                            owner_id=owner_id, label=label.strip())
        except (ValueError, TypeError) as exc:
            raise ApiError.invalid_semantics(str(exc)) from exc
        with self.connect() as conn:
            conn.execute("INSERT INTO certificates VALUES (?,?,?,?,?,?,?)", (
                certificate_id, owner_id, label.strip(), json.dumps(certificate), json.dumps(command_ids),
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), ""))
        return certificate

    def get(self, certificate_id, owner_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM certificates WHERE id=? AND owner_id=? AND revoked_at=''",
                               (certificate_id, owner_id)).fetchone()
        if row is None:
            raise ApiError.not_found("类别认证不存在、不属于当前账号或已撤销")
        certificate = json.loads(row["certificate"])
        try:
            validate_certificate(certificate)
        except (ValueError, TypeError) as exc:
            raise ApiError.conflict("类别认证与当前 HID 实现不兼容，请重新验证") from exc
        return certificate

    def list(self, owner_id):
        with self.connect() as conn:
            rows = conn.execute("SELECT id,label,created_at,revoked_at,command_ids FROM certificates WHERE owner_id=? ORDER BY created_at,id",
                                (owner_id,)).fetchall()
        return [{**dict(row), "command_ids": json.loads(row["command_ids"])} for row in rows]

    def revoke(self, certificate_id, owner_id):
        with self.connect() as conn:
            cursor = conn.execute("UPDATE certificates SET revoked_at=? WHERE id=? AND owner_id=? AND revoked_at=''",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), certificate_id, owner_id))
            if cursor.rowcount != 1:
                raise ApiError.not_found("类别认证不存在或已撤销")


def certification_store():
    from . import deployment_service

    return AoaCertificationStore(deployment_service.store().root)
