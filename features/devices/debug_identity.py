"""Read package and verified signing certificates from an immutable APK."""

from __future__ import annotations

import collections
import contextlib
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from foundation.error_model import ApiError
from foundation.processes import run_local_command

from .deployment_execution import run, verify_artifact


_IDENTITY_TTL_SECONDS = 300
_IDENTITY_CACHE_LIMIT = 512
_identity_lock = threading.RLock()
_identity_cache = collections.OrderedDict()


def binary(name: str):
    candidates = [os.getenv("GMS_" + name.upper() + "_PATH", ""), shutil.which(name) or ""]
    for root in (Path.home() / "android-sdk", Path.home() / "Software/android-sdk", Path("/usr/lib/android-sdk")):
        candidates.extend(str(p) for p in sorted((root / "build-tools").glob("*/" + name), reverse=True))
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ApiError.dependency_unavailable(
        "APK 身份校验需要 Android Build-Tools 的 " + name,
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
    package_line = next(line for line in results[0].splitlines() if line.startswith("package:"))
    versions = dict(re.findall(r"(versionCode|versionName)='([^']*)'", package_line))
    return {"package": package[1], "signers": signers,
            "version_code": versions.get("versionCode", ""), "version_name": versions.get("versionName", "")}


def installed_fingerprint(ssh, serial, package, check=lambda: None):
    """Read lightweight install metadata; APK bytes are transferred only on a cache miss."""
    def shell(args):
        check()
        return run(ssh, ["adb", "-s", serial, "shell", shlex.join(args)])

    listing = shell(["pm", "list", "packages", "--user", "0", package])
    lines = [line.strip() for line in listing.splitlines() if line.strip()]
    if any(not re.fullmatch(r"package:[A-Za-z][A-Za-z0-9_.]*", line) for line in lines):
        raise ApiError.upstream_failure("无法读取已安装应用列表", service="adb")
    if "package:" + package not in lines:
        return None
    output = shell(["pm", "path", "--user", "0", package])
    paths = re.findall(r"(?m)^package:(/[^\r\n]+\.apk)\s*$", output)
    bases = [path for path in paths if path.endswith("/base.apk")]
    if len(paths) == 1:
        bases = paths
    if len(bases) != 1 or not re.fullmatch(r"/[A-Za-z0-9_./+=@~-]+\.apk", bases[0]):
        raise ApiError.upstream_failure("无法确定已安装应用的 base APK", service="adb")
    path = bases[0]
    stat = shell(["stat", "-c", "%s:%Y:%i", path]).strip()
    if not re.fullmatch(r"[1-9]\d*:\d+:\d+", stat) or int(stat.split(":", 1)[0]) > 256 * 1024 * 1024:
        raise ApiError.invalid_semantics("已安装 APK 超出身份校验大小限制或元数据无效")
    boot = shell(["cat", "/proc/sys/kernel/random/boot_id"]).strip()
    if not re.fullmatch(r"[a-f0-9-]{16,64}", boot):
        raise ApiError.upstream_failure("无法读取设备启动标识", service="adb")
    details = shell(["dumpsys", "package", package])
    version = re.search(r"(?m)^\s*versionCode=(\d+)", details)
    updated = re.search(r"(?m)^\s*lastUpdateTime=([^\r\n]+)", details)
    return path, f"{boot}:{stat}:{version[1] if version else ''}:{updated[1].strip() if updated else ''}"


def invalidate_installed_identity(worker_id, serial, package, user=0):
    with _identity_lock:
        _identity_cache.pop((worker_id, serial, user, package), None)


def _cached_identity(key, fingerprint):
    now = time.monotonic()
    with _identity_lock:
        cached = _identity_cache.get(key)
        if not cached or cached["expires"] <= now or cached["fingerprint"] != fingerprint:
            _identity_cache.pop(key, None)
            return None
        _identity_cache.move_to_end(key)
        return cached["identity"]


def _remember_identity(key, fingerprint, identity):
    with _identity_lock:
        _identity_cache[key] = {"fingerprint": fingerprint, "identity": identity,
                                "expires": time.monotonic() + _IDENTITY_TTL_SECONDS}
        _identity_cache.move_to_end(key)
        while len(_identity_cache) > _IDENTITY_CACHE_LIMIT:
            _identity_cache.popitem(last=False)


def installed_identity(ssh, serial, package, check=lambda: None):
    """Pull and verify user 0's actual base APK through the audited SSH channel."""
    def shell(args):
        check()
        return run(ssh, ["adb", "-s", serial, "shell", shlex.join(args)])

    # pm path may exit nonzero for a missing package. Use the successful list
    # command to distinguish absence from an unavailable package manager.
    listing = shell(["pm", "list", "packages", "--user", "0", package])
    lines = [line.strip() for line in listing.splitlines() if line.strip()]
    if any(not re.fullmatch(r"package:[A-Za-z][A-Za-z0-9_.]*", line) for line in lines):
        raise ApiError.upstream_failure("无法读取已安装应用列表", service="adb")
    if "package:" + package not in lines:
        return None
    output = shell(["pm", "path", "--user", "0", package])
    paths = re.findall(r"(?m)^package:(/[^\r\n]+\.apk)\s*$", output)
    bases = [path for path in paths if path.endswith("/base.apk")]
    if len(paths) == 1:
        bases = paths
    if len(bases) != 1 or not re.fullmatch(r"/[A-Za-z0-9_./+=@~-]+\.apk", bases[0]):
        raise ApiError.upstream_failure("无法确定已安装应用的 base APK", service="adb")
    path = bases[0]
    size = shell(["stat", "-c", "%s", path]).strip()
    if not size.isdigit() or not 0 < int(size) <= 256 * 1024 * 1024:
        raise ApiError.invalid_semantics("已安装 APK 超出身份校验大小限制")
    digest = shell(["sha256sum", path]).split()
    if not digest or not re.fullmatch(r"[a-f0-9]{64}", digest[0]):
        raise ApiError.upstream_failure("已安装 APK 未返回有效 SHA256", service="adb")
    before = digest[0]
    directory = "/tmp/gms-identity-" + uuid.uuid4().hex
    check()
    run(ssh, ["mkdir", "-m", "700", directory])
    try:
        check()
        run(ssh, ["adb", "-s", serial, "pull", path, directory + "/base.apk"], 180)
        with tempfile.TemporaryDirectory(prefix="gms-identity-") as local:
            artifact = Path(local) / "base.apk"
            with ssh.open_sftp() as sftp:
                sftp.get_channel().settimeout(180)
                if sftp.stat(directory + "/base.apk").st_size != int(size):
                    raise ApiError.conflict("身份校验期间 APK 已变化")
                sftp.get(directory + "/base.apk", str(artifact))
            identity = inspect_apk(artifact, before)
        after = shell(["sha256sum", path]).split()
        if not after or before != after[0] or identity["package"] != package:
            raise ApiError.conflict("身份校验期间已安装应用发生变化")
        return {**identity, "sha256": before}
    finally:
        with contextlib.suppress(Exception):
            run(ssh, ["rm", "-rf", directory])


def require_installed(ssh, session, check=lambda: None, *, worker_id=""):
    serial = session["serial"]
    package = session["package"]
    key = (worker_id, serial, 0, package)
    observed = installed_fingerprint(ssh, serial, package, check)
    if not observed:
        invalidate_installed_identity(worker_id, serial, package)
        raise ApiError.forbidden("设备上应用的签名与调试授权不一致或应用未安装，请重新申请授权")
    path, fingerprint = observed
    identity = _cached_identity(key, fingerprint)
    if identity is None:
        identity = installed_identity(ssh, serial, package, check)
        current = installed_fingerprint(ssh, serial, package, check)
        if (not current or current != observed or identity["package"] != package
                or path != current[0]):
            invalidate_installed_identity(worker_id, serial, package)
            raise ApiError.conflict("身份校验期间已安装应用发生变化")
        _remember_identity(key, fingerprint, identity)
    if not identity or identity["signers"] != session["signers"]:
        invalidate_installed_identity(worker_id, serial, package)
        raise ApiError.forbidden("设备上应用的签名与调试授权不一致或应用未安装，请重新申请授权")
    return identity
