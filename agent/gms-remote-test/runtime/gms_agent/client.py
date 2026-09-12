"""GMS Agent Runtime SDK.

A thin Python client over the Controller REST API. The MCP server and (over
time) the gms-rt CLI share this SDK so business validation lives in exactly
one place instead of diverging across "CLI validation v1 / MCP validation v2
/ Controller validation v3".

Envelope contract: every call returns the same shape the gms-rt CLI prints
in --json mode —
    {"ok": bool, "exit_code": int, "data": <response body>}   (success)
    {"ok": False, "exit_code": int, "output"|"diagnostics": ...} (failure)
with exit codes matching the CLI's stable mapping (3=auth, 4=permission,
5=conflict, 6=network, 7=operation).
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_PERMISSION = 4
EXIT_CONFLICT = 5
EXIT_NETWORK = 6
EXIT_OPERATION = 7


class GmsApiError(Exception):
    """A Controller call failed; carries the CLI-compatible exit code."""

    def __init__(self, message: str, exit_code: int = EXIT_OPERATION, status: int = 0):
        super().__init__(message)
        self.exit_code = exit_code
        self.status = status


def _http_exit_code(status: int) -> int:
    if status == 401:
        return EXIT_AUTH
    if status == 403:
        return EXIT_PERMISSION
    if status in (409, 423, 429):
        return EXIT_CONFLICT
    if status == 0 or 500 <= status <= 599:
        return EXIT_NETWORK
    if 200 <= status <= 299:
        return EXIT_OK
    return EXIT_OPERATION


def _load_token(token_path: Path) -> str:
    """Read the raw token from a 0600 file (CLI parity).

    The CLI enforces owner-only permissions on token
    files; the SDK/MCP fast path read any world-readable file. Fail
    closed — a mis-permissioned token file is treated as absent and
    requests go out unauthenticated (→ 401) instead of silently using a
    credential any local user could have read.
    """
    try:
        path = token_path.expanduser()
        stat = path.stat()
        mode = stat.st_mode & 0o777
        # Parity with the CLI — permissions alone don't
        # prove ownership; a group/world-readable 0600 file owned by
        # another user (or a file planted in a shared directory) must be
        # rejected exactly like the CLI rejects it.
        if mode & 0o077 or (hasattr(os, "geteuid") and stat.st_uid != os.geteuid()):
            print(
                f"Error: token file {path} 权限为 {oct(mode)}"
                + (
                    f"，owner uid={stat.st_uid} 而非当前 uid={os.geteuid()}"
                    if hasattr(os, "geteuid") and stat.st_uid != os.geteuid()
                    else ""
                )
                + "；要求 0600 且属主为当前用户，已拒绝读取",
                file=sys.stderr,
            )
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class GmsClient:
    """Minimal stdlib-only HTTP client for the Controller API.

    Authentication: Agent Service Token via ``GMS_AUTH_TOKEN_FILE`` (0600
    file containing the raw token). No password handling anywhere — the SDK
    is for agents, and agents never see platform passwords.
    """

    def __init__(
        self,
        server_url: str | None = None,
        token_file: str | None = None,
        ca_cert: str | None = None,
        insecure: bool = False,
        timeout: int = 120,
    ):
        self.server_url = (server_url or os.environ.get("GMS_REMOTE_TEST_SERVER", "")).rstrip("/")
        if not self.server_url:
            raise GmsApiError("GMS_REMOTE_TEST_SERVER 未设置", EXIT_USAGE)
        token_path = token_file or os.environ.get("GMS_AUTH_TOKEN_FILE", "")
        self.token_path = token_path
        self.token = _load_token(Path(token_path)) if token_path else ""
        self.ca_cert = ca_cert or os.environ.get("GMS_CURL_CA_CERT", "")
        self.insecure = insecure or os.environ.get("GMS_CURL_INSECURE", "") == "1"
        self.timeout = timeout
        self._ssl_context: ssl.SSLContext | None = None

    # -- low level ---------------------------------------------------------

    def refresh_token(self, token_path: str | None = None) -> None:
        """Re-read the token file before a request.

        A long-lived client (the MCP adapter keeps one singleton) would
        otherwise keep authenticating with the token captured at startup,
        even after enroll rotated the file. Re-reading a 0600 file per
        request is cheap and keeps MCP auth state in lockstep with the CLI.
        An explicit ``token_path`` also follows the profile TOML when enroll
        moved the token to a new file.
        """

        if token_path:
            self.token_path = token_path
        if self.token_path:
            self.token = _load_token(Path(self.token_path))
        else:
            self.token = ""

    def _ssl(self) -> ssl.SSLContext | None:
        if not self.server_url.startswith("https://"):
            return None
        if self._ssl_context is not None:
            return self._ssl_context
        if self.ca_cert:
            context = ssl.create_default_context(cafile=self.ca_cert)
        elif self.insecure:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
        self._ssl_context = context
        return context

    def request(self, method: str, endpoint: str, body: dict | None = None, params: dict | None = None) -> dict:
        """One Controller call → CLI-compatible JSON envelope dict."""
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        query = ""
        if params:
            from urllib.parse import urlencode

            query = "?" + urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.server_url}/api{endpoint}{query}"
        # Pick up token rotations/enroll-paths between calls.
        self.refresh_token()
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl()) as response:
                raw = response.read().decode("utf-8", "replace")
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace") if error.fp else ""
            status = error.code
        except (urllib.error.URLError, OSError, ssl.SSLError) as error:
            raise GmsApiError(f"Controller 连接失败: {error}", EXIT_NETWORK) from error

        parsed = None
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except ValueError:
            parsed = None
        exit_code = _http_exit_code(status)
        if exit_code != EXIT_OK:
            message = (parsed or {}).get("error") if isinstance(parsed, dict) else None
            raise GmsApiError(message or f"HTTP {status}: {raw[:500]}", exit_code, status)
        envelope = {"ok": True, "exit_code": EXIT_OK, "data": parsed}
        return envelope


from .api import (  # noqa: E402
    ApkApi,
    AuthApi,
    DevicesApi,
    FirmwareApi,
    JobsApi,
    RedmineApi,
    ReportsApi,
    TestsApi,
)


class GmsAgentSdk:
    """Namespaced façade: client.devices.list(), client.jobs.list(), ..."""

    def __init__(self, client: GmsClient | None = None, **kwargs):
        self.client = client or GmsClient(**kwargs)
        self.devices = DevicesApi(self.client)
        self.jobs = JobsApi(self.client)
        self.tests = TestsApi(self.client)
        self.reports = ReportsApi(self.client)
        self.apk = ApkApi(self.client)
        self.redmine = RedmineApi(self.client)
        self.firmware = FirmwareApi(self.client)
        self.auth = AuthApi(self.client)


__all__ = [
    "EXIT_AUTH",
    "EXIT_CONFLICT",
    "EXIT_NETWORK",
    "EXIT_OK",
    "EXIT_OPERATION",
    "EXIT_PERMISSION",
    "EXIT_USAGE",
    "GmsAgentSdk",
    "GmsApiError",
    "GmsClient",
]
