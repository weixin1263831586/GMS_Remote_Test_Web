"""Typed Redmine evidence, APK, SDK, knowledge and image tools."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any


def apk_resolve_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    args: list[str] = []
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "query is required", True
    args.append(query)
    suite_types = str(arguments.get("suite_types") or "").strip()
    if suite_types:
        args.extend(["--types", suite_types])
    prefer = str(arguments.get("prefer") or "").strip()
    if prefer:
        args.extend(["--prefer", prefer])
    suite_path = str(arguments.get("suite_path") or "").strip()
    if suite_path:
        args.extend(["--suite-path", suite_path])
    return run_cli("gms-rt-apk-resolve", args)


def apk_analyze_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    args: list[str] = []
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "query is required", True
    args.append(query)
    suite_types = str(arguments.get("suite_types") or "").strip()
    if suite_types:
        args.extend(["--types", suite_types])
    prefer = str(arguments.get("prefer") or "").strip()
    if prefer:
        args.extend(["--prefer", prefer])
    suite_path = str(arguments.get("suite_path") or "").strip()
    if suite_path:
        args.extend(["--suite-path", suite_path])
    if arguments.get("wait"):
        args.append("--wait")
        max_wait = arguments.get("max_wait")
        if max_wait:
            args.extend(["--max-wait", str(int(max_wait))])
    return run_cli("gms-rt-apk-analyze", args)


def _apk_task_id_argument(arguments: dict[str, Any]) -> str:
    return str(arguments.get("task_id") or "").strip()


def apk_status_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = _apk_task_id_argument(arguments)
    if not task_id:
        return "task_id is required", True
    return run_cli("gms-rt-apk-status", [task_id])


def apk_manifest_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = _apk_task_id_argument(arguments)
    if not task_id:
        return "task_id is required", True
    args: list[str] = [task_id]
    if arguments.get("permissions"):
        args.append("--permissions")
    return run_cli("gms-rt-apk-manifest", args)


def apk_search_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = _apk_task_id_argument(arguments)
    query = str(arguments.get("query") or "").strip()
    if not task_id or not query:
        return "task_id and query are required", True
    args: list[str] = [task_id, query]
    mode = str(arguments.get("mode") or "name").strip()
    if mode not in ("name", "content", "symbol"):
        return "mode must be name, content, or symbol", True
    if mode != "name":
        args.extend(["--mode", mode])
    limit = arguments.get("limit")
    if limit:
        args.extend(["--limit", str(max(1, min(int(limit), 400)))])
    path_filter = str(arguments.get("path") or "").strip()
    if path_filter:
        args.extend(["--path", path_filter])
    line = arguments.get("line")
    if line:
        args.extend(["--line", str(max(0, int(line)))])
    return run_cli("gms-rt-apk-search", args)


def apk_source_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = _apk_task_id_argument(arguments)
    if not task_id:
        return "task_id is required", True
    path = str(arguments.get("path") or "").strip()
    if arguments.get("view"):
        if not path:
            return "path is required with view=true", True
        return run_cli("gms-rt-apk-source-read", [task_id, path])
    args: list[str] = [task_id]
    if path:
        args.append(path)
    return run_cli("gms-rt-apk-source", args)


class ToolContent:
    """Rich tool result: multiple MCP content items (text + image)."""

    def __init__(self, items: list[dict[str, Any]], is_error: bool = False):
        self.items = items
        self.is_error = is_error


# --------------------------------------------------------------------------
# Redmine evidence / APK / SDK typed tools
# --------------------------------------------------------------------------

def _int_arg(arguments: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    value = arguments.get(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


# artifact_read 的 offset 合法域上限：与 Controller 端 ge=0 一致地放行
# 大偏移（仅做合理性防呆），超出时报错并引导先用 search 定位。


def _image_file_result(
    image_b64: str, mime: str, stem: str, meta: dict[str, Any]
) -> ToolContent:
    """Persist an image payload to a local 0600 file and return its path.

    Agent hosts whose model cannot consume MCP image content (no vision
    input) fail at delivery time with errors like "current model does not
    declare image input support". The opt-in `as_file` argument lets those
    agents keep working: the bytes land on disk and the agent reads the
    file with its own file tools or hands it to a vision-capable subagent.
    """
    import base64
    import tempfile

    try:
        raw = base64.b64decode(image_b64, validate=True)
    except (ValueError, TypeError):
        return ToolContent(
            [{"type": "text", "text": "invalid base64 image payload"}],
            is_error=True,
        )
    mime = (mime or "image/png").lower()
    ext = "png" if "png" in mime else ("jpg" if ("jpeg" in mime or "jpg" in mime) else "img")
    fd, path = tempfile.mkstemp(prefix=f"gms-ev-{stem}-", suffix=f".{ext}")
    try:
        with os.fdopen(fd, "wb") as sink:
            sink.write(raw)
    except BaseException:
        os.unlink(path)
        raise
    os.chmod(path, 0o600)
    payload = {
        "path": path,
        "size_bytes": len(raw),
        # Distinct from meta["sha256"] (the ORIGINAL artifact hash from the
        # Controller): this is the hash of the bytes actually written to
        # disk — they differ when the Controller returned a scaled image.
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "mime_type": mime,
        **meta,
    }
    hint = (
        "Image saved to file (inline MCP image content skipped via "
        "as_file=true). Read it with file tools, pass the path to a "
        "vision-capable subagent, or re-run without as_file for inline "
        "image content."
    )
    return ToolContent([
        {"type": "text", "text": json.dumps(payload, ensure_ascii=False) + "\n" + hint},
    ])


def redmine_issue_fetch_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    issue = str(arguments.get("issue") or arguments.get("issue_id") or "").strip()
    if not issue:
        return "issue (id or URL) is required", True
    args: list[str] = [issue]
    download = str(arguments.get("download") or "").strip()
    if download:
        if download not in ("none", "analyzable", "all"):
            return "download must be none, analyzable, or all", True
        args.extend(["--download", download])
    if arguments.get("no_refresh"):
        args.append("--no-refresh")
    if arguments.get("dry_run"):
        # 只校验前置条件，不建快照。
        args.append("--dry-run")
    if arguments.get("wait"):
        args.append("--wait")
        args.extend(["--max-wait", str(_int_arg(arguments, "max_wait", 300, 5, 3600))])
    return run_cli("gms-rt-redmine-issue-fetch", args)


def redmine_issue_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    if not snapshot_id:
        return "snapshot_id is required", True
    return run_cli("gms-rt-redmine-issue-show", [snapshot_id])


def redmine_triage_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    """List today's pending issues (read-only triage entry point)."""
    args: list[str] = []
    if arguments.get("stale_days"):
        args.extend(["--stale-days", str(_int_arg(arguments, "stale_days", 3, 1, 30))])
    if arguments.get("list_limit"):
        args.extend(["--list-limit", str(_int_arg(arguments, "list_limit", 100, 1, 100))])
    if arguments.get("refresh"):
        args.append("--refresh")
    return run_cli("gms-rt-redmine-triage", args)


