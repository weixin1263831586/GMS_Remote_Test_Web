"""APK deployment and structured database reads through ADR 0004's SSH boundary."""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import re
import shlex
from pathlib import Path

import paramiko

from foundation.error_model import ApiError
from foundation.ssh_executor import ssh_executor


def run(ssh, args: list[str], timeout: int = 30):
    result = ssh_executor.run(ssh, shlex.join(args), timeout=timeout)
    if result.timed_out:
        raise ApiError.dependency_timeout("设备操作超时")
    if not result.ok:
        message = (result.stderr or result.stdout or "SSH/ADB 执行失败")[:1500]
        if "not found" in message and "sqlite3" in message:
            raise ApiError.dependency_unavailable(
                "设备缺少 sqlite3；请使用应用 ContentProvider 的只读 content query 或准备 sqlite3",
            )
        if "not debuggable" in message or "Permission denied" in message:
            raise ApiError.forbidden("应用数据库不可读：需要可调试应用或已授予的数据库读取权限")
        if "unable to open database" in message:
            raise ApiError.forbidden("数据库不可读：请检查应用调试属性、库名及设备已有读取权限")
        if "no such table" in message or "no such column" in message or "not a database" in message:
            raise ApiError.invalid_semantics(message)
        raise ApiError.upstream_failure(message, service="ssh/adb")
    return result.stdout


def verify_artifact(path: Path, expected: str):
    if path.is_symlink() or not path.is_file():
        raise ApiError.conflict("APK 制品已丢失")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ApiError.conflict("APK SHA256 与待审批制品不一致，请重新提交")


def checksum(ssh, args):
    fields = run(ssh, args).split()
    if not fields or not re.fullmatch(r"[a-f0-9]{64}", fields[0]):
        raise ApiError.upstream_failure("APK 校验命令未返回有效 SHA256", service="ssh/adb")
    return fields[0]


def install(ssh, payload: dict, artifact: Path, check_claim=lambda: None):
    verify_artifact(artifact, payload["sha256"])
    directory = "/tmp/gms-apk-" + payload["id"]
    remote = directory + "/artifact.apk"
    device_path = "/data/local/tmp/gms-apk-" + payload["id"] + ".apk"
    serial = payload["serial"]
    check_claim()
    run(ssh, ["mkdir", "-m", "700", directory])
    try:
        try:
            with ssh.open_sftp() as sftp:
                sftp.get_channel().settimeout(300)
                sftp.put(str(artifact), remote)
        except (OSError, EOFError, paramiko.SSHException) as exc:
            raise ApiError.upstream_failure("APK 的 SSH 传输失败", service="sftp") from exc
        host_digest = checksum(ssh, ["sha256sum", remote])
        if host_digest != payload["sha256"]:
            raise ApiError.upstream_failure("SSH 传输后 APK SHA256 不匹配", service="sftp")
        check_claim()
        run(ssh, ["adb", "-s", serial, "push", remote, device_path], 180)
        device_digest = checksum(ssh, ["adb", "-s", serial, "shell", shlex.join([
            "sha256sum", device_path,
        ])])
        if device_digest != payload["sha256"]:
            raise ApiError.upstream_failure("ADB 传输后 APK SHA256 不匹配", service="adb")
        command = ["pm", "install"]
        if payload["replace_existing"]:
            command.append("-r")
        command.append(device_path)
        check_claim()
        output = run(ssh, ["adb", "-s", serial, "shell", shlex.join(command)], 180)
        if not re.search(r"(?m)^Success\s*$", output):
            raise ApiError.upstream_failure(output[:1500] or "pm install 未返回 Success", service="adb")
        return {"installed": True, "sha256": payload["sha256"], "output": output[:1500]}
    finally:
        with contextlib.suppress(Exception):
            run(ssh, ["adb", "-s", serial, "shell", shlex.join(["rm", "-f", device_path])])
        with contextlib.suppress(Exception):
            run(ssh, ["rm", "-rf", "--", directory])


def query_database(ssh, serial: str, body):
    columns = ", ".join('"' + value + '"' for value in body.columns) or "*"
    sql = (
        'SELECT ' + columns + ' FROM "' + body.table + '" LIMIT '
        + str(body.limit + 1) + ' OFFSET ' + str(body.offset)
        if body.table else
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 501"
    )
    args = ["run-as", body.package, "sqlite3", "-readonly", "-header", "-csv",
            "databases/" + body.database, sql]
    access = "run_as"
    try:
        output = run(ssh, ["adb", "-s", serial, "shell", shlex.join(args)], 30)
    except ApiError as exc:
        if exc.code != "FORBIDDEN":
            raise
        # Userdebug boards may already grant the device shell access even
        # when the installed system APK is not debuggable. Reuse that access
        # with the SAME read-only SELECT; never invoke adb root/su/chmod.
        args = ["sqlite3", "-readonly", "-header", "-csv",
                "/data/user/0/" + body.package + "/databases/" + body.database, sql]
        output = run(ssh, ["adb", "-s", serial, "shell", shlex.join(args)], 30)
        access = "shell"
    if "[GMS: output truncated]" in output:
        raise ApiError.upstream_failure("数据库输出过大，请减少查询列数或分页大小", service="adb")
    if len(output.encode("utf-8")) > 512 * 1024:
        raise ApiError.invalid_semantics("数据库返回超过 512 KiB，请减少查询列数或分页大小")
    reader = csv.DictReader(io.StringIO(output))
    rows = list(reader)
    limit = body.limit if body.table else 500
    return {"columns": reader.fieldnames or [], "rows": rows[:limit],
            "has_more": len(rows) > limit, "offset": body.offset, "read_only": True,
            "access_mode": access, "user_id": 0}
