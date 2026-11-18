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
SUITE_KEY = os.getenv("GMS_SOAK_SUITE_KEY") or "CTS"
MODULE = os.getenv("GMS_SOAK_MODULE") or "CtsSecurityTestCases"
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
        cls.preexisting_session = False
        cls.active_job_id = ""
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
        # This job was created by this soak invocation; never cancel someone
        # else's workload. Stop only our own timed-out CTS, then wait for both
        # terminal state AND released device leases before allowing USB detach.
        stopped = self._cancel_owned_timed_out_job(job_id)
        self.fail(
            f"CTS job {job_id} exceeded {JOB_TIMEOUT_SECONDS}s; "
            f"last={last.get('status')!r}; "
            f"cancelled_and_released={stopped}. "
            "If False, leave USB/IP attached and investigate the Worker."
        )

    def _cancel_owned_timed_out_job(self, job_id: str) -> bool:
        if job_id != type(self).active_job_id:
            raise AssertionError("Refusing to cancel an unowned CTS job")
        try:
            response = self._api(
                "POST", f"/api/cluster/jobs/{job_id}/cancel", timeout=30,
            )
            if response.status_code != 200:
                print("Owned timed-out CTS cancellation request rejected")
                return False
            # Controller may enter 'stopping' before the Worker terminates
            # Tradefed and reports the final lease transaction.
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                poll = self._api("GET", f"/api/cluster/jobs/{job_id}", timeout=15)
                if poll.status_code == 200:
                    job = poll.json().get("job") or {}
                    if (
                        job.get("status") in {"completed", "failed", "cancelled"}
                        and job.get("leases")
                        and all(lease.get("status") == "released"
                                for lease in job["leases"])
                    ):
                        type(self).active_job_id = ""
                        print("Owned CTS stopped or terminal; all leases released")
                        return True
                time.sleep(5)
        except (requests.RequestException, AssertionError, ValueError, KeyError):
            print("Timed-out CTS stop/lease confirmation failed")
        return False


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

        # A previous GitHub run can end while Tradefed continues on the
        # actual Worker. Never start a new hardware run over an active job.
        jobs_response = self._api(
            "GET", "/api/cluster/jobs",
            params={"include_active": "true", "limit": 500},
        )
        self.assertEqual(jobs_response.status_code, 200, "job inventory unavailable")
        active = [
            item for item in jobs_response.json().get("jobs") or []
            if item.get("assigned_worker_id") == WORKER_ID
            and item.get("status") not in {"completed", "failed", "cancelled"}
        ]
        self.assertFalse(
            active,
            "Selected Worker has unfinished platform jobs; refusing to "
            "re-attach, create CTS, or detach USB/IP. "
            "Inspect their termination and device leases first: "
            + ", ".join(str(item.get("id") or "unknown") for item in active[:5]),
        )

    def _target_assignment(self) -> dict | None:
        """Read persistent USB/IP ownership; never steal or detach another session."""
        resp = self._api("GET", "/api/usbip/assignments")
        self.assertEqual(resp.status_code, 200, "cannot inspect USB/IP ownership")
        groups = resp.json().get("cluster_selections") or []
        matches = [
            group for group in groups
            if group.get("device_host") == DEVICE_HOST
            and DEVICE_BUSIDS[0] in (group.get("busids") or [])
        ]
        self.assertLessEqual(len(matches), 1, "duplicate USB/IP assignment")
        if not matches:
            return None
        current = matches[0]
        self.assertEqual(
            current.get("worker_id"), WORKER_ID,
            "configured USB/IP BUSID is owned by a different Worker",
        )
        return current

    @staticmethod
    def _select_serial(
        available: set[str],
        baseline: set[str],
        reported: set[str],
    ) -> str:
        """Require an explicit or uniquely attributable DUT; never choose first."""
        if DEVICE_SERIAL:
            if reported and DEVICE_SERIAL not in reported:
                raise AssertionError(
                    "configured DUT serial differs from source USB/IP inventory"
                )
            return DEVICE_SERIAL if DEVICE_SERIAL in available else ""
        if len(reported) == 1:
            candidate = next(iter(reported))
            return candidate if candidate in available else ""
        # Only one entirely new device is an admissible fallback.
        new_devices = available - baseline
        return next(iter(new_devices)) if len(new_devices) == 1 else ""

    def test_02_usbip_attach_and_adb_visible(self):
        self.assertTrue(DEVICE_HOST and WORKER_ID and len(DEVICE_BUSIDS) == 1)
        baseline = self._adb_ready_serials(self._worker_devices())
        existing = self._target_assignment()
        reported: set[str] = set()
        transport_connected = False
        adb_ready_at_attach = False

        if existing:
            state = (existing.get("statuses_by_busid") or {}).get(DEVICE_BUSIDS[0])
            self.assertEqual(
                state, "attached",
                "USB/IP target has an existing uncertain session; refusing takeover",
            )
            reported = set(
                (existing.get("device_serials_by_busid") or {})
                .get(DEVICE_BUSIDS[0]) or []
            )
            self.assertTrue(
                DEVICE_SERIAL or len(reported) == 1,
                "pre-attached USB/IP target lacks a unique serial identifier",
            )
            type(self).preexisting_session = True
            transport_connected = True
            print("Target BUSID was already attached; keep existing session")
        else:
            response = self._api(
                "POST", "/api/usbip/connect",
                json_body={
                    "device_host": DEVICE_HOST, "worker_id": WORKER_ID,
                    "busids": DEVICE_BUSIDS, "manual_connect": True,
                },
                timeout=300,
            )
            self.assertEqual(response.status_code, 200, "USB/IP attach HTTP failure")
            payload = response.json()
            self.assertTrue(payload.get("success", False), "USB/IP attach rejected")
            # A successful USB/IP transport request does NOT prove ADB readiness.
            self.assertEqual(payload.get("worker_id"), WORKER_ID)
            self.assertEqual(payload.get("busids"), DEVICE_BUSIDS)
            type(self).attached_by_soak = True
            transport_connected = bool(payload.get("transport_connected"))
            adb_ready_at_attach = bool(payload.get("adb_ready"))
            reported = {
                str(serial) for serial in payload.get("device_serials") or []
                if str(serial)
            }
            self.assertTrue(
                transport_connected, "USB/IP attach returned without transport evidence"
            )
            print(
                "USB/IP transport established; ADB ready in attach response:",
                adb_ready_at_attach,
            )

        refreshed = False
        start = time.monotonic()
        deadline = start + 120
        last_available: set[str] = set()
        while time.monotonic() < deadline:
            last_available = self._adb_ready_serials(self._worker_devices())
            matched = self._select_serial(last_available, baseline, reported)
            if matched:
                type(self).attached_serial = matched
                print("Explicit DUT confirmed in the requested Worker's ADB pool")
                return
            if not refreshed and time.monotonic() - start >= 10:
                # Force an actual Worker adb inventory refresh, not just a
                # Controller DB read; leave unrelated device state untouched.
                refreshed = True
                try:
                    result = self._api(
                        "POST", f"/api/cluster/workers/{WORKER_ID}/refresh",
                        params={"inventory": "devices"}, timeout=30,
                    )
                    print("Worker inventory refresh HTTP:", result.status_code)
                except requests.RequestException:
                    print("Worker inventory refresh unreachable")
            time.sleep(5)
        state = "unknown"
        try:
            state = str(
                ((self._target_assignment() or {}).get("statuses_by_busid") or {})
                .get(DEVICE_BUSIDS[0]) or "none"
            )
        except (AssertionError, requests.RequestException):
            pass
        self.fail(
            "USB/IP transport connected, but target DUT never became ADB-ready "
            "on its selected Worker; "
            f"before={len(baseline)} current={len(last_available)} "
            f"assigned_transport={state} "
            f"initial_adb_ready={adb_ready_at_attach} "
            f"inventory_refresh_attempted={refreshed}. "
            "Check USB debugging authorization, device boot state, Worker ADB "
            "server identity and USB/IP enumeration."
        )

    def test_03_short_module_job_completes_and_leases_released(self):
        serial = type(self).attached_serial
        if not serial:
            self.skipTest("USB/IP ADB identification did not pass; no CTS job submitted")
        devices = self._worker_devices()
        candidates = [
            d for d in devices
            if d.get("worker_id") == WORKER_ID and d.get("serial") == serial
            and d.get("state") == "available"
        ]
        self.assertEqual(len(candidates), 1, devices)
        self.assertFalse(
            candidates[0].get("claimed"),
            "Selected DUT has an existing lease/claim; refusing new CTS job",
        )

        # Use actual Worker test-runner state, not just its ADB inventory.
        directory = self._api("GET", "/api/cluster/workers").json()
        target = next(
            (worker for worker in directory.get("workers") or []
             if worker.get("id") == WORKER_ID), None,
        )
        self.assertIsNotNone(target, "Target Worker registration vanished")
        agent_version = str(target.get("agent_version") or "")
        self.assertFalse(
            agent_version.startswith("controller-"),
            "Selected Worker is a Controller-only inventory bridge, not a "
            "Tradefed-capable Worker Agent. Install/start the actual Worker "
            "Agent or choose its registered Worker ID in soak.json.",
        )
        self.assertIn(target.get("status"), {"online", "busy"})
        self.assertFalse(
            int(target.get("unknown_external_jobs") or 0),
            "Worker reports an external Tradefed job; refusing to submit CTS",
        )
        self.assertLess(
            int(target.get("running_jobs") or 0),
            max(1, int(target.get("max_jobs") or 1)),
            "Target Worker has no spare test execution slots",
        )
        # The Worker directory and scheduler share the same admission policy.
        # A Worker can be busy and have free slots but still be ineligible
        # because disk free space is under its configured safety floor.
        admission = target.get("admission_reasons") or []
        self.assertFalse(
            target.get("admission_blocked") or admission,
            "Selected Worker is not eligible for new CTS jobs: "
            f"reasons={list(admission)}, "
            f"disk_free_gb={target.get('disk_free_gb')}, "
            f"memory_available_gb={target.get('memory_available_gb')}, "
            f"running_jobs={target.get('running_jobs')}, "
            f"max_jobs={target.get('max_jobs')}. "
            "Restore Worker admission prerequisites before rerunning; "
            "never bypass low-disk, in-use-device or external-Tradefed guards.",
        )

        # The suite listing may use a local compatibility scan even when
        # the dispatch database has no suite inventory yet. Refresh the
        # actual Worker and persisted suite inventory before selecting CTS.
        refreshed = self._api(
            "POST", f"/api/cluster/workers/{WORKER_ID}/refresh",
            params={"inventory": "suites"}, timeout=45,
        )
        self.assertEqual(
            refreshed.status_code, 200,
            "Worker suite refresh failed; refusing a non-dispatchable CTS job",
        )
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
        self.assertTrue(
            usable,
            f"Worker {WORKER_ID} has no advertised executable {SUITE_KEY} "
            "suite after explicit inventory refresh",
        )
        suite = usable[0]
        self.assertIn(
            str(candidates[0].get("transport") or "local_usb").lower(),
            {"usbip", "local_usb", "adb_proxy"},
            "Device transport not supported for CTS dispatch",
        )
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
                    "test_case": os.getenv("GMS_SOAK_TEST_CASE", ""),
                    "execution_timeout_seconds": JOB_TIMEOUT_SECONDS,
                },
            },
            timeout=120,
        )
        self.assertEqual(
            created.status_code, 200,
            "CTS job admission failed; inspect Worker agent registration, "
            "suite inventory, device transport, free disk/RAM, running "
            "jobs and external Tradefed activity. "
            f"HTTP {created.status_code}: {created.text[:350]} "
            f"(worker_status={target.get('status')}, "
            f"agent_version={agent_version[:40]}, "
            f"device_transport={candidates[0].get('transport')}, "
            f"suite_key={suite.get('suite_key')}, "
            f"free_disk_gb={target.get('disk_free_gb')}, "
            f"free_memory_gb={target.get('memory_available_gb')}, "
            f"running_jobs={target.get('running_jobs')}, "
            f"external_tradefed={target.get('unknown_external_jobs')})",
        )
        job = created.json()["job"]
        job_id = job["id"]
        type(self).active_job_id = job_id

        final = self._wait_job_terminal(job_id)
        # Never clear the job ownership guard until Controller and Worker
        # have reconciled every associated physical-device lease.
        leases = final.get("leases") or []
        if (
            final["status"] in {"completed", "failed", "cancelled"}
            and leases
            and all(lease.get("status") == "released" for lease in leases)
        ):
            type(self).active_job_id = ""
        self.assertEqual(
            final["status"], "completed",
            f"job ended as {final['status']}: {str(final.get('error'))[:300]}",
        )
        self.assertTrue(leases, f"真机任务 {job_id} 无设备租约")
        for lease in leases:
            self.assertEqual(lease.get("status"), "released", lease)
        detail = self._api("GET", f"/api/cluster/jobs/{job_id}").json()["job"]
        self.assertEqual(detail["status"], "completed")

    def test_04_usbip_detach_and_state_recovery(self):
        if not type(self).attached_serial:
            self.skipTest("DUT was not ADB-ready; cleanup handled by tearDownClass")
        self.assertFalse(
            type(self).active_job_id, "CTS job still active; refuse detach"
        )
        if type(self).preexisting_session:
            # Never disconnect USB/IP sessions that predate this verification.
            current = self._target_assignment()
            self.assertIsNotNone(current, "preexisting USB/IP assignment vanished")
            self.assertIn(type(self).attached_serial,
                          self._adb_ready_serials(self._worker_devices()))
            print("Preexisting USB/IP session retained and still ADB-ready")
            return
        self.assertTrue(type(self).attached_by_soak)
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
            if cls.attached_by_soak and cls.active_job_id:
                raise AssertionError(
                    "Job still potentially active; refusing unsafe USB/IP "
                    f"detach of active job {cls.active_job_id}. "
                    "Investigate job and release it before manual recovery."
                )
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