def redmine_references_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot = str(arguments.get("snapshot_id") or "").strip()
    if not snapshot:
        return "snapshot_id is required", True
    if len(snapshot) > 256 or len(str(arguments.get("query") or "")) > 256:
        return "snapshot_id and query must not exceed 256 characters", True
    args = [snapshot]
    if arguments.get("query"):
        args.extend(["--query", str(arguments["query"])])
    return run_cli("gms-rt-redmine-references", args)


def redmine_history_search_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Search ALL historical Redmine issues for similar problems + fixes."""
    query = str(arguments.get("q") or arguments.get("query") or "").strip()
    if not query:
        return "q (query) is required", True
    args: list[str] = [query]
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 8, 1, 20))])
    exclude = arguments.get("exclude_issue_id")
    if exclude:
        args.extend(["--exclude-issue-id", str(_int_arg(arguments, "exclude_issue_id", 0, 0, 99_999_999))])
    if arguments.get("resolved_only"):
        args.append("--resolved-only")
    return run_cli("gms-rt-redmine-history-search", args)


def redmine_journals_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    if not snapshot_id:
        return "snapshot_id is required", True
    args: list[str] = [snapshot_id]
    limit = arguments.get("limit")
    if limit:
        args.extend(["--limit", str(_int_arg(arguments, "limit", 50, 1, 100))])
    cursor = str(arguments.get("cursor") or "").strip()
    if cursor:
        args.extend(["--cursor", cursor])
    return run_cli("gms-rt-redmine-journals", args)


def redmine_attachments_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    if not snapshot_id:
        return "snapshot_id is required", True
    return run_cli("gms-rt-redmine-attachments", [snapshot_id])


def redmine_artifact_failures_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Parse a test-report artifact into a structured failure-case table.

    Server-side one-pass extraction (test_result.xml, summary HTML, zip
    archives): module / test / result / failure_message / first stack line /
    source per row — replaces the multi-round-trip manual scraping of STS
    summary HTML.
    """
    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return "artifact_id is required", True
    return run_cli("gms-rt-redmine-artifact-failures", [artifact_id])


