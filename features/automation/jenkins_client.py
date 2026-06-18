"""Small Jenkins REST client used by automation runs."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin

import requests


class JenkinsClient:
    def __init__(self, config: dict[str, Any], session: Any | None = None):
        self.config = config or {}
        self.base_url = str(self.config.get("base_url") or "").rstrip("/") + "/"
        self.session = session or requests.Session()
        username = str(self.config.get("username") or "").strip()
        token = str(self.config.get("api_token") or self.config.get("token") or "").strip()
        if username and token:
            self.session.auth = (username, token)
        self.session.verify = bool(self.config.get("verify_ssl", True))

    def _job_url(self, job_name: str, suffix: str = "") -> str:
        parts = [quote(part, safe="") for part in str(job_name or "").strip("/").split("/") if part]
        job_path = "/".join(f"job/{part}" for part in parts)
        return urljoin(self.base_url, f"{job_path}/{suffix.lstrip('/')}")

    def _crumb_headers(self) -> dict[str, str]:
        if self.config.get("crumb", True) is False:
            return {}
        try:
            response = self.session.get(urljoin(self.base_url, "crumbIssuer/api/json"), timeout=10)
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            data = response.json()
            field = data.get("crumbRequestField")
            crumb = data.get("crumb")
            if field and crumb:
                return {field: crumb}
        except Exception:
            return {}
        return {}

    def trigger_build(self, job_name: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        parameters = parameters or {}
        suffix = "buildWithParameters" if parameters else "build"
        url = self._job_url(job_name, suffix)
        headers = self._crumb_headers()
        response = self.session.post(url, data=parameters, headers=headers, timeout=20)
        response.raise_for_status()
        return {
            "success": True,
            "job": job_name,
            "queue_url": response.headers.get("Location", ""),
            "status_code": response.status_code,
        }

    def get_queue_item(self, queue_url: str) -> dict[str, Any]:
        response = self.session.get(urljoin(queue_url.rstrip("/") + "/", "api/json"), timeout=20)
        response.raise_for_status()
        data = response.json()
        executable = data.get("executable") or {}
        return {
            "success": True,
            "cancelled": bool(data.get("cancelled")),
            "why": data.get("why") or "",
            "build_number": str(executable.get("number") or ""),
            "build_url": executable.get("url") or "",
            "raw": data,
        }

    def get_build(self, job_name: str, build_number: str | int) -> dict[str, Any]:
        response = self.session.get(self._job_url(job_name, f"{build_number}/api/json"), timeout=20)
        response.raise_for_status()
        data = response.json()
        return {
            "success": True,
            "job": job_name,
            "build_number": str(build_number),
            "building": bool(data.get("building")),
            "result": data.get("result") or "",
            "url": data.get("url") or self._job_url(job_name, str(build_number)),
            "artifacts": data.get("artifacts") or [],
            "raw": data,
        }

    @staticmethod
    def select_artifact(build: dict[str, Any], artifact_pattern: str) -> dict[str, Any]:
        pattern = re.compile(artifact_pattern or r".*")
        build_url = str(build.get("url") or "").rstrip("/") + "/"
        for artifact in build.get("artifacts") or []:
            relative_path = str(artifact.get("relativePath") or artifact.get("fileName") or "")
            if relative_path and pattern.search(relative_path):
                return {
                    "success": True,
                    "relative_path": relative_path,
                    "url": urljoin(build_url, f"artifact/{relative_path}"),
                    "artifact": artifact,
                }
        return {"success": False, "error": "No matching Jenkins artifact", "pattern": artifact_pattern}
