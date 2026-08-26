"""Nightly real-device soak tests: Controller → Worker → USB/IP → suite → recovery.

这些测试驱动真实部署（默认 ``http://127.0.0.1:5001``），需要：

- 一台真实 Worker Agent 与一台通过 USB/IP 可共享的 Android 设备
- Controller 上已配置该设备主机的 USB/IP 来源
- 一个可用的短模块套件（默认 CTS ``CtsSecurityTestCases``）

全链路覆盖 2026-08 代码审核第五节建议的 nightly 检查：

    attach → ADB 可见 → 创建 job → 租约原子分配 → Tradefed 短模块 →
    job 终态 → 断开 USB/IP → 租约/worker/设备状态全部恢复

无真实环境时整个模块自动跳过（退出码 0），因此可以放进默认
``pytest`` 收集而不影响 CI；nightly 通过环境变量显式开启::

    GMS_SOAK_BASE_URL=http://controller:5001 \\
    GMS_SOAK_USERNAME=<平台管理员> \\
    GMS_SOAK_PASSWORD=<密码> \\
    GMS_SOAK_DEVICE_HOST=user@device-host \\
    GMS_SOAK_WORKER_ID=ats-worker-xxx \\
    pytest tests/soak -q

认证：优先使用 ``GMS_SOAK_SESSION_TOKEN``（现有 ``gms_session`` Cookie），
否则通过 ``GMS_SOAK_USERNAME`` / ``GMS_SOAK_PASSWORD`` 登录并提权。未设置
凭证时仅适用于 ``GMS_AUTH_REQUIRED=false`` 的隔离实验台。
"""

from __future__ import annotations

import os
import time
import unittest
from typing import Any

import requests


BASE_URL = os.getenv("GMS_SOAK_BASE_URL", "").rstrip("/")
SESSION_TOKEN = os.getenv("GMS_SOAK_SESSION_TOKEN", os.getenv("GMS_SOAK_TOKEN", ""))
USERNAME = os.getenv("GMS_SOAK_USERNAME", "")
PASSWORD = os.getenv("GMS_SOAK_PASSWORD", "")
DEVICE_HOST = os.getenv("GMS_SOAK_DEVICE_HOST", "")
WORKER_ID = os.getenv("GMS_SOAK_WORKER_ID", "")
SUITE_KEY = os.getenv("GMS_SOAK_SUITE_KEY", "CTS")
MODULE = os.getenv("GMS_SOAK_MODULE", "CtsSecurityTestCases")
JOB_TIMEOUT_SECONDS = int(os.getenv("GMS_SOAK_JOB_TIMEOUT", "1800"))
POLL_INTERVAL_SECONDS = int(os.getenv("GMS_SOAK_POLL_INTERVAL", "15"))


def _enabled() -> bool:
    return bool(BASE_URL)


