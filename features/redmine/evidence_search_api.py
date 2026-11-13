"""Redmine 证据检索 API（从 evidence_api.py 拆出，检索拆分）。

`GET /evidence/{snapshot_id}/search`：manifest（description/journals）+
artifact 派生文本/原文全文匹配。

检索语义：
- **全量覆盖**：文本按增量块流式扫描整个文件，内存只保留滚动窗口；
  日志末尾的进程死亡/LMKD/Perfetto 异常同样能命中。
- **多次命中**：扫描所有命中并统计总数；limit 仅控制响应分页，offset 可继续读取。
- **大小写不敏感**：默认按小写折叠匹配（折叠不保长时自动退回精确匹配），
  snippet 仍按原文大小写截取。
- **覆盖诚实**：响应带 ``coverage_complete``/``scanned_bytes``/
  ``total_bytes``；``index_complete`` 只有在归档提取完整 **且** 全部候选
  文本都扫描到 EOF 时才为 true——"没搜到"和"还没搜到的部分"是两个概念。
- **权限一致**：搜索会返回原文片段，因此除 ``redmine.read`` 外还要求
  ``artifacts.read_own``（与 artifact 读取端点同界），限定 Scope 的
  Agent 不能借搜索提取无权读取的附件内容。

ZIP v2 成员帧按声明字符长度分块全文匹配，旧索引同样扫描到 EOF。
成员引用给出行号与成员内 char_index；artifact_char_index 对应整份派生
文本，可直接传给 artifact_read 读取后续上下文。
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import json
import re
import sys
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from features.auth import require_agent_scope
from features.redmine.evidence import (
    _ZIP_MEMBER_V2_RE,
)
from features.redmine.evidence_api import (
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    SEARCH_MAX_QUERY_CHARS,
    SNIPPET_CONTEXT_CHARS,
    _error_response,
    _get_snapshot,
    _owner,
)
from features.redmine.evidence_store import owner_evidence_store
from foundation.errors import handle_api_errors


router = APIRouter(prefix="/api/redmine-agent")

# 单次读入内存的文本块（字节）：全量扫描靠增量块 + 滚动窗口完成，
# 不再限制只扫文件前缀。
_SEARCH_CHUNK_BYTES = 256 * 1024
# 原文兜底（stored_path）只对文本类 kind 生效；binary/image/apk/pdf 的
# 原文不可检索，读取只放大内存占用。
_SEARCHABLE_RAW_KINDS = {"text", "log"}


# ---------------------------------------------------------------------------
# 流式全文扫描
# ---------------------------------------------------------------------------

class _Occurrence:
    __slots__ = ("line", "offset", "snippet")

    def __init__(self, offset: int, snippet: str, line: int | None):
        self.offset = offset
        self.snippet = snippet
        self.line = line


def _decode_chunks(raw_chunks):
    """增量 UTF-8 解码：块边界截断的多字节序列由增量解码器持有，
    不会被朴素切块撕坏。"""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    for raw in raw_chunks:
        text = decoder.decode(raw)
        if text:
            yield text
    tail = decoder.decode(b"", True)
    if tail:
        yield tail


def _compile_needles(needles: list[str]):
    """Keep an independent matcher for each query, including case aliases."""
    patterns = {
        needle: re.compile(
            re.escape(needle), re.IGNORECASE if len(needle.lower()) == len(needle) else 0
        )
        for needle in needles if needle
    }
    return patterns, max((len(needle) for needle in patterns), default=1) - 1


class _TextScanState:
    def __init__(self):
        self.complete = False


def _scan_text_multi(
    chunks,
    needles: list[str],
    *,
    stop_after: int,
    context: int = SNIPPET_CONTEXT_CHARS,
    with_lines: bool = False,
    limits: dict[str, int] | None = None,
    scan_state: _TextScanState | None = None,
    on_match=None,
) -> list[tuple[str, _Occurrence]]:
    """Read/decode once, matching overlapping queries with separate budgets.

    The rolling window keeps both snippet context and incomplete matches.
    Only starts with enough following context are emitted before EOF.
    A saturated query retires without consuming another query's budget.
    ``complete`` is set only after matching reaches EOF, never just because
    the last raw chunk was read into memory.
    """
    if not needles or stop_after <= 0:
        return []
    patterns, keep = _compile_needles(needles)
    if not patterns:
        return []
    budgets = {needle: max(0, (limits or {}).get(needle, stop_after)) for needle in patterns}
    counts = dict.fromkeys(patterns, 0)
    searched_to = dict.fromkeys(patterns, 0)
    buffer = ""
    base = newlines = 0
    results: list[tuple[str, _Occurrence]] = []

    def scan(end: int) -> bool:
        for needle, pattern in patterns.items():
            resume = max(0, searched_to[needle] - base)
            while counts[needle] < budgets[needle]:
                match = pattern.search(buffer, resume)
                if match is None or match.start() >= end:
                    break
                start = match.start()
                line = newlines + buffer.count("\n", 0, start) + 1 if with_lines else None
                occurrence = _Occurrence(
                    base + start,
                    buffer[max(0, start - context):match.end() + context],
                    line,
                )
                if on_match is None:
                    results.append((needle, occurrence))
                else:
                    on_match(needle, occurrence)
                counts[needle] += 1
                if len(results) >= stop_after:
                    return True
                resume = start + 1
            searched_to[needle] = base + end
        return all(counts[needle] >= budgets[needle] for needle in patterns)

    for chunk in chunks:
        buffer += chunk
        end = max(0, len(buffer) - keep - context)
        if scan(end):
            return results
        drop = max(0, end - context)
        newlines += buffer.count("\n", 0, drop)
        buffer = buffer[drop:]
        base += drop
    if not scan(len(buffer)) and scan_state is not None:
        scan_state.complete = True
    return results


def _scan_text(chunks, needle: str, *, stop_after: int,
               context: int = SNIPPET_CONTEXT_CHARS,
               with_lines: bool = False,
               scan_state: _TextScanState | None = None) -> list[_Occurrence]:
    """单 needle 特例：见 :func:`_scan_text_multi`。"""
    return [
        occ
        for _, occ in _scan_text_multi(
            chunks, [needle], stop_after=stop_after,
            context=context, with_lines=with_lines, scan_state=scan_state,
        )
    ]


def _scan_file_multi(path, needles: list[str], *, stop_after: int,
                     limits: dict[str, int] | None = None, on_match=None,
                     context: int = SNIPPET_CONTEXT_CHARS):
    """多关键词全文扫描单个文本文件
    →（[(needle, 命中)]，是否扫到 EOF，已扫字节，总字节）。"""
    total = path.stat().st_size
    bytes_read = 0

    def raw_chunks():
        nonlocal bytes_read
        with path.open("rb") as fh:
            while True:
                raw = fh.read(_SEARCH_CHUNK_BYTES)
                if not raw:
                    return
                bytes_read += len(raw)
                yield raw

    state = _TextScanState()
    hits = _scan_text_multi(
        _decode_chunks(raw_chunks()), needles, stop_after=stop_after,
        limits=limits, scan_state=state, on_match=on_match, context=context,
    )
    return hits, state.complete, bytes_read, total


def _scan_file(path, needle: str, *, stop_after: int):
    """单关键词特例：全文扫描 →（命中列表，是否扫到 EOF，已扫字节，总字节）。"""
    hits, complete, scanned, total = _scan_file_multi(path, [needle], stop_after=stop_after)
    return [occ for _, occ in hits], complete, scanned, total


class _ZipStreamState:
    """v2 成员流解析的完整性状态（coverage 上报用）。"""

    __slots__ = ("chars_scanned", "complete", "saw_frame", "truncated_frame")

    def __init__(self):
        self.complete = False
        self.truncated_frame = False
        self.chars_scanned = 0
        self.saw_frame = False


def _iter_zip_members(path, state: _ZipStreamState):
    """Yield (name, bounded text iterator, global character offset) per v2 frame.

    Declared lengths only control streaming, never an allocation. A forged
    frame inside a member body cannot create a different member reference.
    """
    with path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
        while True:
            header = handle.readline(_SEARCH_CHUNK_BYTES)
            state.chars_scanned += len(header)
            if not header:
                state.complete = True
                return
            if header.isspace():
                continue
            match = _ZIP_MEMBER_V2_RE.fullmatch(header)
            if match is None:
                state.truncated_frame = state.saw_frame or header.startswith("<<<zip-member-v2:")
                state.saw_frame = state.saw_frame or state.truncated_frame
                return
            state.saw_frame = True
            try:
                encoded = match.group(1)
                name = base64.b64decode(encoded + "=" * (-len(encoded) % 4),
                                       altchars=b"-_", validate=True).decode("utf-8")
                length = int(match.group(2))
            except (ValueError, UnicodeError):
                state.truncated_frame = True
                return
            remaining = length

            def chunks():
                nonlocal remaining
                while remaining:
                    text = handle.read(min(remaining, _SEARCH_CHUNK_BYTES))
                    if not text:
                        state.truncated_frame = True
                        return
                    state.chars_scanned += len(text)
                    remaining -= len(text)
                    yield text

            yield name, chunks(), state.chars_scanned
            if remaining:
                return


def _collect_text_match(
    matches: list[dict[str, Any]],
    kind: str,
    path: str,
    text: str,
    needle: str,
    meta: dict[str, Any] | None,
) -> bool:
    index = text.lower().find(needle.lower())
    if index < 0:
        return False
    start = max(0, index - SNIPPET_CONTEXT_CHARS)
    end = min(len(text), index + len(needle) + SNIPPET_CONTEXT_CHARS)
    entry: dict[str, Any] = {
        "kind": kind,
        "path": path,
        "char_index": index,
        "snippet": text[start:end],
    }
    if meta is not None:
        if kind in {"journal", "journal_detail"}:
            entry["journal_id"] = meta.get("id")
        else:
            entry["artifact_id"] = meta.get("artifact_id")
            entry["filename"] = meta.get("filename")
    matches.append(entry)
    return True


def _occurrence_entry(
    occ: _Occurrence,
    kind: str,
    path: str,
    meta: dict[str, Any] | None,
    zip_member: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": kind,
        "path": path,
        "char_index": occ.offset,
        "snippet": occ.snippet,
    }
    if occ.line is not None:
        entry["line"] = occ.line
    if zip_member is not None:
        entry["zip_member"] = zip_member
    if meta is not None:
        entry["artifact_id"] = meta.get("artifact_id")
        entry["filename"] = meta.get("filename")
    return entry


def _collect_zip_member_matches(
    matches: list[dict[str, Any]],
    members: list[tuple[str, str]],
    needle: str,
    artifact: dict[str, Any],
    limit: int,
) -> None:
    """内存成员列表匹配（旧格式兜底与测试辅助），引用为
    ``attachment:<file>.zip!/<member>`` 并带 1-based 行号。"""

    filename = str(artifact.get("filename") or "")
    folded = needle.lower()
    for member, member_text in members:
        if len(matches) >= limit:
            return
        index = member_text.lower().find(folded)
        if index < 0:
            continue
        start = max(0, index - SNIPPET_CONTEXT_CHARS)
        end = min(len(member_text), index + len(needle) + SNIPPET_CONTEXT_CHARS)
        matches.append(
            {
                "kind": "artifact",
                "path": f"attachment:{filename}!{member}",
                "line": member_text.count("\n", 0, index) + 1,
                "char_index": index,
                "snippet": member_text[start:end],
                "artifact_id": artifact.get("artifact_id"),
                "filename": filename,
                "zip_member": member,
            }
        )


BATCH_MAX_QUERIES = 16
BATCH_PER_QUERY_DEFAULT_LIMIT = 20
BATCH_PER_QUERY_MAX_LIMIT = 100
# snippet 上下文的合理上限：过大只会把每条命中撑成半屏文本。
SEARCH_MAX_CONTEXT_CHARS = 1000


class BatchSearchRequest(BaseModel):
    """Page size controls the response only; every query scans all evidence."""

    model_config = ConfigDict(extra="forbid")
    queries: list[str] = Field(..., min_length=1, max_length=BATCH_MAX_QUERIES)
    per_query_limit: int = Field(BATCH_PER_QUERY_DEFAULT_LIMIT, ge=1, le=BATCH_PER_QUERY_MAX_LIMIT)
    per_query_offset: int = Field(0, ge=0)
    artifact: str = Field("", max_length=2048)
    member: str = Field("", max_length=512)
    collapse: bool = Field(False)
    context: int = Field(SNIPPET_CONTEXT_CHARS, ge=0, le=SEARCH_MAX_CONTEXT_CHARS)


def _artifact_matches_filter(artifact: dict[str, Any], artifact_filter: str) -> bool:
    """artifact 过滤：art_* ID、数字附件 ID 或文件名子串（大小写不敏感）。"""
    needle = artifact_filter.strip().lower()
    if not needle:
        return True
    haystacks = (
        str(artifact.get("artifact_id") or ""),
        str(artifact.get("attachment_id") or ""),
        str(artifact.get("filename") or "").lower(),
        str(artifact.get("original_filename") or "").lower(),
    )
    return any(needle in value.lower() for value in haystacks if value)


def _member_matches_filter(member_name: str, member_filter: str) -> bool:
    return member_filter.strip().lower() in member_name.lower()


# 折叠窗口：同一 artifact 内相邻（字符距离 ≤ 此值）的重复命中只保留
# 第一条进入分页页，raw total 与 per-artifact 计数不受影响——
# "同一异常栈在 logcat 里重复 10 组"这类日志只占一页，但计数诚实。
COLLAPSE_GAP_CHARS = 200


def _search_all_evidence(store, snapshot, needles, limit, offset,
                         artifact_filter="", member_filter="",
                         collapse=False, context=SNIPPET_CONTEXT_CHARS):
    from .evidence_reindex import repair_budget_indexes

    repair_budget_indexes(store, snapshot)
    rows = {needle: {"query": needle, "total": 0, "matches": []} for needle in needles}
    warnings = []
    scanned_bytes = total_bytes = 0
    complete = True
    # per-artifact 命中计数摘要：不管分页截断，agent 都能先看到
    # "哪个附件命中多少"，再决定翻页还是按附件过滤。
    artifact_hits: dict[str, dict[str, Any]] = {}

    def note_artifact_hit(artifact) -> None:
        key = str(artifact.get("artifact_id") or "")
        row = artifact_hits.setdefault(key, {
            "artifact_id": artifact.get("artifact_id"),
            "filename": artifact.get("filename"),
            "hits": 0,
        })
        row["hits"] += 1

    def add_hit(needle, entry, *, collapsed=False):
        # total 始终按 raw 命中计数；分页页窗口用 raw 序号，折叠只影响
        # 页内是否携带该条 entry（False=携带）。
        row = rows[needle]
        index = row["total"]
        row["total"] += 1
        if collapsed:
            return
        if offset <= index < offset + limit:
            row["matches"].append(entry)

    def scan(chunks, sink, *, with_lines=False):
        state = _TextScanState()
        _scan_text_multi(chunks, needles, stop_after=sys.maxsize,
                         with_lines=with_lines, scan_state=state, on_match=sink,
                         context=context)
        return state.complete

    manifest = snapshot.get("manifest") or {}
    scan(iter([str(manifest.get("description") or "")]),
         lambda needle, occ: add_hit(needle, _occurrence_entry(occ, "description", "", None)))
    for artifact in store.list_artifacts(snapshot["snapshot_id"]):
        kind = str(artifact.get("kind") or "")
        if artifact.get("status") != "ready" or artifact.get("derived_status") in {"partial", "missing"}:
            warnings.append({"artifact_id": artifact["artifact_id"], "filename": artifact.get("filename"),
                             "warning": artifact.get("error") or "附件未完整下载或文本未完整提取"})
        if not _artifact_matches_filter(artifact, artifact_filter):
            continue
        with store._connect() as conn:
            row = conn.execute(
                "SELECT derived_text_path, stored_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
                (artifact["artifact_id"],),
            ).fetchone()
        rel = str(row["derived_text_path"] or "") if row else ""
        if not rel and row and kind in _SEARCHABLE_RAW_KINDS:
            rel = str(row["stored_path"] or "")
        if not rel:
            if kind in {*_SEARCHABLE_RAW_KINDS, "archive", "image", "pdf"} and artifact.get("derived_status") != "empty":
                if kind in {*_SEARCHABLE_RAW_KINDS, "archive"}:
                    complete = False
                warnings.append({"artifact_id": artifact["artifact_id"], "filename": artifact.get("filename"),
                                 "warning": artifact.get("error") or "附件尚无完整的可检索文本"})
            continue
        try:
            path = store.resolve_internal(rel)
            size = path.stat().st_size
            total_bytes += size
            # 折叠状态按 artifact 迭代重置；闭包经默认参数绑定当前迭代
            # 的 dict 对象（B023），sink 惰性调用时不会看到下一次迭代。
            collapse_last: dict[str, int] = {}

            def _collapsed(member_key, global_offset, _last=collapse_last) -> bool:
                if not collapse:
                    return False
                previous = _last.get(member_key)
                if previous is not None and abs(global_offset - previous) <= COLLAPSE_GAP_CHARS:
                    return True
                _last[member_key] = global_offset
                return False

            def artifact_sink(needle, occ, artifact=artifact):
                # per-artifact 计数在折叠判定之前：artifact_hits 必须
                # 诚实反映 raw 命中数，折叠只影响分页页内容。
                note_artifact_hit(artifact)
                if _collapsed("", occ.offset):
                    add_hit(needle, None, collapsed=True)
                    return
                entry = _occurrence_entry(occ, "artifact", "", artifact)
                entry["artifact_char_index"] = occ.offset
                entry["member_char_index"] = occ.offset
                add_hit(needle, entry)
            if kind == "archive":
                state = _ZipStreamState()
                for member, chunks, body_offset in _iter_zip_members(path, state):
                    def member_sink(needle, occ, member=member, body_offset=body_offset, artifact=artifact):
                        if member_filter and not _member_matches_filter(member, member_filter):
                            return
                        global_offset = body_offset + occ.offset
                        # 计数先于折叠（见 artifact_sink）。
                        note_artifact_hit(artifact)
                        if _collapsed(member, global_offset):
                            add_hit(needle, None, collapsed=True)
                            return
                        entry = _occurrence_entry(occ, "artifact",
                                                  f"attachment:{artifact['filename']}!{member}",
                                                  artifact, zip_member=member)
                        entry["artifact_char_index"] = global_offset
                        entry["member_char_index"] = occ.offset
                        add_hit(needle, entry)
                    # 成员内容必须被消费（流式推进文件位置），过滤在 sink 内完成。
                    complete = scan(chunks, member_sink, with_lines=True) and complete
                if state.saw_frame:
                    member_complete = state.complete and not state.truncated_frame
                    complete = complete and member_complete
                    scanned_bytes += size if member_complete else min(size, state.chars_scanned)
                    continue
                # Legacy/no-frame indexes are scanned to EOF in their global
                # coordinate space. Do not invent member references from text.
            _, done, scanned, _ = _scan_file_multi(
                path, needles, stop_after=sys.maxsize, on_match=artifact_sink,
                context=context,
            )
            complete = complete and done
            scanned_bytes += scanned
        except (OSError, ValueError, UnicodeError) as exc:
            complete = False
            warnings.append({"artifact_id": artifact["artifact_id"], "filename": artifact.get("filename"),
                             "warning": f"文本读取失败: {type(exc).__name__}"})
    for journal in manifest.get("journals") or []:
        notes = str(journal.get("notes") or "")
        texts = [("journal", notes)] if notes else [
            ("journal_detail", json.dumps(detail, ensure_ascii=False))
            for detail in journal.get("details") or []
        ]
        for kind, text in texts:
            def journal_sink(needle, occ, kind=kind, journal=journal):
                entry = _occurrence_entry(occ, kind, "", None)
                entry["journal_id"] = journal.get("id")
                add_hit(needle, entry)
            scan(iter([text]), journal_sink)

    for row in rows.values():
        row["offset"] = offset
        row["returned"] = len(row["matches"])
        row["truncated"] = row["total"] > offset + row["returned"]
        row["next_offset"] = offset + row["returned"] if row["truncated"] else None
    data = {"snapshot_id": snapshot["snapshot_id"], "queries": list(rows.values()),
            "total_matches": sum(len(row["matches"]) for row in rows.values()),
            "total_occurrences": sum(row["total"] for row in rows.values()),
            "coverage_complete": complete, "index_complete": complete and not warnings,
            "index_warnings": warnings, "scanned_bytes": scanned_bytes, "total_bytes": total_bytes,
            "artifact_hits": sorted(artifact_hits.values(), key=lambda row: -row["hits"]),
            "filters": {"artifact": artifact_filter, "member": member_filter,
                        "collapse": collapse, "context": context}}
    return data


@router.post("/evidence/{snapshot_id}/search/batch")
@handle_api_errors
async def search_evidence_batch(snapshot_id: str, payload: BatchSearchRequest, request: Request):
    from .evidence import EvidenceError

    require_agent_scope("redmine.read")(request)
    require_agent_scope("artifacts.read_own")(request)
    owner_id = _owner(request)
    snapshot = _get_snapshot(owner_id, snapshot_id)
    needles = list(dict.fromkeys(query.strip() for query in payload.queries if query.strip()))
    if not needles or any(len(query) > SEARCH_MAX_QUERY_CHARS for query in needles):
        return _error_response(EvidenceError("queries 不能为空且关键词长度不得超过上限", status_code=422))
    data = await asyncio.to_thread(
        _search_all_evidence, owner_evidence_store(owner_id), snapshot,
        needles, payload.per_query_limit, payload.per_query_offset,
        artifact_filter=payload.artifact, member_filter=payload.member,
        collapse=bool(payload.collapse), context=payload.context,
    )
    return {"success": True, "data": data}


@router.get("/evidence/{snapshot_id}/search")
@handle_api_errors
async def search_evidence(
    snapshot_id: str, request: Request,
    q: str = Query(..., min_length=1, max_length=SEARCH_MAX_QUERY_CHARS),
    limit: int = Query(SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    artifact: str = Query("", max_length=2048),
    member: str = Query("", max_length=512),
    collapse: bool = Query(False),
    context: int = Query(SNIPPET_CONTEXT_CHARS, ge=0, le=SEARCH_MAX_CONTEXT_CHARS),
):
    from .evidence import EvidenceError

    require_agent_scope("redmine.read")(request)
    require_agent_scope("artifacts.read_own")(request)
    owner_id = _owner(request)
    snapshot = _get_snapshot(owner_id, snapshot_id)
    needle = q.strip()
    if not needle:
        return _error_response(EvidenceError("q 不能为空", status_code=422))
    offset = offset if isinstance(offset, int) else 0
    # 直调兼容：绕过 FastAPI 依赖注入的单测直调里，Query 默认值仍是
    # Query 对象而非解析后的值——与上方 offset 的 isinstance 防御同型。
    if not isinstance(context, int) or isinstance(context, bool):
        context = SNIPPET_CONTEXT_CHARS
    context = max(0, min(int(context), SEARCH_MAX_CONTEXT_CHARS))
    if not isinstance(artifact, str):
        artifact = ""
    if not isinstance(member, str):
        member = ""
    if not isinstance(collapse, bool):
        collapse = False
    data = await asyncio.to_thread(
        _search_all_evidence, owner_evidence_store(owner_id), snapshot,
        [needle], limit, offset,
        artifact_filter=artifact, member_filter=member,
        collapse=collapse, context=context,
    )
    row = data.pop("queries")[0]
    data.update(query=needle, total=len(row["matches"]), total_occurrences=row["total"],
                matches=row["matches"], limited=row["truncated"],
                offset=offset, next_offset=row["next_offset"])
    return {"success": True, "data": data}
