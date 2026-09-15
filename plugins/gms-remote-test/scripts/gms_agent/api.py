"""Namespace modules for the gms_agent SDK.

Each module is a thin, typed façade over one Controller domain. They return
the raw API payload (the ``data`` field of the CLI envelope) and raise
:class:`~client.GmsApiError` with CLI-compatible exit codes on failure.
"""
from __future__ import annotations

from typing import Any

from .client import GmsClient


class DevicesApi:
    def __init__(self, client: GmsClient):
        self._client = client

    def list(self, force_refresh: bool = False) -> Any:
        query = "?force_refresh=true" if force_refresh else ""
        return self._client.request("GET", f"/devices/list{query}")["data"]

    def info(self, device: str) -> Any:
        return self._client.request("GET", f"/devices/info?device={device}")["data"]

    def user_locked(self) -> Any:
        return self._client.request("GET", "/devices/user-locked")["data"]


class JobsApi:
    def __init__(self, client: GmsClient):
        self._client = client

    def list(self, limit: int = 100) -> Any:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1-500")
        return self._client.request("GET", f"/cluster/jobs?limit={limit}")["data"]

    def status(self, job_id: str) -> Any:
        return self._client.request("GET", f"/cluster/jobs/{job_id}")["data"]

    def events(self, job_id: str, after: int = -1, limit: int = 500) -> Any:
        if not 1 <= limit <= 2000:
            raise ValueError("limit must be 1-2000")
        return self._client.request(
            "GET", f"/cluster/jobs/{job_id}/events?after={after}&limit={limit}"
        )["data"]


class TestsApi:
    def __init__(self, client: GmsClient):
        self._client = client

    def start(
        self,
        device: str,
        module: str | None = None,
        case: str | None = None,
        suite: str | None = None,
        test_type: str | None = None,
        retry: str | None = None,
        wait: bool = False,
        max_wait: int | None = None,
        worker_id: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {"device": device, "module": module, "case": case}
        if suite:
            body["suite"] = suite
        if test_type:
            body["type"] = test_type
        if retry:
            body["retry"] = retry
        if wait:
            body["wait"] = wait
        if max_wait is not None:
            body["max_wait"] = max_wait
        if worker_id:
            body["worker_id"] = worker_id
        return self._client.request("POST", "/test/start", body)["data"]

    def suites(self) -> Any:
        return self._client.request("GET", "/test/suites")["data"]

    def status(self, job_id: str) -> Any:
        return self._client.request("GET", f"/test/status?job_id={job_id}")["data"]


class ReportsApi:
    def __init__(self, client: GmsClient):
        self._client = client

    def list(self) -> Any:
        return self._client.request("GET", "/reports/list")["data"]


class ApkApi:
    def __init__(self, client: GmsClient):
        self._client = client

    def tasks(self) -> Any:
        return self._client.request("GET", "/apk/tasks")["data"]

    def status(self, task_id: str) -> Any:
        return self._client.request("GET", f"/apk/status/{task_id}")["data"]

    def manifest(self, task_id: str) -> Any:
        return self._client.request("GET", f"/apk/manifest/{task_id}")["data"]


class RedmineApi:
    def __init__(self, client: GmsClient):
        self._client = client

    def issue_fetch(self, issue: str, **params: Any) -> Any:
        return self._client.request(
            "POST", "/redmine-agent/issues/fetch", {"issue": issue, **params}
        )["data"]

    def issue(self, snapshot_id: str) -> Any:
        return self._client.request("GET", f"/redmine-agent/issues/{snapshot_id}")["data"]

    def journals(self, snapshot_id: str, **params: Any) -> Any:
        return self._client.request(
            "GET", f"/redmine-agent/evidence/{snapshot_id}/journals", params=params or None
        )["data"]


class FirmwareApi:
    def __init__(self, client: GmsClient):
        self._client = client

    def burn_status(self, operation_id: str) -> Any:
        return self._client.request("GET", f"/burn/firmware/{operation_id}/status")["data"]


class AuthApi:
    def __init__(self, client: GmsClient):
        self._client = client

    def status(self) -> Any:
        return self._client.request("GET", "/auth/status")["data"]

    def agent_status(self) -> Any:
        return self._client.request("GET", "/auth/agent-status")["data"]
