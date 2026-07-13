"""Execution adapters for automation stages."""

from __future__ import annotations

import json
import os
import posixpath
import shlex
import time
from pathlib import Path
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

    def poll_test(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def cancel(self, run: dict[str, Any]) -> dict[str, Any]:
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

    def poll_test(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("poll_test", {"running": False})

    def cancel(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("cancel", {"cancelled": True})

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


def _run_result(run: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(run.get("result_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


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
        build_plan = _run_build_plan(run)
        is_ssh_build = bool(build_plan.get("provider") == "ssh" or build_plan.get("server_id"))
        if is_ssh_build:
            firmware_path = run.get("artifact_path") or run.get("artifact_url")
        else:
            firmware_path = run.get("artifact_url") or run.get("artifact_path")
        if not firmware_path:
            return {"success": False, "error": "No firmware artifact path/url"}
        worker_id = str(plan.get("worker_id") or "worker-local")
        if is_ssh_build:
            staged = self._stage_ssh_build_artifact(run, str(firmware_path))
            if not staged.get("success"):
                return staged
            firmware_path = staged["firmware_path"]
        elif str(firmware_path).startswith(("http://", "https://")):
            staged = self._stage_http_artifact(run, str(firmware_path))
            if not staged.get("success"):
                return staged
            firmware_path = staged["firmware_path"]
        if worker_id != "worker-local":
            path = Path(str(firmware_path)).expanduser()
            if not path.is_file():
                return {"success": False, "error": "Cluster firmware must be staged on the Controller first"}
            devices_payload = ",".join(devices)
            with path.open("rb") as source:
                response = self.session.post(self._url("/api/cluster/firmware/stage"),
                    data={"worker_id": worker_id, "devices": devices_payload},
                    files={"firmware_file": (path.name, source, "application/octet-stream")}, timeout=3600)
            result = self._json_response(response)
            command_id = str(result.get("response", {}).get("command_id") or result.get("command_id") or "")
            if result.get("success") and command_id:
                for _ in range(900):
                    polled = self._json_response(self.session.get(
                        self._url(f"/api/cluster/commands/{command_id}"), timeout=30))
                    command = polled.get("response", {}).get("command") or {}
                    if command.get("status") == "completed":
                        return {"success": True, "command_id": command_id,
                                "flash_result": command.get("result") or {}}
                    if command.get("status") in {"failed", "cancelled"}:
                        return {"success": False, "error": command.get("error") or "cluster flash failed"}
                    time.sleep(2)
                return {"success": False, "error": "cluster flash timed out"}
            return result
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

    def _stage_ssh_build_artifact(self, run: dict[str, Any], artifact_path: str) -> dict[str, Any]:
        """Stream an SSH build artifact onto the test host before flashing.

        Build and test hosts are commonly different machines. Passing the
        build host's absolute path to ``/api/burn/firmware`` only works by
        accident when both roles share a filesystem, so copy it explicitly
        without buffering a multi-gigabyte image in the web process.
        """
        source_ssh = None
        try:
            import paramiko

            from features.build import get_build_service
            from features.system import ssh_manager
            from features.test_execution import get_default_suites_path
            from foundation.config import config_manager

            build_service = get_build_service()
            job_id = str(run.get("jenkins_build_number") or "")
            job = build_service.get_job(job_id)
            known_paths = {str(item.get("path") or "") for item in job.get("artifacts") or []}
            if artifact_path not in known_paths:
                return {"success": False, "error": "Selected artifact is not part of the completed build job"}

            server = build_service._with_runtime_password(
                build_service._get_server(job["server_id"]),
                build_service._runtime_passwords.get(job_id, ""),
            )
            backend = build_service._backend(server)
            connect_kwargs = backend._connect_kwargs(server)
            source_ssh = paramiko.SSHClient()
            source_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            source_ssh.connect(**connect_kwargs)

            target_config = config_manager.load_config()
            target_dir = get_default_suites_path(target_config)
            filename = posixpath.basename(artifact_path.rstrip("/"))
            target_path = posixpath.join(target_dir, f"{run.get('id', 'ats')}_{filename}")
            with ssh_manager.optional_connection(target_config) as target_ssh:
                if not target_ssh:
                    return {"success": False, "error": "Cannot connect to test host to stage firmware"}
                ssh_manager.execute_command(target_ssh, f"mkdir -p {shlex.quote(target_dir)}", timeout=30)
                source_sftp = source_ssh.open_sftp()
                target_sftp = target_ssh.open_sftp()
                try:
                    with source_sftp.open(artifact_path, "rb") as source, target_sftp.open(target_path, "wb") as target:
                        while chunk := source.read(4 * 1024 * 1024):
                            target.write(chunk)
                finally:
                    source_sftp.close()
                    target_sftp.close()
            return {"success": True, "firmware_path": target_path}
        except Exception as exc:
            return {"success": False, "error": f"Failed to stage build artifact on test host: {exc}"}
        finally:
            if source_ssh is not None:
                source_ssh.close()

    def _stage_http_artifact(self, run: dict[str, Any], artifact_url: str) -> dict[str, Any]:
        """Stream a Jenkins/HTTP artifact to the test host without local buffering."""
        response = None
        try:
            from urllib.parse import urlparse

            from features.system import ssh_manager
            from features.test_execution import get_default_suites_path
            from foundation.config import config_manager

            jenkins = _jenkins_config_from_run(run, self.jenkins_config)
            auth = None
            if jenkins.get("username") and (jenkins.get("api_token") or jenkins.get("token")):
                auth = (jenkins["username"], jenkins.get("api_token") or jenkins.get("token"))
            response = requests.get(
                artifact_url,
                auth=auth,
                verify=bool(jenkins.get("verify_ssl", True)),
                stream=True,
                timeout=(20, 3600),
            )
            response.raise_for_status()
            filename = posixpath.basename(urlparse(artifact_url).path.rstrip("/")) or "update.img"
            target_config = config_manager.load_config()
            target_dir = get_default_suites_path(target_config)
            target_path = posixpath.join(target_dir, f"{run.get('id', 'ats')}_{filename}")
            with ssh_manager.optional_connection(target_config) as target_ssh:
                if not target_ssh:
                    return {"success": False, "error": "Cannot connect to test host to stage firmware"}
                ssh_manager.execute_command(target_ssh, f"mkdir -p {shlex.quote(target_dir)}", timeout=30)
                sftp = target_ssh.open_sftp()
                try:
                    with sftp.open(target_path, "wb") as target:
                        for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                            if chunk:
                                target.write(chunk)
                finally:
                    sftp.close()
            return {"success": True, "firmware_path": target_path}
        except Exception as exc:
            return {"success": False, "error": f"Failed to stage HTTP artifact on test host: {exc}"}
        finally:
            if response is not None:
                response.close()

    def _verify_post_flash(self, devices: list[str], verify: dict[str, Any]) -> dict[str, Any]:
        """Verify ro.product/fingerprint after flash; retry while the device reboots."""
        expected_product = str(verify.get("product") or "").strip()
        fingerprint_contains = str(verify.get("fingerprint_contains") or "").strip()
        attempts = int(verify.get("retries") or 30)
        delay = int(verify.get("retry_delay") or 10)
        last_error = ""
        for _ in range(max(1, attempts)):
            for serial in devices:
                try:
                    info = self.device_manager.get_device_info(serial) or {}
                except Exception as exc:
                    last_error = f"get_device_info failed for {serial}: {exc}"
                    info = {}
                product_identity = " ".join(
                    str(info.get(key) or "") for key in ("product", "device", "board", "model")
                )
                fingerprint = str(info.get("fingerprint") or "")
                if expected_product and expected_product.lower() not in product_identity.lower():
                    last_error = f"product mismatch on {serial}: expected '{expected_product}', got '{product_identity.strip()}'"
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
            "worker_id": plan.get("worker_id") or "worker-local",
            "test_type": plan.get("test_type", ""),
            "test_module": plan.get("test_module") or (plan.get("modules") or [""])[0],
            "test_case": plan.get("test_case", ""),
            "retry_dir": plan.get("retry_dir", ""),
            "test_suite": plan.get("test_suite", ""),
            "local_server": plan.get("local_server", ""),
            "devices": _run_devices(run),
        }
        response = self.session.post(self._url("/api/test/start"), json=payload, timeout=30)
        result = self._json_response(response)
        if result.get("success"):
            result["running"] = True
        return result

    def poll_test(self, run: dict[str, Any]) -> dict[str, Any]:
        cluster_job_id = str(_run_result(run).get("cluster_job_id") or "")
        if cluster_job_id:
            response = self.session.get(self._url(f"/api/cluster/jobs/{cluster_job_id}"), timeout=30)
            result = self._json_response(response)
            if not result.get("success"):
                return result
            job = result.get("response", {}).get("job") or result.get("job") or {}
            status = str(job.get("status") or "")
            if status in {"created", "queued", "leasing", "assigned", "dispatching", "running",
                          "stopping", "collecting", "worker_lost"}:
                return {"success": True, "running": True, "cluster_job_id": cluster_job_id}
            if status != "completed":
                return {"success": False, "running": False,
                        "error": job.get("error") or f"cluster test {status}"}
            reports = self.session.get(self._url("/api/reports/list"),
                params={"worker_id": job.get("assigned_worker_id", "")}, timeout=30)
            report_data = self._json_response(reports).get("response", {})
            matched = next((item for item in report_data.get("reports", [])
                            if item.get("cluster_job_id") == cluster_job_id), None)
            return {"success": True, "running": False, "cluster_job_id": cluster_job_id,
                    "report_timestamp": (matched or {}).get("timestamp", "")}
        response = self.session.get(
            self._url("/api/test/status"),
            params={"logs": "true"},
            timeout=30,
        )
        result = self._json_response(response)
        if not result.get("success"):
            return result
        status = result.get("response", {})
        running = bool(status.get("running"))
        if running:
            return {"success": True, "running": True, "log_count": status.get("log_count", 0)}
        outcome = str(status.get("test_outcome") or "")
        report_timestamp = str(status.get("report_timestamp") or "")
        if outcome == "failed":
            return {"success": False, "running": False, "error": "GMS test process failed"}
        logs = status.get("logs") or []
        failed = "" if outcome == "completed" else next(
            (
                str(entry.get("msg") or "test failed")
                for entry in reversed(logs)
                if isinstance(entry, dict) and entry.get("type") == "error"
            ),
            "",
        )
        if failed:
            return {"success": False, "running": False, "error": failed}
        if not report_timestamp:
            return {"success": False, "running": False, "error": "Test completed without a persisted report"}
        return {
            "success": True,
            "running": False,
            "log_count": status.get("log_count", len(logs)),
            "report_timestamp": report_timestamp,
        }

    def cancel(self, run: dict[str, Any]) -> dict[str, Any]:
        status = str(run.get("status") or "")
        if status == "flashing":
            return {
                "success": False,
                "error": "Firmware flashing cannot be interrupted safely; wait for the flash command to finish",
            }
        if status in {"testing", "test_running"}:
            cluster_job_id = str(_run_result(run).get("cluster_job_id") or "")
            endpoint = f"/api/cluster/jobs/{cluster_job_id}/cancel" if cluster_job_id else "/api/test/stop"
            response = self.session.post(self._url(endpoint), timeout=30)
            return self._json_response(response)
        if status in {"jenkins_queued", "jenkins_building"}:
            build_job_id = str(run.get("jenkins_build_number") or "")
            if build_job_id:
                try:
                    from features.build import get_build_service

                    get_build_service().cancel_job(build_job_id)
                except Exception as exc:
                    return {"success": False, "error": f"failed to cancel build: {exc}"}
        return {"success": True, "cancelled": True}

    def collect_report(self, run: dict[str, Any]) -> dict[str, Any]:
        if run.get("report_timestamp"):
            return {"success": True, "report_timestamp": run["report_timestamp"]}
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
