"""One-shot approval token storage (2026-09-08 audit §五).

Split out of agent_tokens.py after the burn approval gained
server-side operation derivation, pushing the module past its reviewable
line limit. ApprovalTokenServiceMixin is mixed into AuthService and reuses
its _connect/_lock/hash_token helpers.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .constants import APPROVAL_TOKEN_TTL_SECONDS, CurrentUser


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ApprovalTokenServiceMixin:
    """One-shot approval token storage; mixed into AuthService."""

    # ------------------------------------------------------------------
    # One-shot Approval Tokens (2026-09-08 audit §五)
    # ------------------------------------------------------------------

    @staticmethod
    def command_hash(command: str) -> str:
        return hashlib.sha256(str(command or "").encode("utf-8")).hexdigest()

    # 精确绑定：烧录审批不再绑定 "burn_firmware:<devices>" 这种
    # 宽泛串，而是绑定完整的 operation：规范化设备列表 + 固件 SHA256 +
    # wipe_data + burn_mode。命令串只由服务端从这些字段派生，审批创建与
    # 消费两端使用同一函数，调用方无法用为 A 固件签发的令牌烧 B 固件。
    BURN_TOOL = "gms_rt_burn_firmware"

    @classmethod
    def derive_burn_command(
        cls,
        *,
        device: str,
        firmware_sha256: str,
        wipe_data: bool,
        burn_mode: str,
    ) -> str:
        devices = ",".join(
            sorted({
                part.strip()
                for part in str(device or "").split(",")
                if part.strip()
            })
        )
        digest = str(firmware_sha256 or "").strip().lower()
        if not digest:
            raise ValueError("firmware_sha256 必填")
        wipe = "true" if wipe_data else "false"
        mode = str(burn_mode or "auto").strip().lower()
        if mode not in {"auto", "uf"}:
            raise ValueError("burn_mode 必须是 auto 或 uf")
        return f"burn_firmware:{devices}:{digest}:wipe={wipe}:mode={mode}"

    def create_approval_token(
        self,
        *,
        user: CurrentUser,
        tool: str,
        device: str,
        command: str = "",
        firmware_sha256: str = "",
        wipe_data: bool = True,
        burn_mode: str = "auto",
        ttl_seconds: int = APPROVAL_TOKEN_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Issue a single-use approval bound to tool+device+SHA256(command).

        For the burn tool the command string is DERIVED server-side from the
        full operation (devices + firmware SHA256 + wipe_data + burn_mode);
        any client-supplied command is ignored so an approval minted for one
        firmware can never be consumed for another (精确绑定).
        """
        tool_name = str(tool or "").strip()
        device_name = str(device or "").strip()
        if not tool_name or not device_name:
            raise ValueError("tool 和 device 必填")
        if tool_name == self.BURN_TOOL:
            command = self.derive_burn_command(
                device=device_name,
                firmware_sha256=firmware_sha256,
                wipe_data=wipe_data,
                burn_mode=burn_mode,
            )
            device_name = command.split(":")[1]
        ttl = max(30, min(600, int(ttl_seconds or APPROVAL_TOKEN_TTL_SECONDS)))
        token = secrets.token_urlsafe(32)
        now = _utcnow()
        expires_at = now + timedelta(seconds=ttl)
        with self._lock, self._connect() as conn:
            # Bound the table: drop fully consumed/expired approvals.
            conn.execute(
                "DELETE FROM platform_approval_tokens WHERE expires_at <= ? OR used_at IS NOT NULL",
                (_to_iso(now),),
            )
            conn.execute(
                """
                INSERT INTO platform_approval_tokens (
                    token_hash, user_id, tool, device, command_hash,
                    created_at, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    self.hash_token(token),
                    user.id,
                    tool_name,
                    device_name,
                    self.command_hash(command),
                    _to_iso(now),
                    _to_iso(expires_at),
                ),
            )
            conn.commit()
        return {
            "token": token,
            "tool": tool_name,
            "device": device_name,
            "expires_at": _to_iso(expires_at),
            "ttl_seconds": ttl,
        }

    def consume_approval_token(
        self, token: str, *, tool: str, device: str, command: str
    ) -> bool:
        """Atomically consume a matching one-shot approval token."""
        if not token:
            return False
        token_hash = self.hash_token(token)
        now = _utcnow()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT command_hash, expires_at, used_at
                FROM platform_approval_tokens
                WHERE token_hash = ? AND tool = ? AND device = ?
                """,
                (token_hash, str(tool or "").strip(), str(device or "").strip()),
            ).fetchone()
            if not row or row["used_at"]:
                return False
            try:
                if _from_iso(row["expires_at"]) <= now:
                    return False
            except ValueError:
                return False
            if not hmac.compare_digest(
                str(row["command_hash"]), self.command_hash(command)
            ):
                return False
            # Single use: only the first consume wins; a racing caller's
            # UPDATE matches zero rows because used_at is now set.
            cursor = conn.execute(
                "UPDATE platform_approval_tokens SET used_at = ? "
                "WHERE token_hash = ? AND used_at IS NULL",
                (_to_iso(now), token_hash),
            )
            conn.commit()
            return cursor.rowcount == 1
