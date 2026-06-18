"""Dedicated agent capability for GMS/CTS/VTS report attachment analysis."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from core.archive_utils import ARCHIVE_EXTENSIONS, safe_extract_member_path
from core.report_analyzer import HostLogParser, TestReport, XMLReportParser


logger = logging.getLogger(__name__)


class ReportAnalysisAgent:
    """Analyze report attachments by recursively expanding containers and selecting useful artifacts."""

    _DEVICE_LOG_RE = re.compile(r"device_logcat_test.*\.txt$", re.IGNORECASE)
    _SOC_PATTERNS = (
        re.compile(r"\b(RK\d{4}[A-Za-z0-9]*)\b", re.IGNORECASE),
        re.compile(r"\bro\.board\.platform\s*[:=]\s*(rk\d{4}[A-Za-z0-9]*)\b", re.IGNORECASE),
        re.compile(r"\bro\.product\.board\s*[:=]\s*(RK\d{4}[A-Za-z0-9]*)\b", re.IGNORECASE),
    )
    _ANDROID_PATTERNS = (
        re.compile(r"\bbuild_version_release\s*[:=]\s*([A-Za-z0-9._-]+)", re.IGNORECASE),
        re.compile(r"\bro\.build\.version\.release\s*[:=]\s*([A-Za-z0-9._-]+)", re.IGNORECASE),
        re.compile(r"\bAndroid\s*([0-9]+(?:\.[0-9]+)?)\b", re.IGNORECASE),
    )
    _SUITE_PATTERNS = (
        re.compile(r"\bsuite_version\s*[:=]\s*([A-Za-z0-9._-]+)", re.IGNORECASE),
        re.compile(r"\bandroid-(?:cts|gts|vts)-([0-9]+(?:\.[0-9]+)?(?:_r\d+)?)\b", re.IGNORECASE),
    )
    _TEST_TYPE_PATTERNS = (
        re.compile(r"\b(android-)?(CTS|GTS|VTS|STS|ATS|XTS)\b", re.IGNORECASE),
    )

    def __init__(self, temp_dir: str | None = None, max_depth: int = 6, max_files: int = 2000):
        self.temp_dir = temp_dir or tempfile.gettempdir()
        self.max_depth = max_depth
        self.max_files = max_files
        self.xml_parser = XMLReportParser()
        self.host_log_parser = HostLogParser()

    def analyze_path(self, path: str) -> dict[str, Any] | None:
        """Analyze one file or directory path."""
        return self.analyze_paths([path])

    def analyze_paths(self, paths: Sequence[str]) -> dict[str, Any] | None:
        """Analyze one or more local files/directories as a single report bundle."""
        workspace_parent = self.temp_dir if os.path.isdir(self.temp_dir) else tempfile.gettempdir()
        workspace = tempfile.mkdtemp(prefix="report_agent_", dir=workspace_parent)
        try:
            files: list[str] = []
            seen_archives: set[str] = set()
            for path in paths:
                if path and os.path.exists(path):
                    self._expand_entry(path, workspace, files, seen_archives, depth=0)
            if not files:
                return None
            return self._analyze_expanded_files(files)
        finally:
            try:
                shutil.rmtree(workspace)
            except Exception:
                logger.debug("Failed to remove report agent workspace: %s", workspace, exc_info=True)

    def _expand_entry(
        self,
        path: str,
        workspace: str,
        files: list[str],
        seen_archives: set[str],
        depth: int,
    ) -> None:
        if len(files) >= self.max_files:
            return
        if depth > self.max_depth:
            logger.warning("Report archive nesting exceeds max_depth=%s at %s", self.max_depth, path)
            return

        if os.path.isdir(path):
            for root, dirs, filenames in os.walk(path):
                dirs[:] = [d for d in dirs if d not in {"__MACOSX"}]
                for filename in filenames:
                    self._expand_entry(os.path.join(root, filename), workspace, files, seen_archives, depth)
                    if len(files) >= self.max_files:
                        return
            return

        if self._is_archive(path):
            archive_key = os.path.abspath(path)
            if archive_key in seen_archives:
                return
            seen_archives.add(archive_key)
            extract_dir = tempfile.mkdtemp(prefix=f"layer_{depth}_", dir=workspace)
            if self._extract_archive(path, extract_dir):
                self._expand_entry(extract_dir, workspace, files, seen_archives, depth + 1)
            return

        files.append(path)

    def _is_archive(self, path: str) -> bool:
        lower = path.lower()
        if lower.endswith(".zip"):
            return zipfile.is_zipfile(path)
        if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
            return tarfile.is_tarfile(path)
        if lower.endswith((".rar", ".7z")):
            return True
        return bool(lower.endswith(ARCHIVE_EXTENSIONS))

    def _extract_archive(self, archive_path: str, target_dir: str) -> bool:
        lower = archive_path.lower()
        try:
            if lower.endswith(".zip"):
                self._extract_zip(archive_path, target_dir)
                return True
            if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
                self._extract_tar(archive_path, target_dir)
                return True
            if lower.endswith((".rar", ".7z")):
                self._extract_with_7z(archive_path, target_dir)
                return True
        except Exception as exc:
            logger.warning("Failed to extract report archive %s: %s", archive_path, exc)
        return False

    def _extract_zip(self, archive_path: str, target_dir: str) -> None:
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.infolist():
                target = safe_extract_member_path(target_dir, member.filename)
                if member.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    def _extract_tar(self, archive_path: str, target_dir: str) -> None:
        with tarfile.open(archive_path, "r:*") as tf:
            for member in tf.getmembers():
                target = safe_extract_member_path(target_dir, member.name)
                if member.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                if src:
                    with src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)

    def _extract_with_7z(self, archive_path: str, target_dir: str) -> None:
        command = "rar" if archive_path.lower().endswith(".rar") and shutil.which("rar") else "7z"
        if not shutil.which(command):
            raise RuntimeError(f"{command} command not found")
        args = [command, "x", "-y", archive_path, target_dir + os.sep] if command == "rar" else [
            command,
            "x",
            "-y",
            f"-o{target_dir}",
            archive_path,
        ]
        subprocess.run(args, check=True, capture_output=True, timeout=120)

    def _analyze_expanded_files(self, files: list[str]) -> dict[str, Any] | None:
        xml_report = self._parse_first_xml(files)
        host_logs = self._host_log_files(files)
        device_logs = self._device_log_files(files)
        host_reports = self._parse_host_logs(host_logs)

        report = xml_report or (host_reports[0] if host_reports else None)
        if not report:
            return None

        result = self._report_to_dict(report)
        if xml_report and xml_report.total == 0 and host_reports:
            result = self._report_to_dict(host_reports[0])

        self._enrich_details(result, files, host_reports)
        result["analysis_sources"] = {
            "test_result_xml": self._relative_sources(files, self._xml_files(files)),
            "failures_html": self._relative_sources(files, self._failure_html_files(files)),
            "test_result_html": self._relative_sources(files, self._result_html_files(files)),
            "host_logs": self._relative_sources(files, host_logs),
            "device_logs": self._relative_sources(files, device_logs),
        }
        failures_html = self._parse_failures_html_files(files)
        if failures_html["failures"]:
            result["failures_html"] = failures_html
        result["host_log_errors"] = self._extract_log_errors_from_files(host_logs, "host")
        result["device_log_errors"] = self._extract_log_errors_from_files(device_logs, "device")
        return result

    def _parse_first_xml(self, files: list[str]) -> TestReport | None:
        for path in self._xml_files(files):
            report = self.xml_parser.parse_file(path)
            if report:
                return report
        return None

    def _parse_host_logs(self, host_logs: list[str]) -> list[TestReport]:
        reports = []
        for path in host_logs:
            try:
                content = Path(path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            report = self.host_log_parser.parse_content(content, os.path.dirname(path))
            if report:
                reports.append(report)
        return reports

    def _xml_files(self, files: list[str]) -> list[str]:
        return sorted(
            [p for p in files if os.path.basename(p) == "test_result.xml"],
            key=lambda p: (0 if "/results/" in p.replace("\\", "/") else 1, len(p), p),
        )

    def _failure_html_files(self, files: list[str]) -> list[str]:
        return sorted([p for p in files if os.path.basename(p) == "test_result_failures_suite.html"])

    def _result_html_files(self, files: list[str]) -> list[str]:
        return sorted([p for p in files if os.path.basename(p) == "test_result.html"])

    def _host_log_files(self, files: list[str]) -> list[str]:
        return sorted([p for p in files if HostLogParser._is_host_log_filename(os.path.basename(p))])

    def _device_log_files(self, files: list[str]) -> list[str]:
        return sorted([p for p in files if self._DEVICE_LOG_RE.match(os.path.basename(p))])

    def _relative_sources(self, all_files: list[str], selected: Iterable[str]) -> list[str]:
        if not all_files:
            return []
        common = os.path.commonpath([os.path.abspath(p) for p in all_files])
        return [os.path.relpath(path, common).replace("\\", "/") for path in selected]

    def _parse_failures_html_files(self, files: list[str]) -> dict[str, Any]:
        failures: list[dict[str, str]] = []
        for path in self._failure_html_files(files):
            try:
                content = Path(path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            failures.extend(self._parse_failures_html(content))
        return {"failures": failures[:100]}

    def _parse_failures_html(self, html_content: str) -> list[dict[str, str]]:
        test_names = re.findall(r'<td class="testname">([^<]+)</td>', html_content)
        details_list = re.findall(r'<div class="details">([^<]*(?:<[^>]+>[^<]*</[^>]+>[^<]*)*)</div>', html_content, re.DOTALL)
        failures = []
        for index, test_name in enumerate(test_names[:100]):
            message = ""
            if index < len(details_list):
                message = re.sub(r"<[^>]+>", "", details_list[index])
                message = message.replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
                message = " ".join(message.split())
            failures.append({"test_name": test_name.strip(), "message": message})
        return failures

    def _extract_log_errors_from_files(self, log_files: list[str], log_type: str) -> dict[str, Any]:
        errors: list[str] = []
        for path in log_files:
            try:
                content = Path(path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            errors.extend(self._extract_log_errors(content, log_type))
        unique = []
        seen = set()
        for error in errors:
            key = re.sub(r"\s+", " ", error).strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(key[:800])
        return {"errors": unique[:50], "total_errors": len(unique)}

    def _extract_log_errors(self, log_content: str, log_type: str) -> list[str]:
        if log_type == "host":
            errors = re.findall(r"(?:FAILURE:|ASSUMPTION_FAILURE:|E/TestInvocation:|HarnessRuntimeException)[^\n]*(?:\n(?!\d{2}-\d{2} )[^\n]+){0,8}", log_content)
            if errors:
                return [err.strip() for err in errors]
            return [line.strip() for line in log_content.splitlines() if " fail:" in line.lower() or " error" in line.lower()][:20]

        lines = log_content.splitlines()
        blocks = []
        for index, line in enumerate(lines):
            if "FATAL EXCEPTION" in line or ("AndroidRuntime" in line and ("Exception" in line or "Error" in line)):
                blocks.append("\n".join(lines[index : min(index + 20, len(lines))]).strip())
        return blocks

    def _enrich_details(self, result: dict[str, Any], files: list[str], host_reports: list[TestReport]) -> None:
        details = result.setdefault("details", {})
        sampled_text = self._sample_text(files)
        details["android_version"] = details.get("android_version") or self._first_match(sampled_text, self._ANDROID_PATTERNS)
        details["suite_version"] = details.get("suite_version") or self._first_match(sampled_text, self._SUITE_PATTERNS)
        details["soc_platform"] = self._normalize_soc(self._first_match(sampled_text, self._SOC_PATTERNS))
        if not details.get("test_type"):
            details["test_type"] = self._detect_test_type(sampled_text, host_reports)

    def _sample_text(self, files: list[str]) -> str:
        interesting = self._xml_files(files) + self._host_log_files(files) + self._result_html_files(files)
        chunks = []
        for path in interesting[:20]:
            try:
                with open(path, "rb") as f:
                    chunks.append(f.read(256 * 1024).decode("utf-8", errors="ignore"))
            except Exception:
                continue
        return "\n".join(chunks)

    def _first_match(self, text: str, patterns: Sequence[re.Pattern[str]]) -> str:
        for pattern in patterns:
            match = pattern.search(text or "")
            if match:
                return match.group(match.lastindex or 1)
        return ""

    def _normalize_soc(self, value: str) -> str:
        return value.upper() if value else ""

    def _detect_test_type(self, text: str, host_reports: list[TestReport]) -> str:
        for report in host_reports:
            if report.test_type and report.test_type != "UNKNOWN":
                return report.test_type
        value = self._first_match(text, self._TEST_TYPE_PATTERNS)
        if value:
            return value.upper().replace("ANDROID-", "")
        return "UNKNOWN"

    def _report_to_dict(self, report: TestReport) -> dict[str, Any]:
        return {
            "summary": {
                "total": report.total,
                "pass": report.pass_count,
                "fail": report.fail_count,
                "pass_rate": report.pass_rate,
            },
            "details": {
                "test_type": report.test_type,
                "device": report.device,
                "suite_version": report.suite_version,
                "android_version": report.android_version,
                "start_time": report.start_time,
            },
            "failures": [
                {
                    "name": failure.name,
                    "reason": failure.reason,
                    "module": failure.module,
                    "stack_trace": failure.stack_trace,
                }
                for failure in report.failures
            ],
        }
