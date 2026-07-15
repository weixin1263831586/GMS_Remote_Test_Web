"""Execution adapters for automation stages."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from features.automation.executor_contract import (
    AutomationExecutor as AutomationExecutor,
)
from features.automation.executor_contract import (
    StubAutomationExecutor as StubAutomationExecutor,
)
from features.automation.jenkins_client import JenkinsClient


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


def _artifact_stage_target(run_id: str, filename: str) -> tuple[Path, Path]:
    """Return an atomic Controller-local staging target for a firmware artifact."""
    from foundation.config import settings

    safe_run_id = re.sub(r"[^A-Za-z0-9._-]", "_", run_id or "ats")[:128]
    safe_filename = re.sub(
        r"[^A-Za-z0-9._+-]", "_", Path(filename or "firmware.img").name
    )[:255]
    directory = settings.data_root / "automation" / "artifact-stage" / safe_run_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / safe_filename
    temporary = directory / f".{safe_filename}.{uuid.uuid4().hex}.part"
    return target, temporary


def _artifact_limit_bytes() -> int:
    return max(
        1,
        int(os.getenv("GMS_AUTOMATION_ARTIFACT_MAX_BYTES", str(20 * 1024**3))),
    )


def _validated_controller_artifact(value: str) -> Path:
    """Resolve a firmware file only from configured Controller-side roots."""
    from foundation.config import config_manager, settings

    path = Path(value).expanduser().resolve()
    config = config_manager.load_config()
    configured = (
        (config.get("firmware_shares") or {}).get("allowed_prefixes")
        or config.get("firmware_share_allowed_prefixes")
        or ["/home/", "/data/", "/mnt/"]
    )
    roots = [settings.data_root.resolve()]
    roots.extend(Path(str(root)).expanduser().resolve() for root in configured)
    if not path.is_file() or not any(
        path == root or path.is_relative_to(root) for root in roots
    ):
        raise ValueError(
            "Firmware artifact is missing or outside the configured Controller roots"
        )
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("Firmware artifact is empty")
    if size > _artifact_limit_bytes():
        raise ValueError("Firmware artifact exceeds the ATS size limit")
    return path


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
            if queue.get("cancelled"):
                return {"success": False, "error": "Jenkins queue item was cancelled"}
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
        try:
            from features.cluster import get_cluster_service

            cluster = get_cluster_service()
            plan = _run_test_plan(run)
            worker_id = str(
                plan.get("worker_id") or run.get("worker_id")
                or cluster.config.local_worker_id
            )
            if worker_id == "worker-local":
                worker_id = cluster.config.local_worker_id
            selector = plan.get("device_selector") if isinstance(
                plan.get("device_selector"), dict
            ) else {}
            minimum = max(1, int(selector.get("min_count") or 1))

            existing = cluster.repository.get_reservation_by_source(run["id"])
            if existing:
                cluster.repository.renew_reservation(existing["id"])
                return {
                    "success": True,
                    "worker_id": existing["worker_id"],
                    "reservation_id": existing["id"],
                    "devices": [
                        {"serial": item["id"]} for item in existing["devices"]
                    ],
                    "recovered": True,
                }

            devices = _run_devices(run)
            if worker_id == "auto":
                suite_path = str(plan.get("test_suite") or "")
                suite = next(
                    (item for item in cluster.repository.list_suites()
                     if item.get("tools_path") == suite_path and item.get("available")),
                    None,
                )
                worker_id, auto_devices = cluster.select_worker(
                    (suite or {}).get("suite_key", ""), minimum, require_agent=True
                )
                if not devices:
                    devices = auto_devices
            if not cluster.has_command_agent(worker_id):
                return {
                    "success": False,
                    "error": f"Worker {worker_id} has no durable Agent for ATS flash/test",
                }

            if devices:
                normalized = [
                    value if value.startswith(f"{worker_id}:") else f"{worker_id}:{value}"
                    for value in devices
                ]
            else:
                prefix = str(selector.get("serial_prefix") or "")
                board = str(selector.get("board") or "").lower()
                candidates = []
                for item in cluster.repository.list_devices(worker_id):
                    properties = item.get("properties") or {}
                    product_board = str(
                        properties.get("board") or properties.get("product") or ""
                    ).lower()
                    if item.get("state") != "available":
                        continue
                    if prefix and not str(item.get("serial") or "").startswith(prefix):
                        continue
                    if board and board not in product_board:
                        continue
                    candidates.append(item["id"])
                if len(candidates) < minimum:
                    return {
                        "success": False,
                        "retry": True,
                        "error": f"not enough idle devices on {worker_id}: have {len(candidates)}, need {minimum}",
                    }
                normalized = candidates[:minimum]

            reservation = cluster.repository.reserve_devices(
                worker_id,
                normalized,
                owner_id=str(run.get("created_by") or run.get("owner") or "automation"),
                source_id=run["id"],
            )
            return {
                "success": True,
                "worker_id": worker_id,
                "reservation_id": reservation["id"],
                "devices": [
                    {"serial": item["id"]} for item in reservation["devices"]
                ],
            }
        except ValueError as exc:
            return {"success": False, "retry": True, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @staticmethod
    def release_resources(run: dict[str, Any]) -> dict[str, Any]:
        reservation_id = str(run.get("device_reservation_id") or "")
        if not reservation_id:
            return {"success": True, "released": False}
        from features.cluster import get_cluster_service

        released = get_cluster_service().repository.release_reservation(
            reservation_id, "cancelled"
        )
        return {"success": True, "released": released}

    def flash(self, run: dict[str, Any]) -> dict[str, Any]:
        plan = _run_test_plan(run)
        flash_plan = plan.get("flash") if isinstance(plan.get("flash"), dict) else {}
        if flash_plan.get("mode") == "skip":
            return {"success": True, "skipped": True}
        devices = _run_devices(run)
        if len(devices) != 1:
            return {"success": False, "error": "ATS firmware flash requires exactly one reserved device"}
        reservation_id = str(run.get("device_reservation_id") or "")
        if not reservation_id:
            return {"success": False, "error": "ATS device reservation is missing"}
        try:
            from features.cluster import get_cluster_service

            cluster = get_cluster_service()
            if not cluster.repository.renew_reservation(reservation_id):
                return {"success": False, "error": "ATS device reservation expired before flash"}
        except Exception as exc:
            return {"success": False, "error": f"failed to renew device reservation: {exc}"}

        worker_id = str(
            run.get("worker_id") or plan.get("worker_id")
            or cluster.config.local_worker_id
        )
        if worker_id == "worker-local":
            worker_id = cluster.config.local_worker_id
        command_id = str(run.get("flash_command_id") or "")
        recovered_stage_id = str(run.get("flash_stage_id") or "")
        finder = getattr(cluster.repository, "find_correlated_command", None)
        if not command_id and callable(finder):
            existing = finder(
                worker_id, "flash_firmware", "automation_run_id", str(run.get("id") or "")
            )
            if existing:
                command_id = str(existing.get("id") or "")
                recovered_stage_id = str(
                    (existing.get("payload") or {}).get("stage_id") or ""
                )
        if command_id:
            try:
                polled = self._json_response(self.session.get(
                    self._url(f"/api/cluster/commands/{command_id}"), timeout=30
                ))
            except Exception as exc:
                return {"success": True, "running": True, "command_id": command_id,
                        "stage_id": recovered_stage_id, "poll_error": str(exc)}
            command = polled.get("response", {}).get("command") or polled.get("command") or {}
            status = str(command.get("status") or "")
            if status in {"queued", "delivered", "accepted", "running"}:
                return {"success": True, "running": True, "command_id": command_id,
                        "stage_id": recovered_stage_id, "command_status": status}
            if status != "completed":
                return {"success": False, "command_id": command_id,
                        "error": command.get("error") or f"cluster flash {status or 'unknown'}"}
            verify = flash_plan.get("verify") if isinstance(flash_plan.get("verify"), dict) else {}
            checked = self._verify_cluster_post_flash(worker_id, devices, verify)
            if not checked.get("success") and checked.get("retry"):
                timeout = max(60, int(verify.get("retries") or 30) * int(verify.get("retry_delay") or 10))
                acknowledged = str(command.get("acknowledged_at") or command.get("updated_at") or "")
                try:
                    from datetime import datetime, timezone

                    started = datetime.fromisoformat(acknowledged.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - started).total_seconds()
                except (TypeError, ValueError):
                    age = 0
                if age < timeout:
                    return {"success": True, "running": True, "command_id": command_id,
                            "stage_id": recovered_stage_id, **checked}
            if not checked.get("success"):
                return {"success": False, "command_id": command_id,
                        "error": checked.get("error") or "post-flash verification failed",
                        "verification": checked}
            return {"success": True, "running": False, "command_id": command_id,
                    "stage_id": recovered_stage_id,
                    "flash_result": command.get("result") or {}, "verification": checked}

        build_plan = _run_build_plan(run)
        is_ssh_build = bool(build_plan.get("provider") == "ssh" or build_plan.get("server_id"))
        if is_ssh_build:
            firmware_path = run.get("artifact_path") or run.get("artifact_url")
        else:
            firmware_path = run.get("artifact_url") or run.get("artifact_path")
        if not firmware_path:
            return {"success": False, "error": "No firmware artifact path/url"}
        cleanup_path = ""
        if is_ssh_build:
            staged = self._stage_ssh_build_artifact(run, str(firmware_path))
            if not staged.get("success"):
                return staged
            firmware_path = staged["firmware_path"]
            cleanup_path = staged.get("cleanup_path", "")
        elif str(firmware_path).startswith(("http://", "https://")):
            staged = self._stage_http_artifact(run, str(firmware_path))
            if not staged.get("success"):
                return staged
            firmware_path = staged["firmware_path"]
            cleanup_path = staged.get("cleanup_path", "")
        try:
            path = _validated_controller_artifact(str(firmware_path))
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        try:
            from requests_toolbelt.multipart.encoder import MultipartEncoder

            with path.open("rb") as source:
                encoder = MultipartEncoder(fields={
                    "worker_id": worker_id,
                    "devices": ",".join(devices),
                    "reservation_id": reservation_id,
                    "automation_run_id": run["id"],
                    "firmware_file": (path.name, source, "application/octet-stream"),
                })
                response = self.session.post(
                    self._url("/api/cluster/firmware/stage"),
                    data=encoder,
                    headers={"Content-Type": encoder.content_type},
                    timeout=3600,
                )
            result = self._json_response(response)
        except Exception as exc:
            return {"success": False, "error": f"failed to stage firmware for Worker: {exc}"}
        finally:
            if cleanup_path:
                try:
                    Path(cleanup_path).unlink(missing_ok=True)
                except OSError:
                    pass
        command_id = str(
            result.get("response", {}).get("command_id") or result.get("command_id") or ""
        )
        stage_id = str(result.get("response", {}).get("stage_id") or result.get("stage_id") or "")
        if not result.get("success") or not command_id:
            return result if not result.get("success") else {
                "success": False, "error": "firmware stage did not return a command id"
            }
        return {"success": True, "running": True, "command_id": command_id,
                "stage_id": stage_id, "deduplicated": bool(
                    result.get("response", {}).get("deduplicated")
                )}

    def _stage_ssh_build_artifact(self, run: dict[str, Any], artifact_path: str) -> dict[str, Any]:
        """Stream an approved SSH build artifact onto the Controller."""
        source_ssh = None
        temporary: Path | None = None
        try:
            import paramiko

            from features.build import get_build_service

            build_service = get_build_service()
            job_id = str(run.get("jenkins_build_number") or "")
            if not job_id:
                return {"success": False, "error": "SSH build job id is missing"}
            job = build_service.get_job(job_id)
            if job.get("status") != "completed":
                return {"success": False, "error": "SSH build job is not completed"}
            if job.get("automation_run_id") and job["automation_run_id"] != run.get("id"):
                return {"success": False, "error": "SSH build job belongs to another automation run"}
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
            source_sftp = source_ssh.open_sftp()
            try:
                stat = source_sftp.stat(artifact_path)
                size = int(stat.st_size or 0)
                if size <= 0:
                    return {"success": False, "error": "SSH build artifact is empty"}
                if size > _artifact_limit_bytes():
                    return {"success": False, "error": "SSH build artifact exceeds the ATS size limit"}
                target, temporary = _artifact_stage_target(
                    str(run.get("id") or "ats"), Path(artifact_path).name
                )
                total = 0
                with source_sftp.open(artifact_path, "rb") as source, temporary.open("wb") as output:
                    while chunk := source.read(4 * 1024 * 1024):
                        total += len(chunk)
                        if total > _artifact_limit_bytes():
                            raise ValueError("SSH build artifact exceeds the ATS size limit")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if total != size:
                    raise ValueError(
                        f"SSH build artifact size changed during transfer: expected {size}, got {total}"
                    )
                temporary.replace(target)
                temporary = None
            finally:
                source_sftp.close()
            return {
                "success": True,
                "firmware_path": str(target),
                "cleanup_path": str(target),
                "size_bytes": size,
            }
        except Exception as exc:
            return {"success": False, "error": f"Failed to stage build artifact on Controller: {exc}"}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if source_ssh is not None:
                source_ssh.close()

    def _stage_http_artifact(self, run: dict[str, Any], artifact_url: str) -> dict[str, Any]:
        """Stream a Jenkins artifact to the Controller without buffering it."""
        response = None
        temporary: Path | None = None
        try:
            jenkins = _jenkins_config_from_run(run, self.jenkins_config)
            configured_url = str(jenkins.get("base_url") or "").strip()
            requested = urlparse(artifact_url)
            configured = urlparse(configured_url)
            if (
                requested.scheme not in {"http", "https"}
                or not configured_url
                or requested.scheme != configured.scheme
                or requested.netloc != configured.netloc
                or requested.username
                or requested.password
            ):
                return {"success": False, "error": "Artifact URL is outside the configured Jenkins origin"}
            configured_path = configured.path.rstrip("/")
            if configured_path and not (
                requested.path == configured_path
                or requested.path.startswith(configured_path + "/")
            ):
                return {"success": False, "error": "Artifact URL is outside the configured Jenkins path"}
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
            final_url = urlparse(str(getattr(response, "url", artifact_url) or artifact_url))
            if final_url.scheme != configured.scheme or final_url.netloc != configured.netloc:
                return {"success": False, "error": "Jenkins redirected the artifact outside its configured origin"}
            declared_size = int(response.headers.get("Content-Length") or 0)
            limit = _artifact_limit_bytes()
            if declared_size > limit:
                return {"success": False, "error": "HTTP artifact exceeds the ATS size limit"}
            filename = Path(requested.path.rstrip("/")).name or "update.img"
            target, temporary = _artifact_stage_target(str(run.get("id") or "ats"), filename)
            total = 0
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("HTTP artifact exceeds the ATS size limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not total:
                raise ValueError("HTTP artifact is empty")
            if declared_size and total != declared_size:
                raise ValueError(
                    f"HTTP artifact is incomplete: expected {declared_size}, got {total}"
                )
            temporary.replace(target)
            temporary = None
            return {
                "success": True,
                "firmware_path": str(target),
                "cleanup_path": str(target),
                "size_bytes": total,
            }
        except Exception as exc:
            return {"success": False, "error": f"Failed to stage HTTP artifact on Controller: {exc}"}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if response is not None:
                response.close()

    def _verify_cluster_post_flash(
        self, worker_id: str, devices: list[str], verify: dict[str, Any]
    ) -> dict[str, Any]:
        """Probe the exact reserved Worker device and validate its flashed build."""
        try:
            response = self.session.post(
                self._url("/api/cluster/devices/actions"),
                json={"worker_id": worker_id, "devices": devices, "action": "props"},
                timeout=45,
            )
            result = self._json_response(response)
        except Exception as exc:
            return {
                "success": False,
                "retry": True,
                "error": f"post-flash device is not reachable: {exc}",
            }
        if not result.get("success"):
            return {
                "success": False,
                "retry": True,
                "error": result.get("error") or "post-flash property probe failed",
            }
        payload = result.get("response") or result
        properties = {
            str(item.get("name") or ""): str(item.get("value") or "")
            for item in payload.get("rows") or []
            if isinstance(item, dict) and item.get("name")
        }
        boot_completed = properties.get("sys.boot_completed", "")
        fingerprint = properties.get("ro.build.fingerprint", "")
        if boot_completed != "1" or not fingerprint:
            return {
                "success": False,
                "retry": True,
                "error": "device has not completed Android boot after flashing",
                "boot_completed": boot_completed,
            }
        identities = {
            key: properties.get(key, "")
            for key in (
                "ro.product.device",
                "ro.product.name",
                "ro.product.model",
                "ro.product.board",
            )
        }
        expected_product = str(verify.get("product") or "").strip()
        identity_text = " ".join(identities.values())
        if expected_product and expected_product.lower() not in identity_text.lower():
            return {
                "success": False,
                "retry": False,
                "error": f"post-flash product mismatch: expected '{expected_product}', got '{identity_text.strip()}'",
                "identities": identities,
                "fingerprint": fingerprint,
            }
        exact_fingerprint = str(verify.get("fingerprint") or "").strip()
        contains = str(verify.get("fingerprint_contains") or "").strip()
        if exact_fingerprint and fingerprint != exact_fingerprint:
            return {
                "success": False,
                "retry": False,
                "error": "post-flash fingerprint does not match the expected build",
                "fingerprint": fingerprint,
            }
        if contains and contains.lower() not in fingerprint.lower():
            return {
                "success": False,
                "retry": False,
                "error": f"post-flash fingerprint is missing '{contains}'",
                "fingerprint": fingerprint,
            }
        return {
            "success": True,
            "verified": True,
            "worker_id": worker_id,
            "devices": devices,
            "boot_completed": boot_completed,
            "fingerprint": fingerprint,
            "identities": identities,
        }

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
        worker_id = str(run.get("worker_id") or plan.get("worker_id") or "")
        if not worker_id or worker_id == "worker-local":
            from features.cluster import get_cluster_service

            try:
                worker_id = get_cluster_service().config.local_worker_id
            except (AttributeError, RuntimeError):
                worker_id = "worker-local"
        redmine = plan.get("redmine") if isinstance(plan.get("redmine"), dict) else {}
        payload = {
            "worker_id": worker_id,
            "test_type": plan.get("test_type", ""),
            "test_module": plan.get("test_module") or (plan.get("modules") or [""])[0],
            "test_case": plan.get("test_case", ""),
            "retry_dir": plan.get("retry_dir", ""),
            "test_suite": plan.get("test_suite", ""),
            "local_server": plan.get("local_server", ""),
            "devices": _run_devices(run),
            "automation_run_id": run.get("id", ""),
            "device_reservation_id": run.get("device_reservation_id", ""),
            "build_id": run.get("jenkins_build_number", ""),
            "build_artifact_id": run.get("build_artifact_id", ""),
            "gerrit_change_id": run.get("gerrit_change_id", ""),
            "gerrit_patchset": run.get("gerrit_patchset", ""),
            "redmine_issue_id": str(
                plan.get("redmine_issue_id") or redmine.get("issue_id", "")
            ),
        }
        response = self.session.post(self._url("/api/test/start"), json=payload, timeout=30)
        result = self._json_response(response)
        if result.get("success"):
            if not result.get("cluster_job_id"):
                return {
                    "success": False,
                    "error": "ATS requires a durable Cluster Job but the test API started a local transient test",
                }
            result["running"] = True
        return result

    def poll_test(self, run: dict[str, Any]) -> dict[str, Any]:
        cluster_job_id = str(
            run.get("cluster_job_id") or _run_result(run).get("cluster_job_id") or ""
        )
        if cluster_job_id:
            response = self.session.get(self._url(f"/api/cluster/jobs/{cluster_job_id}"), timeout=30)
            result = self._json_response(response)
            if not result.get("success"):
                return result
            job = result.get("response", {}).get("job") or result.get("job") or {}
            status = str(job.get("status") or "")
            if status in {"created", "queued", "leasing", "assigned", "dispatching", "running",
                          "stopping", "collecting", "worker_lost"}:
                return {
                    "success": True,
                    "running": True,
                    "cluster_job_id": cluster_job_id,
                    "attempt_id": job.get("current_attempt_id", ""),
                    "job_status": status,
                }
            if status != "completed":
                return {"success": False, "running": False,
                        "error": job.get("error") or f"cluster test {status}"}
            reports = self.session.get(
                self._url("/api/reports/list"),
                params={
                    "worker_id": job.get("assigned_worker_id", ""),
                    "cluster_job_id": cluster_job_id,
                    "attempt_id": job.get("current_attempt_id", ""),
                    "automation_run_id": run.get("id", ""),
                },
                timeout=30,
            )
            report_data = self._json_response(reports).get("response", {})
            matched = next(iter(report_data.get("reports", [])), None)
            if not matched:
                return {
                    "success": True,
                    "running": True,
                    "cluster_job_id": cluster_job_id,
                    "attempt_id": job.get("current_attempt_id", ""),
                    "report_pending": True,
                }
            return {
                "success": True,
                "running": False,
                "cluster_job_id": cluster_job_id,
                "attempt_id": job.get("current_attempt_id", ""),
                "report_timestamp": matched.get("timestamp", ""),
                "report_id": matched.get("report_id") or matched.get("timestamp", ""),
            }
        return {
            "success": False,
            "running": False,
            "error": "ATS test correlation is missing its durable Cluster Job id",
        }

    def cancel(self, run: dict[str, Any]) -> dict[str, Any]:
        status = str(run.get("status") or "")
        if status == "flashing":
            return {
                "success": False,
                "error": "Firmware flashing cannot be interrupted safely; wait for the flash command to finish",
            }
        if status in {"testing", "test_running"}:
            cluster_job_id = str(
                run.get("cluster_job_id") or _run_result(run).get("cluster_job_id") or ""
            )
            endpoint = f"/api/cluster/jobs/{cluster_job_id}/cancel" if cluster_job_id else "/api/test/stop"
            response = self.session.post(self._url(endpoint), timeout=30)
            result = self._json_response(response)
            if result.get("success"):
                self.release_resources(run)
            return result
        if status in {"jenkins_queued", "jenkins_building"}:
            try:
                build_plan = _run_build_plan(run)
                if build_plan.get("provider") == "ssh" or build_plan.get("server_id"):
                    from features.build import get_build_service

                    build_job_id = str(run.get("jenkins_build_number") or "")
                    if not build_job_id and str(run.get("jenkins_queue_url") or "").startswith("build://"):
                        build_job_id = str(run["jenkins_queue_url"])[len("build://"):]
                    if not build_job_id:
                        return {"success": False, "error": "Build job id missing"}
                    get_build_service().cancel_job(build_job_id)
                else:
                    config = _jenkins_config_from_run(run, self.jenkins_config)
                    if not config.get("base_url"):
                        return {"success": False, "error": "Jenkins config missing"}
                    client = JenkinsClient(config)
                    build_number = str(run.get("jenkins_build_number") or "")
                    queue_url = str(run.get("jenkins_queue_url") or "")
                    if build_number:
                        result = client.cancel_build(run.get("jenkins_job", ""), build_number)
                    elif queue_url:
                        result = client.cancel_queue_item(queue_url)
                    else:
                        return {"success": False, "error": "Jenkins build correlation missing"}
                    if not result.get("success"):
                        return result
            except Exception as exc:
                return {"success": False, "error": f"failed to cancel build: {exc}"}
        self.release_resources(run)
        return {"success": True, "cancelled": True}

    def collect_report(self, run: dict[str, Any]) -> dict[str, Any]:
        cluster_job_id = str(run.get("cluster_job_id") or "")
        report_timestamp = str(run.get("report_timestamp") or "")
        if not cluster_job_id and not report_timestamp:
            return {"success": False, "error": "Automation run has no exact report correlation"}
        params = {
            "cluster_job_id": cluster_job_id,
            "attempt_id": run.get("attempt_id", ""),
            "automation_run_id": run.get("id", ""),
            "report_timestamp": report_timestamp,
        }
        response = self.session.get(
            self._url("/api/reports/list"), params=params, timeout=30
        )
        data = self._json_response(response)
        if not data.get("success"):
            return data
        reports = data.get("response", {}).get("reports") or data.get("response", {}).get("data", {}).get("reports") or []
        if not reports:
            return {
                "success": False,
                "retry": True,
                "error": "The exact Cluster Job report has not been indexed yet",
            }
        report = reports[0]
        if cluster_job_id and report.get("cluster_job_id") != cluster_job_id:
            return {"success": False, "error": "Report belongs to another Cluster Job"}
        if report.get("automation_run_id") and report["automation_run_id"] != run.get("id"):
            return {"success": False, "error": "Report belongs to another automation run"}
        timestamp = report.get("timestamp") or report.get("report_timestamp") or ""
        return {
            "success": True,
            "report_timestamp": timestamp,
            "report_id": report.get("report_id") or timestamp,
            "report": report,
        }

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

            notification_status = (
                "completed" if run.get("status") == "reporting" else run.get("status", "")
            )
            notifications = notify_run_completion(
                {**run, "status": notification_status}
            )
        except Exception:
            pass
        if notifications.get("ok") is False:
            return {
                "success": False,
                "error": "required completion notifications failed: "
                + ", ".join(notifications.get("failed_required") or []),
                "notifications": notifications,
            }
        return {
            "success": True,
            "report_timestamp": run.get("report_timestamp", ""),
            "result": json.loads(run.get("result_json") or "{}"),
            "notifications": notifications,
        }
