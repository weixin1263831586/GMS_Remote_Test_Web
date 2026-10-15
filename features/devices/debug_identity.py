"""Read package and verified signing certificates from an immutable APK."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from foundation.error_model import ApiError
from foundation.processes import run_local_command

from .deployment_execution import verify_artifact


def binary(name: str):
    candidates = [os.getenv("GMS_" + name.upper() + "_PATH", ""), shutil.which(name) or ""]
    for root in (Path.home() / "android-sdk", Path.home() / "Software/android-sdk", Path("/usr/lib/android-sdk")):
        candidates.extend(str(p) for p in sorted((root / "build-tools").glob("*/" + name), reverse=True))
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ApiError.dependency_unavailable(
        "调试会话需要 Android Build-Tools 的 " + name,
        next_actions=({"action": "配置 GMS_" + name.upper() + "_PATH 后重新提交"},),
    )


def inspect_apk(path: Path, sha256: str):
    verify_artifact(path, sha256)
    results = []
    for argv in ([binary("aapt2"), "dump", "badging", str(path)],
                 [binary("apksigner"), "verify", "--print-certs", str(path)]):
        result = run_local_command(argv, timeout=60)
        if result.timed_out or (result.code == -1 and "timed out" in result.stderr.lower()):
            raise ApiError.dependency_timeout("APK 包名/签名校验超时")
        if result.code == -1:
            raise ApiError.dependency_unavailable("APK 校验工具无法执行，请检查 Build-Tools 和 Java")
        if not result.ok:
            raise ApiError.invalid_semantics("APK 清单或签名校验失败，请提交已签名的完整 APK")
        results.append(result.stdout)
    package = re.search(r"(?m)^package: name='([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)'", results[0])
    signers = sorted({s.lower() for s in re.findall(
        r"(?m)^Signer #\d+ certificate SHA-256 digest: ([a-fA-F0-9]{64})\s*$", results[1],
    )})
    if not package or not signers:
        raise ApiError.invalid_semantics("APK 未返回有效包名和签名证书")
    verify_artifact(path, sha256)
    return {"package": package[1], "signers": signers}