def redmine_attachment_download_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Download one artifact original to a local file (agent host).

    Streams the original bytes (35MB test_result.xml, 800k-char logs) so the
    agent can grep/rg/jq the full text instead of paging char windows. The
    default output lands in the controlled state dir
    (~/.local/state/gms-remote-test/artifacts/, same level as profile
    tokens, 0600); pass output_path to choose another location.
    """
    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return "artifact_id is required", True
    args: list[str] = [artifact_id]
    output_path = str(arguments.get("output_path") or "").strip()
    if output_path:
        args.append(output_path)
    return run_cli("gms-rt-redmine-attachment-download", args)


def _evidence_offset(arguments: dict[str, Any], name: str, *, allow_negative: bool = False) -> int:
    """Parse an integer offset argument.

    ``allow_negative=True`` accepts tail-relative offsets (-1 = last char)
    for artifact_read; the Controller resolves them to an absolute window
    and reports both ``offset`` and ``requested_offset`` back.
    """
    raw = arguments[name]
    if isinstance(raw, (bool, float)):
        raise ValueError(f"{name} must be an integer")
    try:
        offset = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if offset < 0 and not allow_negative:
        raise ValueError(f"{name} must be a nonnegative integer")
    return offset


def redmine_artifact_search_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    query = str(arguments.get("query") or "").strip()
    if not snapshot_id or not query:
        return "snapshot_id and query are required", True
    args: list[str] = [snapshot_id, query]
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 50, 1, 200))])
    if arguments.get("offset") is not None:
        try:
            args.extend(["--offset", str(_evidence_offset(arguments, "offset"))])
        except (TypeError, ValueError) as exc:
            return str(exc), True
    for flag, key in (("--artifact", "artifact"), ("--member", "member")):
        value = str(arguments.get(key) or "").strip()
        if value:
            args.extend([flag, value])
    if arguments.get("context") is not None:
        args.extend(["--context", str(_int_arg(arguments, "context", 200, 0, 1000))])
    if arguments.get("collapse"):
        args.append("--collapse")
    return run_cli("gms-rt-artifact-search", args)


def redmine_artifact_search_batch_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Multi-keyword one-pass search: one CLI round trip matches every
    keyword server-side instead of re-scanning the same large logs per
    keyword. Coverage semantics match the single-query
    tool: with coverage_complete=false a zero hit is not negative
    evidence."""
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    if not snapshot_id:
        return "snapshot_id is required", True
    raw_queries = arguments.get("queries")
    queries: list[str] = []
    if isinstance(raw_queries, list):
        for item in raw_queries:
            text = str(item or "").strip()
            if text and text not in queries:
                queries.append(text)
    if not queries:
        return "queries must be a non-empty list of strings", True
    if len(queries) > 16:
        return "queries accepts at most 16 distinct keywords", True
    args: list[str] = [snapshot_id, *queries]
    if arguments.get("per_query_limit"):
        args.extend([
            "--per-query-limit",
            str(_int_arg(arguments, "per_query_limit", 20, 1, 100)),
        ])
    if arguments.get("per_query_offset") is not None:
        try:
            args.extend(["--per-query-offset", str(_evidence_offset(arguments, "per_query_offset"))])
        except (TypeError, ValueError) as exc:
            return str(exc), True
    for flag, key in (("--artifact", "artifact"), ("--member", "member")):
        value = str(arguments.get(key) or "").strip()
        if value:
            args.extend([flag, value])
    if arguments.get("context") is not None:
        args.extend(["--context", str(_int_arg(arguments, "context", 200, 0, 1000))])
    if arguments.get("collapse"):
        args.append("--collapse")
    return run_cli("gms-rt-artifact-search-batch", args)


def redmine_artifact_read_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return "artifact_id is required", True
    args: list[str] = [artifact_id]
    if arguments.get("offset") is not None:
        try:
            # 负 offset 合法：从尾部计数（-1 = 最后一个字符起），由
            # Controller 解析为绝对窗口并回传 requested_offset/offset。
            offset = _evidence_offset(arguments, "offset", allow_negative=True)
        except (TypeError, ValueError) as exc:
            return str(exc), True
        args.extend(["--offset", str(offset)])
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 65536, 1, 262144))])
    return run_cli("gms-rt-artifact-read", args)


