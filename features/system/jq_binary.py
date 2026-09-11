"""Pinned jq binary distribution (GET /api/system/tools/jq).

Extracted from api.py so the system API stays under the reviewable-size
limit (see docs/architecture/adr/0002-feature-foundation-boundary.md).
Air-gapped build servers fetch this file from the Controller instead of
GitHub; the staged file is validated before serving so a broken staging
never reaches installers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os

from fastapi import Request
from fastapi.responses import Response

from foundation.responses import error_response


logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JQ_BIN_PATH = os.path.join(PROJECT_ROOT, "tools", "jq-linux-amd64")

# Operators stage this file by hand (scripts/prepare_pinned_jq.sh), so the
# endpoint rejects anything that cannot run on a bare Linux x86-64 host
# before it is advertised to installers. This catches the classic mistakes:
# a text/HTML error page saved as jq, a wrong-architecture build, or a
# distro jq that needs libjq.so.1 on a host that does not have it (such a
# binary is also tiny compared to the ~2.2 MB official release build).
_JQ_MIN_BYTES = 1024 * 1024
_JQ_EM_X86_64 = 62  # e_machine value for EM_X86_64


def jq_binary_rejection_reason(data: bytes) -> str | None:
    """Return why ``data`` is not a usable Linux x86-64 jq binary, else None."""
    if len(data) < _JQ_MIN_BYTES:
        return (
            f"文件过小（{len(data)} 字节），不像 jq 发布二进制"
            "（可能是依赖 libjq 的发行版包装或截断文件）"
        )
    if data[:4] != b"\x7fELF":
        return "不是 ELF 文件（可能是文本/HTML 误存为二进制）"
    if len(data) < 20:
        return "ELF 头不完整"
    if data[4] != 2:  # ELFCLASS64
        return "不是 64 位 ELF"
    if data[5] != 1:  # little-endian
        return "不是小端 ELF"
    machine = int.from_bytes(data[18:20], "little")
    if machine != _JQ_EM_X86_64:
        return f"不是 x86-64 二进制（e_machine={machine}）"
    return None


def load_binary() -> tuple[bytes, str, str | None]:
    """Read the pinned file and return ``(data, sha256, rejection_reason)``."""
    with open(JQ_BIN_PATH, "rb") as jq_file:
        data = jq_file.read()
    digest = hashlib.sha256(data).hexdigest()
    return data, digest, jq_binary_rejection_reason(data)


async def serve(request: Request) -> Response:
    """Serve the pinned jq binary for the skill installer (§十二).

    The Controller is the trust root for the whole agent bootstrap chain
    (same origin as the ZIP and the Ed25519 signing key), so fetching jq
    from here keeps air-gapped build servers working without GitHub
    access. 404 when the pinned file is absent — never a redirect.
    The staged file is validated (ELF64 / x86-64 / sane size) before
    serving; the installer additionally runs `jq --version` after download.
    """
    try:
        data, digest, rejection = await asyncio.to_thread(load_binary)
    except OSError:
        logger.warning("[TOOLS_JQ] pinned jq binary missing: %s", JQ_BIN_PATH)
        return error_response("jq 二进制不可用", status_code=404)
    if rejection:
        logger.error(
            "[TOOLS_JQ] staged jq binary rejected (%s): %s",
            JQ_BIN_PATH,
            rejection,
        )
        return error_response(f"jq 二进制不可用: {rejection}", status_code=404)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="jq"',
            "X-GMS-SHA256": digest,
            "Cache-Control": "no-store",
        },
    )
