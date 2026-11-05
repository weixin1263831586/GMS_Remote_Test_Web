"""AOA 触摸标定记录的持久化存储与复用匹配（ADR 0026）。

录制返回的 ``touch_calibration`` 此前只存在前端 ``aoaPanelState`` 临时
状态里，同一设备重连/重开页面就丢失，无法支撑"userdebug 标定 → 刷
user build → 匹配标定记录 → 回放"流程。本模块按 ADR 0026 增加独立的
``aoa_calibrations`` 表：

- 每条记录绑定 owner（ADR 0010 账号分区语义）与匹配键（model +
  Android release + display + rotation + hid_touch_mode）；
- ``TouchMapping`` 全量 JSON 持久化（矩阵/偏移/采样/误差/policy）；
- ``find_matching`` 只在环境完全一致且 ``verified`` 时复用——旋转、
  固件、显示配置或 HID 模式任一变化都必须重新标定；
- 进程安全：SQLite ``BEGIN IMMEDIATE`` 写锁 + ``PRAGMA user_version``
  幂等迁移（architecture hard rules）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from foundation.aoa_calibration import TouchMapping, context_matches
from foundation.error_model import ApiError
from foundation.sqlite_schema import ensure_schema


class AoaCalibrationStore:
    """SQLite 存储的 AOA 触摸标定记录（owner 隔离、进程安全）。"""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = root / "aoa_calibrations.sqlite3"
        self._lock = threading.Lock()
        self.connect().close()

    @staticmethod
    def _initialize_schema(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS aoa_calibrations (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                match_key TEXT NOT NULL, hid_touch_mode TEXT NOT NULL,
                mapping_policy TEXT NOT NULL DEFAULT '',
                verified INTEGER NOT NULL DEFAULT 0,
                display TEXT NOT NULL DEFAULT '[0, 0]',
                mapping TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aoa_calibrations_owner "
            "ON aoa_calibrations(owner_id, match_key)")
        conn.execute("PRAGMA user_version = 1")

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            ensure_schema(conn, self.db, self._initialize_schema)
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def match_key(mapping: TouchMapping) -> str:
        """复用匹配键：环境快照各字段按稳定顺序拼接。"""
        context = mapping.context or {}
        display = "|".join(str(value) for value in
                           (context.get("display") or mapping.display_pixels or [0, 0]))
        return "|".join(str(context.get(key) or "") for key in
                        ("model", "release", "fingerprint", "density",
                         "rotation", "hid_touch_mode", "kernel", "display_route")) + f"|{display}"

    @staticmethod
    def _decode(row) -> dict[str, Any]:
        return {
            "id": row["id"], "owner_id": row["owner_id"],
            "match_key": row["match_key"], "hid_touch_mode": row["hid_touch_mode"],
            "mapping_policy": row["mapping_policy"], "verified": bool(row["verified"]),
            "display": json.loads(row["display"] or "[0, 0]"),
            "mapping": json.loads(row["mapping"]),
            "created_at": row["created_at"],
        }

    def save_calibration(self, owner_id: str, mapping: TouchMapping,
                         label: str = "") -> dict[str, Any]:
        """保存一条标定记录（同 owner + 匹配键的旧记录被替换）。"""
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ApiError.invalid_semantics("AOA calibration requires an owner")
        try:
            mapping = TouchMapping.from_dict(mapping.to_dict())
            context = mapping.context
            if (not context.get("model") or not context.get("release")
                    or type(context.get("rotation")) is not int
                    or context["rotation"] not in range(4)
                    or context.get("hid_touch_mode") != mapping.hid_touch_mode
                    or context.get("display") != list(mapping.display_pixels)):
                raise ValueError("AOA calibration requires a complete, consistent device context")
        except ValueError as exc:
            raise ApiError.invalid_semantics(str(exc)) from exc
        if label and len(label) > 256:
            raise ApiError.invalid_semantics("AOA calibration label exceeds 256 characters")
        key = self.match_key(mapping)
        payload = mapping.to_dict()
        payload["label"] = label
        if len(json.dumps(payload)) > 65536:
            raise ApiError.invalid_semantics("AOA calibration payload exceeds 65536 bytes")
        calibration_id = uuid.uuid4().hex
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM aoa_calibrations WHERE owner_id=? AND match_key=?",
                         (owner_id, key))
            conn.execute(
                "INSERT INTO aoa_calibrations (id, owner_id, match_key, hid_touch_mode,"
                " mapping_policy, verified, display, mapping, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (calibration_id, owner_id, key, mapping.hid_touch_mode,
                 mapping.effective_policy, 1 if mapping.verified else 0,
                 json.dumps(list(mapping.display_pixels)),
                 json.dumps(payload, ensure_ascii=False), now))
            # Capture this write's receipt before another process can replace
            # the same owner's matching record after the transaction commits.
            row = conn.execute("SELECT * FROM aoa_calibrations WHERE id=?", (calibration_id,)).fetchone()
        return self._decode(row)

    def list_calibrations(self, owner_id: str, include_all: bool = False) -> list[dict[str, Any]]:
        where = "" if include_all else "WHERE owner_id=?"
        values = () if include_all else (owner_id,)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM aoa_calibrations {where}"
                " ORDER BY created_at DESC, id", values).fetchall()
        return [self._decode(row) for row in rows]

    def get_calibration(self, calibration_id: str, owner_id: str,
                        include_all: bool = False) -> dict[str, Any]:
        where = ("WHERE id=? " if include_all else "WHERE id=? AND owner_id=? ")
        values = (calibration_id,) if include_all else (calibration_id, owner_id)
        with self.connect() as conn:
            row = conn.execute(f"SELECT * FROM aoa_calibrations {where}", values).fetchone()
        if row is None:
            raise ApiError.not_found("AOA 标定记录不存在")
        return self._decode(row)

    def delete_calibration(self, calibration_id: str, owner_id: str) -> None:
        self.get_calibration(calibration_id, owner_id)
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM aoa_calibrations WHERE id=? AND owner_id=?",
                         (calibration_id, owner_id))

    def find_matching(self, owner_id: str, query_context: dict) -> dict[str, Any] | None:
        """返回与设备环境匹配且已验证的标定记录；无匹配返回 None。

        环境键（model/release/display/rotation/hid_touch_mode）不一致、
        记录未通过误差验证或 policy 为 unsupported 时都不复用——回放侧
        只能降级为按轴估算并标记 unverified。
        """
        query_touch_mode = str(query_context.get("hid_touch_mode") or "")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM aoa_calibrations WHERE owner_id=? AND verified=1"
                " ORDER BY created_at DESC, id", (owner_id,)).fetchall()
        for row in rows:
            record = self._decode(row)
            try:
                mapping = TouchMapping.from_dict(record["mapping"])
            except ValueError:
                continue  # Old or incomplete records require fresh verification.
            if record["hid_touch_mode"] != query_touch_mode:
                continue
            if not mapping.verified:
                continue
            if context_matches(mapping.context, query_context):
                return record
        return None


_stores: dict[str, AoaCalibrationStore] = {}
_store_lock = threading.Lock()


def calibration_store() -> AoaCalibrationStore:
    """按 Controller 数据根复用的单例（与 profile_store 同款缓存模式）。"""
    from features.devices import deployment_service

    root = deployment_service.store().root
    with _store_lock:
        if root not in _stores or not _stores[root].db.exists():
            _stores[root] = AoaCalibrationStore(root)
        return _stores[root]
