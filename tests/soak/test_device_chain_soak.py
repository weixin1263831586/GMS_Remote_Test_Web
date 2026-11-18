"""Nightly real-device soak tests: Controller → Worker → USB/IP → suite → recovery.

这些测试驱动真实部署（默认 ``http://127.0.0.1:5001``），需要：

- 一台真实 Worker Agent 与一台通过 USB/IP 可共享的 Android 设备
- Controller 上已配置该设备主机的 USB/IP 来源
- 一个可用的短模块套件（默认 CTS ``CtsSecurityTestCases``）

全链路覆盖以下 nightly 检查：

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
DEVICE_SERIAL = os.getenv("GMS_SOAK_DEVICE_SERIAL", "")
DEVICE_BUSIDS = [
    item.strip() for item in os.getenv("GMS_SOAK_BUSIDS", "").split(",")
    if item.strip()
]
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
        cls.attached_serial = ""
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
    """Operate only the explicitly selected USB/IP port and verify its DUT."""

    def _worker_devices(self) -> list[dict]:
        response = self._api(
            "GET", "/api/cluster/devices", params={"worker_id": WORKER_ID},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload.get("success"), payload)
        return payload["devices"]

    @staticmethod
    def _adb_ready_serials(devices: list[dict]) -> set[str]:
        return {
            str(device.get("serial") or "")
            for device in devices
            if device.get("state") in {"available", "online"}
            and str(device.get("serial") or "")
        }

    def test_01_health_and_worker_online(self):
        self.assertTrue(DEVICE_HOST, "GMS_SOAK_DEVICE_HOST 必须配置，禁止跳过真机")
        self.assertTrue(WORKER_ID, "GMS_SOAK_WORKER_ID 必须配置，禁止跳过真机")
        self.assertTrue(DEVICE_BUSIDS, "GMS_SOAK_BUSIDS 必须指定测试设备的 USB/IP BUSID")
        health = self._api("GET", "/api/system/health")
        self.assertEqual(health.status_code, 200, health.text)
        self.assertEqual(health.json().get("status"), "alive")
        workers = self._api("GET", "/api/cluster/workers")
        self.assertEqual(workers.status_code, 200, workers.text)
        directory = workers.json()
        matching = [worker for worker in directory.get("workers", [])
                    if worker.get("id") == WORKER_ID]
        self.assertEqual(len(matching), 1, directory)
        self.assertIn(matching[0].get("status"), {"online", "busy"}, directory)

    def test_02_usbip_attach_and_adb_visible(self):
        self.assertTrue(DEVICE_HOST and WORKER_ID and DEVICE_BUSIDS)
        baseline = self._adb_ready_serials(self._worker_devices())
        response = self._api(
            "POST", "/api/usbip/connect",
            json_body={
                "device_host": DEVICE_HOST, "worker_id": WORKER_ID,
                "busids": DEVICE_BUSIDS, "manual_connect": True,
            },
            timeout=300,
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload.get("success", False), payload)
        type(self).attached_by_soak = True
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            available = self._adb_ready_serials(self._worker_devices())
            new_devices = available - baseline
            if DEVICE_SERIAL and DEVICE_SERIAL in new_devices:
                type(self).attached_serial = DEVICE_SERIAL
                return
            if not DEVICE_SERIAL and len(new_devices) == 1:
                type(self).attached_serial = next(iter(new_devices))
                return
            time.sleep(5)
        self.fail(
            "USB/IP attach 后未在指定 Worker 上发现唯一的新设备；"
            "请检查 BUSID/序列号及目标 Worker 的 ADB 心跳。"
        )

    def test_03_short_module_job_completes_and_leases_released(self):
        serial = type(self).attached_serial
        self.assertTrue(serial, "未确认新接入的真机序列号，禁止执行测试")
        devices = self._worker_devices()
        candidates = [
            d for d in devices
            if d.get("worker_id") == WORKER_ID and d.get("serial") == serial
            and d.get("state") == "available"
        ]
        self.assertEqual(len(candidates), 1, devices)
        suites_response = self._api(
            "GET", "/api/cluster/suites", params={"worker_id": WORKER_ID},
        )
        self.assertEqual(suites_response.status_code, 200, suites_response.text)
        suites = suites_response.json().get("suites", [])
        usable = [
            item for item in suites if item.get("available")
            and item.get("worker_id") == WORKER_ID
            and (str(item.get("suite_key") or "") == SUITE_KEY
                 or str(item.get("suite_type") or "").upper() == SUITE_KEY.upper())
            and item.get("tools_path")
        ]
        self.assertTrue(usable, f"Worker {WORKER_ID} 没有可执行的 {SUITE_KEY} 套件")
        suite = usable[0]
        suite_path = suite["tools_path"]
        created = self._api(
            "POST", "/api/cluster/jobs",
            json_body={
                "worker_id": WORKER_ID,
                "suite_key": suite["suite_key"],
                "suite_path": suite_path,
                "devices": [candidates[0]["id"]],
                "device_count": 1,
                "execution_spec": {
                    "suite_path": suite_path,
                    "test_type": str(suite["suite_type"]).lower(),
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
        leases = final.get("leases") or []
        self.assertTrue(leases, f"真机任务 {job_id} 无设备租约")
        for lease in leases:
            self.assertEqual(lease.get("status"), "released", lease)
        detail = self._api("GET", f"/api/cluster/jobs/{job_id}").json()["job"]
        self.assertEqual(detail["status"], "completed")

    def test_04_usbip_detach_and_state_recovery(self):
        self.assertTrue(type(self).attached_by_soak, "未接入 USB/IP，不能验证断开")
        self.assertTrue(type(self).attached_serial, "未记录真机序列号")
        response = self._api(
            "POST", "/api/usbip/disconnect",
            json_body={
                "device_host": DEVICE_HOST, "worker_id": WORKER_ID,
                "busids": DEVICE_BUSIDS,
            },
            timeout=300,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json().get("success", False), response.text)
        type(self).attached_by_soak = False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            available = self._adb_ready_serials(self._worker_devices())
            if type(self).attached_serial not in available:
                return
            time.sleep(5)
        self.fail("USB/IP detach 后目标设备仍在 Worker 可用池")

    @classmethod
    def tearDownClass(cls):
        # Failure after attach must not strand a lab USB/IP session. Scope
        # cleanup to the busids owned by this invocation, never all devices.
        try:
            if cls.attached_by_soak:
                response = cls.session.post(
                    f"{cls.base}/api/usbip/disconnect",
                    json={
                        "device_host": DEVICE_HOST, "worker_id": WORKER_ID,
                        "busids": DEVICE_BUSIDS,
                    },
                    timeout=300,
                )
                if response.status_code != 200 or not response.json().get("success"):
                    raise AssertionError(
                        f"真机回收失败: HTTP {response.status_code} "
                        f"{response.text[:250]}"
                    )
        finally:
            cls.session.close()


if __name__ == "__main__":
    unittest.main()
