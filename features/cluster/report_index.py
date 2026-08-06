"""Index durable Cluster job artifacts in the unified reports store."""

from __future__ import annotations

from pathlib import Path

from .api import service


def index_cluster_report(job: dict, result_dir: Path, artifact: dict) -> None:
    """Expose completed Worker results through the Reports page."""
    from features.reports import (
        XMLReportParser,
        report_client_display_id,
        report_name_from_result_dir,
        test_report_db,
        tradefed_result_folder_name,
    )

    request_data = job.get("request") or {}
    suite_key = job.get("suite_key") or "XTS"
    test_type = suite_key.split(":", 1)[0].upper()
    timestamp = f"cluster-{job['id']}"
    attempt_id = artifact.get("attempt_id") or job.get("current_attempt_id", "")
    owner_id = str(job.get("owner_id") or "")
    existing = test_report_db.get_report_by_timestamp(
        timestamp,
        owner_id=owner_id,
    ) or {}
    artifact_ids = list(existing.get("artifact_ids") or [])
    if artifact.get("id") and artifact["id"] not in artifact_ids:
        artifact_ids.append(artifact["id"])
    report_name = report_name_from_result_dir(str(result_dir))
    source_timestamp = tradefed_result_folder_name(
        report_name,
        existing.get("source_timestamp"),
    )
    report_info = {
        **existing,
        "timestamp": timestamp,
        "report_id": f"cluster:{job['id']}:{attempt_id}",
        "test_type": test_type,
        "test_module": request_data.get("test_module", ""),
        "test_case": request_data.get("test_case", ""),
        "client_id": job.get("owner_id", "cluster"),
        "owner_id": job.get("owner_id", "cluster"),
        "display_client_id": report_client_display_id({
            "owner_id": owner_id,
            "worker_id": job.get("assigned_worker_id", ""),
        }),
        "report_name": report_name or existing.get("report_name", ""),
        "devices": [item["device_id"] for item in job.get("leases", [])],
        "result_dir": str(result_dir),
        "suite_path": job.get("suite_path", ""),
        "status": "collecting",
        "worker_id": job.get("assigned_worker_id", ""),
        "cluster_job_id": job["id"],
        "attempt_id": attempt_id,
        "artifact_id": artifact["id"],
        "artifact_ids": artifact_ids,
        "automation_run_id": request_data.get("automation_run_id", ""),
        "build_id": request_data.get("build_id", ""),
        "build_artifact_id": request_data.get("build_artifact_id", ""),
        "gerrit_change_id": request_data.get("gerrit_change_id", ""),
        "gerrit_patchset": request_data.get("gerrit_patchset", ""),
        "redmine_issue_id": request_data.get("redmine_issue_id", ""),
        "source_type": job.get("source_type", "cluster"),
    }
    if source_timestamp:
        report_info["source_timestamp"] = source_timestamp
    if artifact.get("artifact_type") == "report-archive":
        report_info["archive_artifact_id"] = artifact["id"]
    if artifact.get("filename") == "test_result.xml":
        report_info["report_artifact_id"] = artifact["id"]
        xml_path = result_dir / artifact["filename"]
        parsed = XMLReportParser().parse_file(str(xml_path)) if xml_path.is_file() else None
        if parsed:
            report_info.update({
                "pass": parsed.pass_count,
                "fail": parsed.fail_count,
                "total": parsed.total,
                "suite_version": parsed.suite_version,
                "android_version": parsed.android_version,
                "start_time": parsed.start_time if parsed.start_time != "未知时间" else "",
            })
            # 默认 GTS 类型不能覆盖任务中已知的测试类型。
            if parsed.test_type and parsed.test_type.upper() != "GTS":
                report_info["test_type"] = parsed.test_type.upper()
    test_report_db.add_report(report_info)


def update_cluster_report_status(job_id: str, status: str, error: str = "") -> None:
    """Finalize exactly the report indexed for this durable Cluster job."""
    from features.reports import test_report_db

    timestamp = f"cluster-{job_id}"
    job = service().repository.get_job(job_id)
    if not job:
        return
    owner_id = str(job.get("owner_id") or "")
    if test_report_db.get_report_by_timestamp(timestamp, owner_id=owner_id):
        test_report_db.update_report_status(
            timestamp,
            status,
            owner_id=owner_id,
            error=error,
        )
