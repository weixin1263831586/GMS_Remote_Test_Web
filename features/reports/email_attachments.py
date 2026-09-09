"""Email attachment bundles built from report resources (R04).

Lives in ``features.reports`` so the report-attachment capability is owned
by the reports feature; the email feature receives it via a provider
injected by the composition root (bootstrap/dependencies.py). This keeps
the feature dependency graph acyclic — email must not import reports
(2026-09-08 R13 architecture gate; the previous direct import closed the
email → reports → redmine → email cycle).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from features.auth import require_authenticated_user


def resolve_report_attachments(request, report_ids: list) -> tuple[list[Path], list[str]]:
    """Resolve report IDs into ZIP bundles with owner checks.

    客户端不能直接指定共享目录路径：目录边界（data/reports 等）不能
    替代报告/附件的 owner 边界，任何登录用户都能借此读取其他用户的报告
    文件。附件只接受报告资源 ID，逐个调用 reports 的 owner 校验后再把
    报告 results/logs 打包成 ZIP；校验在构造 MIME 之前完成。
    返回 ``(attachments, denied_report_ids)``。
    """
    from features.reports.access import can_access_report
    from features.reports.downloads import create_local_report_bundle
    from features.reports.repository import test_report_db

    principal = require_authenticated_user(request)
    attachments: list[Path] = []
    denied: list[str] = []
    bundle_dir = Path(tempfile.mkdtemp(prefix="gms-email-attach-"))
    for raw_id in report_ids:
        report_id = str(raw_id or "").strip()
        if not report_id:
            continue
        report = test_report_db.get_report(
            report_id,
            owner_id=None if principal.role == "admin" else principal.id,
            include_all=principal.role == "admin",
        )
        if not report or not can_access_report(request, report):
            denied.append(report_id)
            continue
        bundle = create_local_report_bundle(report)
        if bundle is None:
            continue
        target = bundle_dir / f"{report_id}.zip"
        shutil.move(str(bundle.path), target)
        attachments.append(target)
    for leftover in bundle_dir.glob("gms-report-download-*"):
        leftover.unlink(missing_ok=True)
    return attachments, denied
