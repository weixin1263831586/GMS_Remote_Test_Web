import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.reports.analysis_api import _resolve_suite_log_dir


class ResolveSuiteLogDirTests(unittest.TestCase):
    """Path resolution + boundary checks for the suite log-dir analyzer."""

    def _config(self, suites_path: str) -> dict:
        return {"suites_path": suites_path}

    def test_resolves_relative_path_inside_suite(self):
        with TemporaryDirectory() as tmp:
            suite_root = str(Path(tmp) / "android-vts" / "android-vts")
            abs_path, err = _resolve_suite_log_dir(
                f"{suite_root}/tools",
                "logs/2026.06.25_10.57.05",
                self._config(tmp),
            )
            self.assertIsNone(err)
            self.assertEqual(abs_path, f"{suite_root}/logs/2026.06.25_10.57.05")

    def test_rejects_parent_traversal(self):
        with TemporaryDirectory() as tmp:
            _abs, err = _resolve_suite_log_dir(
                f"{tmp}/android-vts/tools",
                "../../../etc",
                self._config(tmp),
            )
            self.assertEqual(err, "非法路径")

    def test_rejects_path_outside_suites_root(self):
        with TemporaryDirectory() as tmp:
            _abs, err = _resolve_suite_log_dir(
                "/opt/secret/tools",
                "logs",
                self._config(tmp),
            )
            self.assertEqual(err, "测试套件不在配置的套件目录内")

    def test_rejects_non_absolute_suite_path(self):
        _abs, err = _resolve_suite_log_dir(
            "relative/android-vts/tools",
            "logs",
            self._config("/tmp"),
        )
        self.assertEqual(err, "无效的测试套件路径")

    def test_expands_home_in_configured_suites_path(self):
        suite_root = Path.home() / "GMS-Suite" / "android-vts" / "android-vts"
        abs_path, err = _resolve_suite_log_dir(
            f"{suite_root}/tools",
            "logs/2026.06.25_10.57.05",
            {"suites_path": "~/GMS-Suite"},
        )
        self.assertIsNone(err)
        self.assertEqual(
            abs_path,
            str(suite_root / "logs" / "2026.06.25_10.57.05"),
        )


class AnalyzeLogDirEndpointTests(unittest.TestCase):
    """End-to-end: a real logs/<timestamp>/inv_*/host_log_*.txt tree."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        # 使用隔离的认证数据库。
        from features.auth import auth_service
        self._orig_auth_db = auth_service.db_path
        auth_service.db_path = Path(self.tmp.name) / "platform_auth.sqlite3"
        auth_service._initialized = False
        self.suite_root = Path(self.tmp.name) / "android-vts-17_r1" / "android-vts"
        logs_dir = self.suite_root / "logs" / "2026.06.25_10.57.05" / "inv_123"
        logs_dir.mkdir(parents=True)
        # 构造解析器可接受的最小 host_log。
        (logs_dir / "host_log_111.txt").write_text(
            "07-01 10:00:00 I/Test: sample host log line\n", encoding="utf-8"
        )

        # Point suites_path at the temp root so the boundary check passes.
        from foundation.config import config_manager as _cm
        self._orig_config = _cm.load_config()
        self._config_patch = {
            **(self._orig_config or {}),
            "suites_path": str(self.tmp.name),
        }
        _cm.save_config(self._config_patch)
        self.client = TestClient(create_app())
        # The endpoint is auth-gated like all /api routes; create + log in an
        # admin so the requests pass the gate.
        setup = self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        owner_id = setup.json()["user"]["id"]

        class OwnedReportStore:
            @staticmethod
            def get_report_by_timestamp(
                timestamp,
                *,
                owner_id=None,
                include_all=False,
            ):
                if timestamp != "2026.06.25_10.57.05":
                    return None
                if not include_all and owner_id != owner_id_value:
                    return None
                return {
                    "report_id": "report-log-dir-test",
                    "timestamp": timestamp,
                    "owner_id": owner_id_value,
                }

        owner_id_value = owner_id

        self._report_store_patch = patch(
            "features.reports.analysis_api.test_report_db",
            OwnedReportStore(),
        )
        self._report_store_patch.start()

    def tearDown(self):
        self._report_store_patch.stop()
        self.client.close()
        from foundation.config import config_manager as _cm
        _cm.save_config(self._orig_config)
        from features.auth import auth_service
        auth_service.db_path = self._orig_auth_db
        auth_service._initialized = False
        self.tmp.cleanup()

    def test_analyzes_real_log_folder(self):
        resp = self.client.post(
            "/api/reports/analyze-log-dir",
            data={
                "suite_path": f"{self.suite_root}/tools",
                "path": "logs/2026.06.25_10.57.05",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["mode"], "suite_log_dir")
        self.assertEqual(body["data"]["report_type"], "log")
        self.assertEqual(body["data"]["report_name"], "2026.06.25_10.57.05")

    def test_returns_clear_error_when_dir_not_local(self):
        resp = self.client.post(
            "/api/reports/analyze-log-dir",
            data={
                "suite_path": f"{self.suite_root}/tools",
                "path": "logs/does_not_exist",
            },
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertFalse(body["success"])

    def test_anonymous_development_mode_can_analyze_local_log_folder(self):
        self.client.cookies.clear()
        with patch.dict(
            "os.environ",
            {"GMS_ENV": "development", "GMS_AUTH_REQUIRED": "false"},
        ):
            resp = self.client.post(
                "/api/reports/analyze-log-dir",
                data={
                    "suite_path": f"{self.suite_root}/tools",
                    "path": "logs/2026.06.25_10.57.05",
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["success"])

    def test_optional_admin_session_does_not_require_deleted_report_index(self):
        class EmptyReportStore:
            @staticmethod
            def get_report_by_timestamp(*_args, **_kwargs):
                return None

        with patch(
            "features.reports.analysis_api.test_report_db", EmptyReportStore()
        ), patch.dict(
            "os.environ",
            {"GMS_ENV": "development", "GMS_AUTH_REQUIRED": "false"},
        ):
            resp = self.client.post(
                "/api/reports/analyze-log-dir",
                data={
                    "suite_path": f"{self.suite_root}/tools",
                    "path": "logs/2026.06.25_10.57.05",
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["success"])


if __name__ == "__main__":
    unittest.main()