def _image_tool_payload(
    run_cli, cli_command: str, args: list[str]
) -> tuple[dict[str, Any] | None, str, bool]:
    """Run the image CLI and return ``(data, raw_text, is_error)``.

    Empty base64 with an ok envelope is observed transiently under load
    (2026-09-24 daily brief: 16 such failures in one batch run); one
    immediate retry papers over that without teaching the model to blind-
    retry. ``None`` means the CLI did not return a JSON envelope; an empty
    dict means it returned valid JSON but still had no image payload after
    the retry.  Callers must keep those states distinct.
    """
    for attempt in (1, 2):
        text, is_error = run_cli(cli_command, args)
        if is_error:
            return {}, text, True
        try:
            data = json.loads(text).get("data") or {}
        except (ValueError, AttributeError):
            return None, text, False
        if str(data.get("base64") or ""):
            return data, text, False
        if attempt == 1:
            continue
    return data, text, False


def _image_unavailable_content(cli_command: str, args: list[str]) -> ToolContent:
    """Actionable error for an image payload that never arrived.

    Tells the model exactly which fallbacks exist instead of a bare
    "no payload" it can only retry blindly.
    """
    fallback = (
        "可改用 gms-rt-redmine-attachment-download 下载原件（不占会话上下文，"
        "按路径/元数据引用即可）"
        if cli_command == "gms-rt-redmine-artifact-image"
        else "可改用 gms-rt-devices-ui-dump / gms-rt-devices-logcat 收集文本证据"
    )
    return ToolContent(
        [{
            "type": "text",
            "text": (
                "controller 未返回图片载荷（已自动重试 1 次）。不要盲目重试本调用，"
                f"{fallback}；或在报告 missing_information 中记录"
                "「图片不可读取」，证据门禁按基础设施不可用处理，不影响 root_cause_type。"
            ),
        }],
        is_error=True,
    )


def devices_screencap_tool(run_cli, arguments: dict[str, Any]) -> ToolContent:
    """Capture one device screenshot and return MCP image content.

    Read-only UI diagnosis for agents: replaces the manual
    screencap -> pull -> read-file loop. The device must be visible to the
    controller; a device currently leased by another client is rejected
    server-side with a conflict error. With ``as_file=true`` the image is
    written to a local 0600 file and its path is returned as text instead
    of inline image content — for hosts whose model has no vision input.
    """
    device = str(arguments.get("device") or "").strip()
    if not device:
        return ToolContent(
            [{"type": "text", "text": "device (serial) is required"}], is_error=True
        )
    # The CLI envelope carries data.base64/mime_type; convert to image
    # content. _image_tool_payload retries once on an empty payload.
    data, text, is_error = _image_tool_payload(run_cli, "gms-rt-devices-screencap", [device])
    if is_error:
        return ToolContent([{"type": "text", "text": text}], is_error=True)
    if data is None:
        # Not a JSON envelope — surface the raw CLI text for diagnosis.
        return ToolContent([{"type": "text", "text": text}], is_error=False)
    image_b64 = str(data.get("base64") or "")
    mime = str(data.get("mime_type") or "image/png")
    if not image_b64:
        return _image_unavailable_content("gms-rt-devices-screencap", [device])
    meta = {"device_id": str(data.get("device_id") or device)}
    if arguments.get("as_file"):
        return _image_file_result(image_b64, mime, "screencap", meta)
    return ToolContent([
        {"type": "text", "text": json.dumps(meta, ensure_ascii=False)},
        {"type": "image", "mimeType": mime, "data": image_b64},
    ])


def redmine_image_tool(run_cli, arguments: dict[str, Any]) -> ToolContent:
    """Fetch an evidence artifact image and return MCP image content.

    The Controller endpoint enforces size limits and returns base64 with the
    original artifact metadata; oversized images surface as tool errors with
    a download hint instead of being silently cropped. With
    ``as_file=true`` the image is written to a local 0600 file and its path
    is returned as text instead of inline image content — for hosts whose
    model has no vision input.
    """
    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return ToolContent(
            [{"type": "text", "text": "artifact_id is required"}], is_error=True
        )
    args = [artifact_id]
    # The CLI envelope carries data.base64/mime_type; convert to image
    # content. _image_tool_payload retries once on an empty payload.
    data, text, is_error = _image_tool_payload(
        run_cli, "gms-rt-redmine-artifact-image", args
    )
    if is_error:
        return ToolContent([{"type": "text", "text": text}], is_error=True)
    if data is None:
        return ToolContent([{"type": "text", "text": text}], is_error=False)
    image_b64 = str(data.get("base64") or "")
    mime = str(data.get("mime_type") or "image/png")
    if not image_b64:
        return _image_unavailable_content("gms-rt-redmine-artifact-image", args)
    meta = {
        k: v for k, v in data.items()
        if k in ("artifact_id", "size_bytes", "sha256", "scaled", "derived_sha256")
    }
    if arguments.get("as_file"):
        return _image_file_result(image_b64, mime, "artifact", meta)
    return ToolContent([
        {"type": "text", "text": json.dumps(meta, ensure_ascii=False)},
        {"type": "image", "mimeType": mime, "data": image_b64},
    ])


