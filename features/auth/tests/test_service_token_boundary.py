"""Service-token boundary tests.

Split from test_security_boundary.py (11.txt P1-3: the file outgrew the
reviewable-size gate in tests/architecture/test_file_size_rules.py).
Covers worker-token routes and service authentication auditing; shares the
production-bootstrap fixture via SecurityBoundaryFixtureTests.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from features.cluster import ClusterRepository, ClusterService
from features.cluster import api as cluster_api
from features.system import security_audit_logger

from .test_security_boundary import SecurityBoundaryFixtureTests


class ServiceTokenBoundaryTests(SecurityBoundaryFixtureTests):
    def test_worker_token_routes_bypass_browser_session_but_still_validate_token(self):
        previous_service = cluster_api.cluster_service
        repository = ClusterRepository(Path(self.tmp.name) / "cluster.sqlite3")
        cluster_api.cluster_service = ClusterService(repository)
        registration = {
            "worker_id": "worker-246",
            "hostname": "worker-host",
            "address": "192.0.2.10",
            "session_id": "session-1",
        }
        try:
            tokens_path = Path(self.tmp.name) / "worker_tokens_246.json"
            tokens_path.write_text(
                json.dumps({"worker_tokens": {"worker-246": "worker-secret"}}),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(tokens_path)},
            ):
                invalid = self.client.post(
                    "/api/cluster/workers/register",
                    headers={"Authorization": "Bearer wrong"},
                    json=registration,
                )
                accepted = self.client.post(
                    "/api/cluster/workers/register",
                    headers={"Authorization": "Bearer worker-secret"},
                    json=registration,
                )
        finally:
            cluster_api.cluster_service = previous_service

        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["detail"], "invalid worker token")
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["worker"]["id"], "worker-246")

    def test_worker_adb_proxy_pair_code_uses_service_authentication(self):
        previous_service = cluster_api.cluster_service
        repository = ClusterRepository(Path(self.tmp.name) / "cluster.sqlite3")
        cluster_api.cluster_service = ClusterService(repository)
        repository.register_worker({
            "worker_id": "worker-target",
            "hostname": "target-host",
            "address": "192.0.2.20",
        })
        tokens_path = Path(self.tmp.name) / "worker_tokens_adb_proxy.json"
        tokens_path.write_text(
            json.dumps({
                "worker_tokens": {
                    "worker-source": "source-worker-secret",
                    "worker-target": "target-worker-secret",
                },
            }),
            encoding="utf-8",
        )
        try:
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(tokens_path)},
            ):
                grant = self._pair_grant("worker-source", "worker-target")
                invalid = self.client.post(
                    "/api/cluster/workers/worker-target/adb-proxy/pair-code",
                    headers={"Authorization": "Bearer wrong"},
                    json={
                        "source_worker_id": "worker-source",
                        "access_token": grant,
                    },
                )
                accepted = self.client.post(
                    "/api/cluster/workers/worker-target/adb-proxy/pair-code",
                    headers={"Authorization": "Bearer target-worker-secret"},
                    json={
                        "source_worker_id": "worker-source",
                        "access_token": grant,
                    },
                )
        finally:
            cluster_api.cluster_service = previous_service

        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["detail"], "invalid worker token")
        self.assertEqual(accepted.status_code, 200)
        self.assertRegex(accepted.json()["access_token"], r"^[A-Z2-7]{8}$")
        self.assertIn("no-store", accepted.headers["cache-control"])

    def test_service_authenticated_successes_are_not_audited_but_failures_are(self):
        # Worker heartbeat/poll/register are trusted internal traffic on a
        # hot path (polled every few seconds per worker). Auditing every
        # success grew security_audit.json to 240+ MB. Only failures must
        # land in the audit log.
        import json as _json

        previous_service = cluster_api.cluster_service
        repository = ClusterRepository(Path(self.tmp.name) / "cluster.sqlite3")
        cluster_api.cluster_service = ClusterService(repository)
        registration = {
            "worker_id": "worker-246",
            "hostname": "worker-host",
            "address": "192.0.2.10",
            "session_id": "session-1",
        }
        try:
            tokens_path = Path(self.tmp.name) / "worker_tokens_246.json"
            tokens_path.write_text(
                json.dumps({"worker_tokens": {"worker-246": "worker-secret"}}),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(tokens_path)},
            ):
                self.client.post(
                    "/api/cluster/workers/register",
                    headers={"Authorization": "Bearer worker-secret"},
                    json=registration,
                )
                self.client.post(
                    "/api/cluster/workers/register",
                    headers={"Authorization": "Bearer wrong"},
                    json=registration,
                )
        finally:
            cluster_api.cluster_service = previous_service

        audit_path = security_audit_logger.log_path
        paths: list[str] = []
        try:
            with open(audit_path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        paths.append(_json.loads(line).get("path", ""))
                    except Exception:
                        continue
        except FileNotFoundError:
            pass
        register_successes = [
            p for p in paths
            if p == "/api/cluster/workers/register"
        ]
        # The successful registration must not have been audited; the failed
        # one (status 401) must be.
        self.assertEqual(len(register_successes), 1)

    def test_worker_authenticated_suite_download_is_not_a_public_link(self):
        previous_service = cluster_api.cluster_service
        repository = ClusterRepository(Path(self.tmp.name) / "cluster.sqlite3")
        cluster_api.cluster_service = ClusterService(repository)
        try:
            tokens_path = Path(self.tmp.name) / "worker_tokens_246.json"
            tokens_path.write_text(
                json.dumps({"worker_tokens": {"worker-246": "worker-secret"}}),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(tokens_path)},
            ):
                invalid = self.client.get(
                    "/api/cluster/suite-library-download/safe/archive.zip",
                    params={"worker_id": "worker-246"},
                    headers={"Authorization": "Bearer wrong"},
                )
                authenticated = self.client.get(
                    "/api/cluster/suite-library-download/safe/archive.zip",
                    params={"worker_id": "worker-246"},
                    headers={"Authorization": "Bearer worker-secret"},
                )
        finally:
            cluster_api.cluster_service = previous_service

        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["detail"], "invalid worker token")
        self.assertEqual(authenticated.status_code, 404)


if __name__ == "__main__":
    unittest.main()
