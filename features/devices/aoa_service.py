"""AOA 自动恢复：Profile 存储、校验与操作编排（ADR 0022）。

Profile 是针对不同 Android 版本/产品形态（手机触屏、TV/STB 遥控）预设的
AOA 命令序列，例如 user build 烧写后"进入设置 → 开发者选项 → 打开 USB
调试"。存储 owner 隔离（同平台报告的 ADR 0010 账号分区语义）。

核心操作：
- ``probe``：检测设备 AOAv2 能力（GET_PROTOCOL ≥ 2）；
- ``run-profile``：执行预设序列（aoa_sequence device_action）；
- ``recover-adb``：执行序列并等待 ADB 恢复（device 状态）。

AOA 执行成功 ≠ ADB 已恢复：unauthorized 需要人工在设备上确认授权弹窗
（结果里如实带回 adb_state，由调用方决定后续）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from foundation import aoa_profile
from foundation.error_model import ApiError
from foundation.sqlite_schema import ensure_schema


class AoaProfileStore:
    """SQLite 存储的 AOA Profile（owner 隔离、进程安全）。"""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = root / "aoa_profiles.sqlite3"
        self._lock = threading.Lock()
        self.connect().close()

    @staticmethod
    def _initialize_schema(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS aoa_profiles (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                commands TEXT NOT NULL, wait_adb_seconds INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(owner_id, name)
            )""")
        conn.execute('PRAGMA user_version = 1')

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
    def _decode(row) -> dict[str, Any]:
        return {
            "id": row["id"], "owner_id": row["owner_id"], "name": row["name"],
            "description": row["description"],
            "commands": json.loads(row["commands"]),
            "wait_adb_seconds": int(row["wait_adb_seconds"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def list_profiles(self, owner_id: str, include_all: bool = False) -> list[dict[str, Any]]:
        where = "" if include_all else "WHERE owner_id=?"
        values = () if include_all else (owner_id,)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM aoa_profiles {where} ORDER BY name", values).fetchall()
        return [self._decode(row) for row in rows]

    def get_profile(self, profile_id: str, owner_id: str, include_all: bool = False) -> dict[str, Any]:
        row = self._fetch(profile_id, owner_id, include_all)
        if row is None:
            raise ApiError.not_found("AOA Profile 不存在")
        return self._decode(row)

    def _fetch(self, profile_id: str, owner_id: str, include_all: bool):
        where = ("WHERE id=? " if include_all
                 else "WHERE id=? AND owner_id=? ")
        values = (profile_id,) if include_all else (profile_id, owner_id)
        with self.connect() as conn:
            return conn.execute(
                f"SELECT * FROM aoa_profiles {where}", values).fetchone()

    @staticmethod
    def validate_commands(commands: list[str]) -> list[dict[str, Any]]:
        """解析校验命令序列，返回解析摘要（供保存与执行前校验复用）。"""
        parsed = aoa_profile.parse_profile_commands(commands)
        return [{"kind": item.kind} for item in parsed]

    def create_profile(self, owner_id: str, name: str, description: str,
                       commands: list[str], wait_adb_seconds: int = 0) -> dict[str, Any]:
        commands = [str(item or "").strip() for item in commands or [] if str(item or "").strip()]
        self.validate_commands(commands)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        profile_id = uuid.uuid4().hex
        with self._lock, self.connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO aoa_profiles (id,owner_id,name,description,commands,"
                    "wait_adb_seconds,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (profile_id, owner_id, name, description,
                     json.dumps(commands, ensure_ascii=False),
                     max(0, min(int(wait_adb_seconds or 0), 600)),
                     now, now))
            except sqlite3.IntegrityError as exc:
                raise ApiError.conflict(f"同名 AOA Profile 已存在: {name}") from exc
        return self.get_profile(profile_id, owner_id)

    def update_profile(self, profile_id: str, owner_id: str, *, name: str | None = None,
                       description: str | None = None, commands: list[str] | None = None,
                       wait_adb_seconds: int | None = None) -> dict[str, Any]:
        current = self.get_profile(profile_id, owner_id)
        updates: dict[str, Any] = {}
        if name is not None and name != current["name"]:
            updates["name"] = str(name)
        if description is not None:
            updates["description"] = str(description)
        if commands is not None:
            cleaned = [str(item or "").strip() for item in commands if str(item or "").strip()]
            self.validate_commands(cleaned)
            updates["commands"] = json.dumps(cleaned, ensure_ascii=False)
        if wait_adb_seconds is not None:
            updates["wait_adb_seconds"] = max(0, min(int(wait_adb_seconds), 600))
        if not updates:
            return current
        updates["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._lock, self.connect() as conn:
            conn.execute(
                f"UPDATE aoa_profiles SET {assignments} WHERE id=? AND owner_id=?",
                [*updates.values(), profile_id, owner_id])
        return self.get_profile(profile_id, owner_id)

    def delete_profile(self, profile_id: str, owner_id: str) -> None:
        self.get_profile(profile_id, owner_id)
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM aoa_profiles WHERE id=? AND owner_id=?",
                         (profile_id, owner_id))


_stores: dict[str, AoaProfileStore] = {}
_store_lock = threading.Lock()


def profile_store() -> AoaProfileStore:
    """按 Controller 数据根复用的单例（与 DebugStore 同款缓存模式）。"""
    from features.devices import deployment_service

    root = deployment_service.store().root
    with _store_lock:
        if root not in _stores or not _stores[root].db.exists():
            _stores[root] = AoaProfileStore(root)
        return _stores[root]