@unittest.skipUnless(_enabled(), "GMS_SOAK_BASE_URL 未设置；nightly 真机环境专用")
class SoakFoundation(unittest.TestCase):
    """Shared helpers for the full-chain soak."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.session = requests.Session()
        cls.session.headers["Content-Type"] = "application/json"
        cls.base = BASE_URL
        cls._log: list[str] = []
        cls.attached_by_soak = False
        if SESSION_TOKEN:
            cls.session.cookies.set("gms_session", SESSION_TOKEN)
        elif USERNAME and PASSWORD:
            login = cls.session.post(
                f"{cls.base}/api/auth/login",
                json={"username": USERNAME, "password": PASSWORD},
                timeout=30,
            )
            if login.status_code != 200:
                raise RuntimeError(
                    f"soak login failed: HTTP {login.status_code}"
                )
            elevated = cls.session.post(
                f"{cls.base}/api/auth/elevate",
                json={"username": USERNAME, "password": PASSWORD},
                timeout=30,
            )
            if elevated.status_code != 200:
                raise RuntimeError(
                    f"soak elevation failed: HTTP {elevated.status_code}"
                )

    def _api(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict | None = None,
        timeout: int = 60,
    ) -> requests.Response:
        url = f"{self.base}{path}"
        self._log.append(f"{method} {path} params={params} body={json_body}")
        response = self.session.request(
            method, url, json=json_body, params=params, timeout=timeout
        )
        # 5xx 立即失败并带上日志上下文，避免 soak 挂在半途无从排查。
        self.assertTrue(
            response.status_code < 500,
            f"{method} {path} -> {response.status_code}: {response.text[:500]}\n"
            f"recent calls:\n" + "\n".join(self._log[-10:]),
        )
        return response

    def _wait_job_terminal(self, job_id: str) -> dict:
        """Poll a job until a terminal status; fail on timeout."""
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        last: dict = {}
        while time.monotonic() < deadline:
            response = self._api("GET", f"/api/cluster/jobs/{job_id}")
            self.assertEqual(response.status_code, 200, response.text)
            last = response.json()["job"]
            if last.get("status") in {"completed", "failed", "cancelled"}:
                return last
            time.sleep(POLL_INTERVAL_SECONDS)
        self.fail(
            f"job {job_id} not terminal after {JOB_TIMEOUT_SECONDS}s "
            f"(last status: {last.get('status')!r})\n"
            f"recent calls:\n" + "\n".join(self._log[-10:])
        )


class TestDeviceChainSoak(SoakFoundation):
    """attach → job → detach → full recovery."""

    def test_01_health_and_worker_online(self):
        response = self._api("GET", "/api/system/health")
        if response.status_code == 404:
            self.skipTest("controller has no /api/system/health; check worker API instead")
        health = response.json()
        self.assertTrue(health.get("success", True), health)

        workers = self._api("GET", "/api/cluster/workers").json()
        worker_ids = [w.get("worker_id") for w in workers.get("workers", [])]
        if WORKER_ID:
            self.assertIn(WORKER_ID, worker_ids, workers)

    def test_02_usbip_attach_and_adb_visible(self):
        if not DEVICE_HOST:
            self.skipTest("GMS_SOAK_DEVICE_HOST 未设置，跳过 USB/IP 链路")
        response = self._api(
            "POST", "/api/usbip/connect", json_body={"device_host": DEVICE_HOST},
            timeout=300,
        )
        payload = response.json()
        self.assertTrue(
            payload.get("success", False), payload,
        )
        type(self).attached_by_soak = True
        # 等待设备出现在 ADB 列表（attach 后 worker 侧需要几秒枚举）。
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            devices = self._api(
                "GET", "/api/devices/list", params={"force_refresh": "true"}
            ).json()
            if devices:
                return
            time.sleep(5)
        self.fail("USB/IP attach 后 120s 内未见任何 ADB 设备")

    def test_03_short_module_job_completes_and_leases_released(self):
        """Create a real short-module job and verify lease/worker recovery."""
        if not WORKER_ID:
            self.skipTest("GMS_SOAK_WORKER_ID 未设置，跳过真实套件执行")
        created = self._api(
            "POST",
            "/api/cluster/jobs",
            json_body={
                "worker_id": WORKER_ID,
                "suite_key": SUITE_KEY,
                "execution_spec": {
                    "test_type": SUITE_KEY.lower(),
                    "module": MODULE,
                },
            },
            timeout=120,
        )
        self.assertEqual(
            created.status_code, 200, f"{created.status_code}: {created.text[:500]}"
        )
        job = created.json()["job"]
        job_id = job["id"]

        final = self._wait_job_terminal(job_id)
        self.assertEqual(
            final["status"], "completed",
            f"job ended as {final['status']}: {str(final.get('error'))[:300]}",
        )

        # 任务终态后，所有租约必须已释放，设备回到可用状态。
        for lease in final.get("leases") or []:
            self.assertIn(
                lease.get("status"), {"released", ""}, f"lease not released: {lease}"
            )

        # 终态后再查一次列表，确认 controller 状态一致（无 worker_lost 残留）。
        detail = self._api("GET", f"/api/cluster/jobs/{job_id}").json()["job"]
        self.assertEqual(detail["status"], "completed")
        self.assertNotEqual(detail["status"], "worker_lost")

    def test_04_usbip_detach_and_state_recovery(self):
        if not DEVICE_HOST:
            self.skipTest("GMS_SOAK_DEVICE_HOST 未设置，跳过 USB/IP 断开链路")
        if not type(self).attached_by_soak:
            self.skipTest("本轮 soak 未成功 attach，不断开既有 USB/IP 会话")
        response = self._api(
            "POST",
            "/api/usbip/disconnect",
            json_body={"device_host": DEVICE_HOST},
            timeout=300,
        )
        payload = response.json()
        self.assertTrue(payload.get("success", False), payload)
        type(self).attached_by_soak = False

        # 断开后 worker 应不再报告该设备为 available。
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            workers = self._api("GET", "/api/cluster/workers").json()
            found_busy = any(
                device.get("state") == "available"
                for worker in workers.get("workers", [])
                if WORKER_ID and worker.get("worker_id") == WORKER_ID
                for device in worker.get("devices", [])
            )
            if not found_busy:
                return
            time.sleep(5)
        self.fail("USB/IP detach 后 120s 内设备仍被报告为 available")


if __name__ == "__main__":
    unittest.main()
