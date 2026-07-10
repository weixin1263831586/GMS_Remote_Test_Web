"""Execution adapters for automation stages."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Protocol

import requests

from features.automation.jenkins_client import JenkinsClient


class AutomationExecutor(Protocol):
    def trigger_build(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def poll_build(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def select_artifact(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def select_devices(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def flash(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def start_test(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def collect_report(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def analyze_report(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def report_result(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class StubAutomationExecutor:
    """Deterministic executor for Phase 1 orchestration tests and dry runs."""

    def __init__(self, fail_stage: str = ""):
        self.fail_stage = fail_stage

    def _result(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_stage == stage:
            return {"success": False, "error": f"{stage} failed by stub"}
        return {"success": True, **payload}

    def select_devices(self, run: dict[str, Any]) -> dict[str, Any]:
        devices = json.loads(run.get("devices_json") or "[]")
        return self._result("select_devices", {"devices": devices})

    def trigger_build(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("trigger_build", {
            "jenkins_queue_url": f"stub://jenkins/queue/{run.get('id', 'run')}",
            "jenkins_build_number": "1",
            "jenkins_build_url": f"stub://jenkins/{run.get('jenkins_job', 'job')}/1/",
        })

    def poll_build(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("poll_build", {
            "building": False,
            "result": "SUCCESS",
            "jenkins_build_number": run.get("jenkins_build_number") or "1",
            "jenkins_build_url": run.get("jenkins_build_url") or f"stub://jenkins/{run.get('jenkins_job', 'job')}/1/",
        })

    def select_artifact(self, run: dict[str, Any]) -> dict[str, Any]:
        job = run.get("jenkins_job", "job")
        number = run.get("jenkins_build_number") or "1"
        return self._result("select_artifact", {
            "artifact_url": f"stub://jenkins/{job}/{number}/update.img",
            "artifact_path": run.get("artifact_path", ""),
        })

    def flash(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("flash", {"artifact_path": run.get("artifact_path", "")})

    def start_test(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("start_test", {"test_plan": json.loads(run.get("test_plan_json") or "{}")})

    def collect_report(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("collect_report", {"report_timestamp": f"stub_report_{run['id']}"})

    def analyze_report(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("analyze_report", {"summary": {"total": 1, "fail": 0}})

    def report_result(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("report_result", {
            "comment": f"Automation run {run['id']} completed. Report: {run.get('report_timestamp', '')}",
        })


def _run_devices(run: dict[str, Any]) -> list[str]:
    devices = json.loads(run.get("devices_json") or "[]")
    serials = []
    for item in devices:
        if isinstance(item, dict) and item.get("serial"):
            serials.append(str(item["serial"]))
    return serials


def _run_test_plan(run: dict[str, Any]) -> dict[str, Any]:
    return json.loads(run.get("test_plan_json") or "{}")


def _run_jenkins_plan(run: dict[str, Any]) -> dict[str, Any]:
    plan = _run_test_plan(run)
    jenkins = plan.get("jenkins") if isinstance(plan.get("jenkins"), dict) else {}
    return jenkins


def _run_build_plan(run: dict[str, Any]) -> dict[str, Any]:
    plan = _run_test_plan(run)
    build = plan.get("build") if isinstance(plan.get("build"), dict) else {}
    return build


def _jenkins_config_from_run(run: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    jenkins = _run_jenkins_plan(run)
    config = dict(fallback or {})
    for key in ("base_url", "username", "api_token", "token", "verify_ssl", "crumb"):
        if key in jenkins:
            config[key] = jenkins[key]
    return config


class HttpAutomationExecutor:
    """Executor that reuses this service's existing HTTP APIs.

    This is intentionally opt-in from the worker API because it can flash devices
    and launch real GMS tests.
    """

    def __init__(
        self,
        base_url: str = "",
        session: Any = None,
        jenkins_config: dict[str, Any] | None = None,
        build_password_provider: Any = None,
        device_selector: Any = None,
        device_manager: Any = None,
    ):
        port = os.environ.get("GMS_PORT", "5001")
        self.base_url = (base_url or os.environ.get("GMS_AUTOMATION_BASE_URL") or f"http://127.0.0.1:{port}").rstrip("/")
        self.session = session or requests.Session()
        self.jenkins_config = jenkins_config or {}
        self.build_password_provider = build_password_provider
        self.device_selector = device_selector
        self.device_manager = device_manager

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _json_response(self, response: Any) -> dict[str, Any]:
        response.raise_for_status()
        data = response.json()
        if data.get("success") is False:
            return {"success": False, "error": data.get("error") or data.get("message") or "request failed", "response": data}
        return {"success": True, "response": data, **(data.get("data") if isinstance(data.get("data"), dict) else {})}

    def trigger_build(self, run: dict[str, Any]) -> dict[str, Any]:
        build_plan = _run_build_plan(run)
        if build_plan.get("provider") == "ssh" or build_plan.get("server_id"):
            try:
                from features.build import get_build_service

                parameters = dict(build_plan.get("parameters") or {})
                for key in ("workspace", "lunch_target", "build_command"):
                    if key in build_plan and key not in parameters:
                        parameters[key] = build_plan[key]
                job = get_build_service().create_job(
                    {
                        "server_id": build_plan.get("server_id", ""),
                        "template_id": build_plan.get("template_id", "rk-android-build"),
                        "server_password": (
                            self.build_password_provider(run.get("id", ""))
                            if self.build_password_provider
                            else build_plan.get("server_password", "")
                        ),
                        "parameters": parameters,
                        "source_type": run.get("source_type", "automation"),
                        "source_key": f"automation-build:{run.get('id', '')}",
                        "owner": run.get("owner", ""),
                        "automation_run_id": run.get("id", ""),
                    },
                    start=True,
                )
                return {
                    "success": True,
                    "jenkins_queue_url": f"build://{job['id']}",
                    "jenkins_build_number": job["id"],
                    "jenkins_build_url": f"/api/build/jobs/{job['id']}",
                    "build_job_id": job["id"],
                    "build_status": job["status"],
                }
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        config = _jenkins_config_from_run(run, self.jenkins_config)
        if not config.get("base_url"):
            return {"success": False, "error": "Jenkins config missing"}
        jenkins_plan = _run_jenkins_plan(run)
        result = JenkinsClient(config).trigger_build(run.get("jenkins_job", ""), jenkins_plan.get("parameters") or {})
        return {
            **result,
            "jenkins_queue_url": result.get("queue_url", ""),
        }

    def poll_build(self, run: dict[str, Any]) -> dict[str, Any]:
        build_plan = _run_build_plan(run)
        if build_plan.get("provider") == "ssh" or build_plan.get("server_id"):
            try:
                from features.build import get_build_service

                job_id = run.get("jenkins_build_number") or ""
                if not job_id and str(run.get("jenkins_queue_url") or "").startswith("build://"):
                    job_id = str(run["jenkins_queue_url"])[len("build://"):]
                if not job_id:
                    return {"success": False, "error": "Build job id missing"}
                job = get_build_service().poll_job(job_id)
                artifacts = job.get("artifacts") or []
                result = {
                    "success": True,
                    "building": job.get("status") in {"queued", "running"},
                    "result": "SUCCESS" if job.get("status") == "completed" else ("FAILURE" if job.get("status") == "failed" else ""),
                    "jenkins_build_number": job_id,
                    "jenkins_build_url": f"/api/build/jobs/{job_id}",
                    "build_job_id": job_id,
                    "build": job,
                }
                if artifacts:
                    artifact = artifacts[0]
                    result["artifact_path"] = artifact.get("path", "")
                    result["artifact_url"] = artifact.get("path", "")
                    result["artifact"] = artifact
                if job.get("status") == "failed":
                    return {"success": False, "error": job.get("error") or "Build failed", **result}
                return result
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        config = _jenkins_config_from_run(run, self.jenkins_config)
        if not config.get("base_url"):
            return {"success": False, "error": "Jenkins config missing"}
        jenkins_plan = _run_jenkins_plan(run)
        client = JenkinsClient(config)
        build_number = run.get("jenkins_build_number", "")
        if not build_number and run.get("jenkins_queue_url"):
            queue = client.get_queue_item(run["jenkins_queue_url"])
            if not queue.get("success"):
                return queue
            build_number = queue.get("build_number", "")
            if not build_number:
                return {"success": True, "building": True, "jenkins_queue_url": run["jenkins_queue_url"]}
        if not build_number:
            return {"success": False, "error": "Jenkins build number missing"}
        result = client.get_build(run.get("jenkins_job", ""), build_number)
        result["jenkins_build_number"] = build_number
        if result.get("success") and not result.get("building") and result.get("result") == "SUCCESS":
            artifact = JenkinsClient.select_artifact(result, str(jenkins_plan.get("artifact_pattern") or ""))
            if artifact.get("success"):
                result["artifact_url"] = artifact.get("url", "")
                result["artifact_path"] = artifact.get("relative_path", "")
                result["artifact"] = artifact.get("artifact")
        return result

    def select_artifact(self, run: dict[str, Any]) -> dict[str, Any]:
        if run.get("artifact_url") or run.get("artifact_path"):
            return {"success": True, "artifact_url": run.get("artifact_url", ""), "artifact_path": run.get("artifact_path", "")}
        return {"success": False, "error": "No artifact selected"}

    def select_devices(self, run: dict[str, Any]) -> dict[str, Any]:
        devices = _run_devices(run)
        if devices:
            return {"success": True, "devices": [{"serial": serial} for serial in devices]}
        # No manual devices — try automatic selection via device_selector.
        if self.device_selector is not None:
            return self.device_selector.select(run)
        return {"success": False, "error": "No devices selected"}

    def flash(self, run: dict[str, Any]) -> dict[str, Any]:
        plan = _run_test_plan(run)
        flash_plan = plan.get("flash") if isinstance(plan.get("flash"), dict) else {}
        if flash_plan.get("mode") == "skip":
            return {"success": True, "skipped": True}
        devices = _run_devices(run)
        firmware_path = run.get("artifact_path") or run.get("artifact_url")
        if not firmware_path:
            return {"success": False, "error": "No firmware artifact path/url"}
        response = self.session.post(
            self._url(f"/api/burn/firmware?devices={','.join(devices)}"),
            data={"firmware_path": firmware_path},
            timeout=3600,
        )
        result = self._json_response(response)
        if not result.get("success"):
            return result
        verify = flash_plan.get("verify") if isinstance(flash_plan.get("verify"), dict) else {}
        if verify and self.device_manager is not None:
            check = self._verify_post_flash(devices, verify)
            if not check["success"]:
                return check
        return result

    def _verify_post_flash(self, devices: list[str], verify: dict[str, Any]) -> dict[str, Any]:
        """Verify ro.product/fingerprint after flash; retry while the device reboots."""
        expected_product = str(verify.get("product") or "").strip()
        fingerprint_contains = str(verify.get("fingerprint_contains") or "").strip()
        attempts = int(verify.get("retries") or 3)
        delay = int(verify.get("retry_delay") or 10)
        last_error = ""
        for _ in range(max(1, attempts)):
            for serial in devices:
                try:
                    info = self.device_manager.get_device_info(serial) or {}
                except Exception as exc:
                    last_error = f"get_device_info failed for {serial}: {exc}"
                    info = {}
                model = str(info.get("model") or "")
                fingerprint = str(info.get("fingerprint") or "")
                if expected_product and expected_product.lower() not in model.lower():
                    last_error = f"product mismatch on {serial}: expected '{expected_product}', got '{model}'"
                    break
                if fingerprint_contains and fingerprint_contains.lower() not in fingerprint.lower():
                    last_error = f"fingerprint mismatch on {serial}: '{fingerprint}' missing '{fingerprint_contains}'"
                    break
            else:
                return {"success": True, "verified": True}
            time.sleep(delay)
        return {"success": False, "error": f"post-flash verification failed: {last_error}"}

    def start_test(self, run: dict[str, Any]) -> dict[str, Any]:
        plan = _run_test_plan(run)
        payload = {
            "test_type": plan.get("test_type", ""),
            "test_module": plan.get("test_module") or (plan.get("modules") or [""])[0],
            "test_case": plan.get("test_case", ""),
            "retry_dir": plan.get("retry_dir", ""),
            "test_suite": plan.get("test_suite", ""),
            "local_server": plan.get("local_server", ""),
            "devices": _run_devices(run),
        }
        response = self.session.post(self._url("/api/test/start"), json=payload, timeout=30)
        return self._json_response(response)

    def collect_report(self, run: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(self._url("/api/reports/list"), timeout=30)
        data = self._json_response(response)
        if not data.get("success"):
            return data
        reports = data.get("response", {}).get("reports") or data.get("response", {}).get("data", {}).get("reports") or []
        if not reports:
            return {"success": False, "error": "No test reports found"}
        timestamp = reports[0].get("timestamp") or reports[0].get("report_timestamp") or ""
        return {"success": True, "report_timestamp": timestamp, "report": reports[0]}

    def analyze_report(self, run: dict[str, Any]) -> dict[str, Any]:
        report_timestamp = run.get("report_timestamp", "")
        if not report_timestamp:
            return {"success": False, "error": "No report timestamp"}
        response = self.session.post(
            self._url("/api/reports/analyze"),
            data={"mode": "saved", "report_timestamp": report_timestamp},
            timeout=300,
        )
        return self._json_response(response)

    def report_result(self, run: dict[str, Any]) -> dict[str, Any]:
        # Fire completion notifications (email/Gerrit/Redmine), each gated by the
        # profile's reporting block. Never raises — a missing transport must not
        # block the run from completing.
        notifications = {"sent": [], "reason": "no reporting config"}
        try:
            from features.automation.notifier import notify_run_completion

            notifications = notify_run_completion(run)
        except Exception:
            pass
        return {
            "success": True,
            "report_timestamp": run.get("report_timestamp", ""),
            "result": json.loads(run.get("result_json") or "{}"),
            "notifications": notifications,
        }
