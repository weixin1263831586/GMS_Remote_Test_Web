"""Read native result headers on the owning Worker without starting a JVM."""

from __future__ import annotations

import re
import stat
from pathlib import Path
from xml.parsers import expat


_RUN_DIRECTORY = re.compile(r"\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(?:\.\d+)?(?:_\d+)?")


class _HeaderCompleteError(Exception):
    pass


def read_report_header(xml_path: Path) -> dict:
    """Only return observed Result/Summary attributes; forbid DTDs/entities."""
    result = {}
    parser = expat.ParserCreate()

    def reject_doctype(*_args):
        raise ValueError("XML document types and entities are not allowed")

    def start(name, attributes):
        if name == "Result" and not result:
            result.update(attributes)
        elif name == "Summary" and result:
            result["summary"] = dict(attributes)
            raise _HeaderCompleteError

    parser.StartElementHandler = start
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.EntityDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = reject_doctype
    with xml_path.open("rb") as handle:
        try:
            for block in iter(lambda: handle.read(65536), b""):
                parser.Parse(block, False)
            parser.Parse(b"", True)
        except _HeaderCompleteError:
            return result
    raise ValueError("Result/Summary metadata is missing")


def native_report_summaries(suite_root: Path, *, test_type="", limit=100) -> dict:
    """Inspect every run directory before ordering/limiting the metadata."""
    limit = int(limit)
    if not 1 <= limit <= 100:
        raise ValueError("report limit must be between 1 and 100")
    if not suite_root.is_dir():
        raise FileNotFoundError("suite directory is unavailable")
    root = (suite_root / "results").resolve()
    if not root.is_relative_to(suite_root.resolve()):
        raise ValueError("results directory is outside the suite")
    if not root.exists():
        return {"reports": [], "count": 0, "scanned_results": 0,
                "coverage_complete": True, "errors": []}
    reports, errors = [], []
    scanned, seen = 0, set()
    for entry in root.iterdir():
        # Result XML identifies a report, not its directory spelling. Keep
        # expected timestamp directories with missing XML as explicit gaps.
        try:
            if not stat.S_ISDIR(entry.stat().st_mode) and not _RUN_DIRECTORY.fullmatch(entry.name):
                continue
            directory = entry.resolve()
            xml = (directory / "test_result.xml").resolve()
            if not directory.is_relative_to(root) or not xml.is_relative_to(directory):
                raise ValueError("report path escapes its result directory")
            if directory in seen:
                continue
            seen.add(directory)
            try:
                xml_stat = xml.stat()
            except FileNotFoundError:
                if not _RUN_DIRECTORY.fullmatch(entry.name):
                    continue
                raise
            if not stat.S_ISREG(xml_stat.st_mode):
                raise ValueError("report XML is not a regular file")
            scanned += 1
            header = read_report_header(xml)
            actual_type = str(header.get("suite_name") or "").upper()
            if test_type and actual_type != test_type.upper():
                continue
            summary = header["summary"]
            passed, failed = int(summary["pass"]), int(summary["failed"])
            start_ms = int(header["start"])
            if min(passed, failed, start_ms) < 0:
                raise ValueError("report contains invalid counts or timestamps")
            reports.append({
                "source": "native_suite", "registered": False,
                "timestamp": directory.name, "result_directory": directory.name,
                "result_dir": str(directory), "test_type": actual_type,
                "suite_version": header.get("suite_version", ""),
                "suite_build_number": header.get("suite_build_number", ""),
                "start_ms": start_ms, "start_display": header.get("start_display", ""),
                "end_display": header.get("end_display", ""),
                "devices": header.get("devices", ""),
                "pass": passed, "fail": failed, "total": passed + failed,
                "modules_done": summary.get("modules_done", ""),
                "modules_total": summary.get("modules_total", ""),
                "metadata_only": True,
            })
        except (OSError, ValueError, KeyError, expat.ExpatError) as exc:
            errors.append({"result_directory": entry.name,
                           "error": type(exc).__name__})
    reports.sort(key=lambda item: (item["start_ms"], item["timestamp"]), reverse=True)
    return {"reports": reports[:limit], "count": len(reports),
            "scanned_results": scanned, "coverage_complete": not errors, "errors": errors}
