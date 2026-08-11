import subprocess
import unittest
from unittest.mock import patch

from features.system.update_monitor import api_support
from features.system.update_monitor.fetching import fetch_source
from features.system.update_monitor.models import SourceConfig


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.url = "https://docs.partner.android.com/protected"
        self.content = b""

    def raise_for_status(self):
        raise AssertionError("protected HTTP errors should be translated first")


class _Session:
    def __init__(self, response):
        self.response = response

    def get(self, *_args, **_kwargs):
        return self.response


class UpdateMonitorFailureTests(unittest.TestCase):
    def test_protected_404_explains_partner_login_recovery(self):
        source = SourceConfig(
            key="mainline_preload",
            name="Mainline PRELOAD Release Notes",
            url="https://docs.partner.android.com/mainline/release/release-notes",
            category="mainline_package",
            parser="mainline_release_notes",
            auth_required=True,
        )

        with self.assertRaisesRegex(RuntimeError, "Firefox.*HTTP 404|HTTP 404.*Firefox"):
            fetch_source(_Session(_Response(404)), source, 30)

    def test_sync_status_includes_last_stderr_error(self):
        result = subprocess.CompletedProcess(
            args=["python", "-m", "features.system.update_monitor.cli"],
            returncode=1,
            stdout="",
            stderr="fetching mainline_preload\nerror: Partner login expired\n",
        )
        with patch("features.system.update_monitor.api_support.subprocess.run", return_value=result):
            api_support._run_sync_job("full", ["mainline_preload"])

        with api_support._sync_lock:
            status = dict(api_support._sync_status)
        self.assertEqual(
            status["error"],
            "sync exited with 1: Partner login expired",
        )
        self.assertEqual(status["stderr"], result.stderr)


if __name__ == "__main__":
    unittest.main()