def apk_analyze_attachment_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not snapshot_id or not artifact_id:
        return "snapshot_id and artifact_id are required", True
    return run_cli(
        "gms-rt-apk-analyze-attachment", [snapshot_id, artifact_id]
    )


def apk_source_search_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = str(arguments.get("task_id") or "").strip()
    query = str(arguments.get("query") or "").strip()
    if not task_id or not query:
        return "task_id and query are required", True
    args: list[str] = [task_id, query, "--mode", "content"]
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 50, 1, 200))])
    path_filter = str(arguments.get("path") or "").strip()
    if path_filter:
        args.extend(["--path", path_filter])
    return run_cli("gms-rt-apk-search", args)


def apk_source_read_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = str(arguments.get("task_id") or "").strip()
    path = str(arguments.get("path") or "").strip()
    if not task_id or not path:
        return "task_id and path are required", True
    args: list[str] = [task_id, path]
    if arguments.get("offset") is not None:
        args.extend(["--offset", str(_int_arg(arguments, "offset", 0, 0, 10_000_000))])
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 400, 1, 4000))])
    return run_cli("gms-rt-apk-source-read", args)


def sdk_sources_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    return run_cli("gms-rt-sdk-sources")


def sdk_search_tool(run_cli, arguments: dict[str, Any], *, exact_path: bool = False) -> tuple[str, bool]:
    source = str(arguments.get("source") or "").strip()
    revision = str(arguments.get("revision") or "").strip()
    # keywords 是 query 的别名：codesearch 风格工具用 keywords，调用方
    # 混用工具名时不应让整次检索失败。两字段严格互斥（schema oneOf 同
    # 约束）：同时出现说明调用请求本身有歧义，静默挑一个会掩盖错误。
    has_query = bool(str(arguments.get("query") or "").strip())
    has_keywords = bool(str(arguments.get("keywords") or "").strip())
    if has_query and has_keywords:
        return "provide exactly one of query/keywords, not both", True
    query = str(arguments.get("query") or arguments.get("keywords") or "").strip()
    if not source or not query:
        return "source and query (or its alias keywords) are required", True
    args = ["--source", source, "--query", query]
    if revision:
        args.extend(["--revision", revision])
    # revision 缺省：CLI 会用该 source 的 default_revision 解析，
    # 未知 source 时报错并列出可用 source。
    # limit 与 Controller 契约对齐（/api/sdk/search 的 limit le=200，
    # SEARCH_MAX_LIMIT）：越界显式报错而不是静默截断调用方的请求。
    raw_limit = arguments.get("limit")
    if raw_limit is not None and raw_limit != "":
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return "limit must be an integer in [1, 200]", True
        if not 1 <= limit <= 200:
            return "limit must be an integer in [1, 200]", True
        args.extend(["--limit", str(limit)])
    path_filter = str(arguments.get("path") or "").strip()
    if path_filter:
        args.extend(["--path", path_filter])
    repo_scope = str(arguments.get("repo_scope") or "").strip()
    if repo_scope:
        if repo_scope.startswith("/") or ".." in repo_scope.split("/"):
            return "repo_scope must be an exact child repository path", True
        args.extend(["--repo-scope", repo_scope])
    if exact_path:
        args.append("--exact-path")
    budget = arguments.get("budget_seconds")
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 600:
            return "budget_seconds must be an integer in [1, 600]", True
        args.extend(["--budget-seconds", str(budget)])
    return run_cli("gms-rt-sdk-search", args)


def sdk_read_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    result_id = str(arguments.get("result_id") or "").strip()
    if not result_id:
        return "missing required field: result_id", True
    # 只传 result_id；source/path/commit 已绑定在 token 内。
    args: list[str] = [result_id]
    if arguments.get("offset") is not None:
        args.extend(["--offset", str(_int_arg(arguments, "offset", 0, 0, 10_000_000))])
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 400, 1, 4000))])
    return run_cli("gms-rt-sdk-read", args)


def knowledge_search_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    # ADR 0014: background-only Android mechanism knowledge; results carry
    # provenance and are never root-cause evidence.
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "missing required field: query", True
    args = ["--query", query]
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 5, 1, 10))])
    if arguments.get("android_api_level") is not None:
        args.extend([
            "--android-api-level",
            str(_int_arg(arguments, "android_api_level", 1, 1, 1000)),
        ])
    return run_cli("gms-rt-knowledge-search", args)
