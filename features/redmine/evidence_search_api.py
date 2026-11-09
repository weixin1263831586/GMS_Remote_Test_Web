"""Redmine 证据检索 API（从 evidence_api.py 拆出，检索拆分）。

`GET /evidence/{snapshot_id}/search`：manifest（description/journals）+
artifact 派生文本/原文全文匹配。

检索语义（Redmine #661066 复审后收紧，替代旧的"前缀 1MiB + 每文件首命中"）：
- **全量覆盖**：文本按增量块流式扫描整个文件，内存只保留滚动窗口；
  日志末尾的进程死亡/LMKD/Perfetto 异常同样能命中。
- **多次命中**：每个文件报告每一次出现（最多 limit+1 处，超出即提前
  停止扫描），同一进程多次死亡、先后因果链不再被吞掉。
- **大小写不敏感**：默认按小写折叠匹配（折叠不保长时自动退回精确匹配），
  snippet 仍按原文大小写截取。
- **覆盖诚实**：响应带 ``coverage_complete``/``scanned_bytes``/
  ``total_bytes``；``index_complete`` 只有在归档提取完整 **且** 全部候选
  文本都扫描到 EOF 时才为 true——"没搜到"和"还没搜到的部分"是两个概念。
- **权限一致**：搜索会返回原文片段，因此除 ``redmine.read`` 外还要求
  ``artifacts.read_own``（与 artifact 读取端点同界），限定 Scope 的
  Agent 不能借搜索提取无权读取的附件内容。

zip 附件的 v2 成员帧（``<<<zip-member-v2:<b64>:<len>>>``，写入侧限
2MiB/成员、32MiB 总量）逐成员全文匹配并给
``attachment:<file>.zip!/<member>:L<line>`` 行级引用；旧版行级标记快照
退回 1MiB 截断兜底，coverage 如实报告不完整。命中偏移与
``artifact_read`` 的字符偏移同一坐标空间（普通附件为全文件偏移，zip
成员为成员内偏移），可直接喂给后续上下文读取。
"""

from __future__ import annotations

import base64
import codecs
import json
import re
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from features.auth import require_agent_scope
from features.redmine.evidence import (
    _ZIP_MEMBER_V2_RE,
    split_zip_derived_text,
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
# 伪造成员帧的读取硬上限：v2 帧写入侧限 2MiB/成员，读侧给宽松上限，
# 防止恶意快照声明超大长度拖垮内存。
_MEMBER_TEXT_HARD_CAP = 16 * 1024 * 1024
# 旧版（v1 行级标记）派生文本的截断兜底窗口：旧格式没有可流式解析的
# 帧长度，保持旧的 1MiB 前缀行为，coverage 字段如实报告不完整。
_LEGACY_DERIVED_PREFIX_BYTES = 1024 * 1024
# v2 流式解析的缓冲上限：超过仍无帧头即认为不是 v2 派生文本。
_ZIP_STREAM_BUFFER_CAP = 4 * 1024 * 1024
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
    """编译多 needle 的交替正则；返回 (pattern, key→needle 映射, keep)。

    优先大小写不敏感（所有 needle 折叠保长时）；任一 needle 折叠不保长
    则整批退回大小写敏感的精确匹配。needle 按长度降序进 alternation，
    同一位置更长（更具体）的词优先命中。keep 是跨块不可见尾巴长度
    （最长 needle - 1）。映射键即 ``match.group(0)``。
    """
    probes: list[str] = []
    probe_map: dict[str, str] = {}
    foldable = True
    for needle in needles:
        if not needle:
            continue
        probe = needle.lower()
        if len(probe) != len(needle):
            foldable = False
    if foldable:
        for needle in needles:
            if not needle:
                continue
            probe = needle.lower()
            if probe not in probe_map:
                probe_map[probe] = needle
                probes.append(probe)
    else:
        for needle in needles:
            if needle and needle not in probe_map:
                probe_map[needle] = needle
                probes.append(needle)
    if not probes:
        return None, {}, 0
    probes.sort(key=len, reverse=True)
    flags = re.IGNORECASE if foldable else 0
    pattern = re.compile("|".join(re.escape(probe) for probe in probes), flags)
    keep = max(len(probe) for probe in probes) - 1
    return pattern, probe_map, keep


def _scan_text_multi(
    chunks,
    needles: list[str],
    *,
    stop_after: int,
    context: int = SNIPPET_CONTEXT_CHARS,
    with_lines: bool = False,
) -> list[tuple[str, _Occurrence]]:
    """滚动窗口全文扫描：返回 ``(命中 needle, 出现)`` 列表（≤ stop_after）。

    多关键词单遍交替匹配——批量搜索一次 IO/解码即可完成全部关键词
    匹配（#661066 复审：12 个关键词逐个搜索会反复扫描同一批文本）。

    大小写折叠仅在所有 needle 折叠保长时启用（日志文本几乎总是成立），
    否则整批退回精确匹配；snippet 始终取自未折叠原文。命中偏移是拼接
    流的 0 基字符偏移——对普通文件即 ``artifact_read`` 的 offset 坐标
    空间。

    跨块正确性：匹配只要在当前缓冲内完整出现即为真命中（literal 交替
    不会因后文改变）；因此块内无界扫描并按起点去重（``reported_to``），
    ``searched_to`` 推进到流末尾减 ``keep``——块尾 keep 区的起点可能
    因 needle 不完整而未匹配，必须留给下一轮/EOF 重扫，已报告的起点由
    去重跳过。滚动窗口保留 ``max(context, keep)`` 字符。
    """
    if not needles or stop_after <= 0:
        return []
    pattern, probe_map, keep = _compile_needles(needles)
    if pattern is None:
        return []
    retain = max(context, keep)

    low_buf = orig_buf = ""
    base = 0            # buf[0] 在全流中的字符偏移
    searched_to = 0     # 已完整检查过的流末尾（起点 < 它-keep 均已匹配）
    reported_to = 0     # 已报告命中的起点上界（< 它的都已报告，去重）
    newlines = 0        # orig_buf[0:..] 之前的换行总数
    pending = None      # (needle, 全局偏移, 头部 snippet, 完成所需流末尾, probe)
    results: list[tuple[str, _Occurrence]] = []

    def finish(hold):
        hit_needle, start, head, need_end, probe = hold
        rel_start = start - base
        match_len = len(probe)
        after = orig_buf[rel_start + match_len:max(rel_start + match_len,
                                                   need_end - base)]
        line = None
        if with_lines:
            line = newlines + orig_buf.count("\n", 0, rel_start) + 1
        return hit_needle, _Occurrence(start, head + after, line)

    def emit(hold) -> bool:
        results.append(finish(hold))
        return len(results) >= stop_after

    def hit_at(match) -> None:
        nonlocal pending, reported_to
        start = base + match.start()
        if start < reported_to:
            return  # 已在更早的 pass 报告过（EOF/下一轮重扫区）
        if pending is not None and emit(pending):
            raise _ScanFullError()
        reported_to = start + 1
        hit_needle = probe_map.get(match.group(0)) or probe_map.get(
            match.group(0).lower()
        )
        probe = match.group(0).lower() if hit_needle != match.group(0) else match.group(0)
        head = orig_buf[max(0, match.start() - context):match.end()]
        pending = (hit_needle, start, head,
                   start + len(probe) + context, probe)

    try:
        for chunk in chunks:
            low_buf += chunk
            orig_buf += chunk
            resume = max(searched_to - base, 0)
            for match in pattern.finditer(low_buf, resume):
                hit_at(match)
            # keep 尾区的起点可能因 needle 不完整未匹配，下一轮重扫。
            searched_to = base + len(low_buf) - keep
            # 裁剪滚动窗口：先补完会被裁掉的 pending 上下文。
            drop = len(low_buf) - retain
            if drop > 0:
                if pending is not None and base + drop > pending[3]:
                    if emit(pending):
                        raise _ScanFullError()
                    pending = None
                newlines += orig_buf.count("\n", 0, drop)
                low_buf = low_buf[drop:]
                orig_buf = orig_buf[drop:]
                base += drop

        # EOF：保留区之后的新起点全部报告。
        resume = max(searched_to - base, 0)
        for match in pattern.finditer(low_buf, resume):
            hit_at(match)
        if pending is not None:
            emit(pending)
    except _ScanFullError:
        pass
    return results


def _scan_text(chunks, needle: str, *, stop_after: int,
               context: int = SNIPPET_CONTEXT_CHARS,
               with_lines: bool = False) -> list[_Occurrence]:
    """单 needle 特例：见 :func:`_scan_text_multi`。"""
    return [
        occ
        for _, occ in _scan_text_multi(
            chunks, [needle], stop_after=stop_after,
            context=context, with_lines=with_lines,
        )
    ]


class _ScanFullError(Exception):
    """命中数到达 stop_after，提前终止扫描的内部信号。"""


def _scan_file_multi(path, needles: list[str], *, stop_after: int):
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

    hits = _scan_text_multi(
        _decode_chunks(raw_chunks()), needles, stop_after=stop_after
    )
    return hits, bytes_read >= total, bytes_read, total


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
    """文本域流式解析 v2 成员帧 → (成员名, 成员文本)。

    与 split_zip_derived_text 同一帧格式；帧间填充与旧版行级标记被跳过
    （调用方用空结果触发旧格式兜底）。成员体按帧声明长度读取，读取侧
    另设宽松硬上限防伪造长度；缓冲超限仍无帧头则放弃（非 v2 文本）。
    """
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        pending = ""
        while True:
            chunk = fh.read(_SEARCH_CHUNK_BYTES)
            if chunk:
                state.chars_scanned += len(chunk)
                pending += chunk
            while True:
                match = _ZIP_MEMBER_V2_RE.search(pending)
                if match is None:
                    break
                state.saw_frame = True
                try:
                    encoded = match.group(1)
                    padding = "=" * (-len(encoded) % 4)
                    name = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
                    length = int(match.group(2))
                except (ValueError, UnicodeDecodeError):
                    pending = pending[match.end():]
                    continue
                if length > _MEMBER_TEXT_HARD_CAP:
                    state.truncated_frame = True
                    pending = pending[match.end():]
                    continue
                body = pending[match.end():match.end() + length]
                while len(body) < length:
                    more = fh.read(length - len(body))
                    if not more:
                        state.truncated_frame = True
                        body = ""
                        break
                    state.chars_scanned += len(more)
                    body += more
                if not body and length:
                    pending = ""
                    break
                pending = pending[match.end() + length:]
                yield name, body
            if not chunk:
                state.complete = True
                return
            if len(pending) > _ZIP_STREAM_BUFFER_CAP:
                return  # 非 v2 文本：交由旧格式兜底


def _read_legacy_derived_prefix(path) -> str:
    """旧版行级标记派生文本的截断兜底（保持旧行为）。"""
    try:
        with path.open("rb") as fh:
            raw = fh.read(_LEGACY_DERIVED_PREFIX_BYTES)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


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


class BatchSearchRequest(BaseModel):
    """批量关键词搜索请求体：一次 IO 完成多个关键词的全文匹配。"""

    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(..., min_length=1)
    per_query_limit: int = Field(BATCH_PER_QUERY_DEFAULT_LIMIT, ge=1)


def _needle_key(probe: str, probe_map: dict[str, str]) -> str | None:
    """命中文本 → 原始 needle（折叠匹配按 lower 归一）。"""
    if probe in probe_map:
        return probe_map[probe]
    return probe_map.get(probe.lower())


@router.post("/evidence/{snapshot_id}/search/batch")
@handle_api_errors
async def search_evidence_batch(
    snapshot_id: str,
    payload: BatchSearchRequest,
    request: Request,
):
    """多关键词单遍全文搜索（#661066 复审：MCP→CLI→HTTP 逐关键词重复
    扫描同一批大日志的调用模式，改为一次服务端遍历完成全部匹配）。

    每个关键词独立计数与截断（``truncated``=该词命中超过 per_query_limit），
    覆盖语义与单查询端点一致：``coverage_complete=false`` 时零命中不可
    当作否定证据。
    """
    from features.redmine.evidence import EvidenceError

    # 与单查询端点同界：搜索返回原文片段，需要 artifacts.read_own。
    require_agent_scope("redmine.read")(request)
    require_agent_scope("artifacts.read_own")(request)
    owner_id = _owner(request)
    try:
        snapshot = _get_snapshot(owner_id, snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)

    needles: list[str] = []
    for query in payload.queries[:BATCH_MAX_QUERIES]:
        stripped = str(query or "").strip()
        if stripped and stripped not in needles:
            needles.append(stripped)
    per_query_limit = min(payload.per_query_limit, BATCH_PER_QUERY_MAX_LIMIT)
    per_query_stop = per_query_limit + 1
    if not needles:
        return _error_response(
            EvidenceError("queries 不能为空", status_code=422)
        )

    rows: dict[str, dict[str, Any]] = {
        needle: {"query": needle, "total": 0, "truncated": False, "matches": []}
        for needle in needles
    }

    def add_hit(needle: str, entry: dict[str, Any]) -> None:
        row = rows.get(needle)
        if row is None:
            return
        row["total"] += 1
        if row["total"] >= per_query_stop:
            row["truncated"] = True
            return
        row["matches"].append(entry)

    manifest = snapshot.get("manifest") or {}
    pattern, probe_map, _ = _compile_needles(needles)
    manifest_texts: list[tuple[str, str, dict[str, Any] | None]] = []
    description = str(manifest.get("description") or "")
    if description:
        manifest_texts.append(("description", description, None))
    for journal in manifest.get("journals") or []:
        notes = str(journal.get("notes") or "")
        if notes:
            manifest_texts.append(("journal", notes, journal))
        else:
            for detail in journal.get("details") or []:
                blob = json.dumps(detail, ensure_ascii=False)
                manifest_texts.append(("journal_detail", blob, journal))
    if pattern is not None:
        for kind, text, meta in manifest_texts:
            for match in pattern.finditer(text):
                needle = _needle_key(match.group(0), probe_map)
                if needle is None or rows[needle]["total"] >= per_query_stop:
                    continue
                entry: dict[str, Any] = {
                    "kind": kind,
                    "path": "",
                    "char_index": match.start(),
                    "snippet": text[
                        max(0, match.start() - SNIPPET_CONTEXT_CHARS)
                        :match.end() + SNIPPET_CONTEXT_CHARS
                    ],
                }
                if kind in {"journal", "journal_detail"} and meta is not None:
                    entry["journal_id"] = meta.get("id")
                add_hit(needle, entry)

    store = owner_evidence_store(owner_id)
    artifacts = store.list_artifacts(snapshot["snapshot_id"])
    index_warnings = [
        {
            "artifact_id": artifact.get("artifact_id"),
            "filename": artifact.get("filename"),
            "warning": artifact.get("error"),
        }
        for artifact in artifacts
        if str(artifact.get("kind") or "") == "archive"
        and str(artifact.get("derived_status") or "") in {"partial", "missing"}
        and artifact.get("error")
    ]

    scanned_bytes = 0
    total_bytes = 0
    coverage_complete = True

    def record_coverage(scanned: int, size: int, complete: bool) -> None:
        nonlocal scanned_bytes, total_bytes, coverage_complete
        scanned_bytes += scanned
        total_bytes += size
        if not complete:
            coverage_complete = False

    def saturated() -> bool:
        return all(row["total"] >= per_query_stop for row in rows.values())

    def artifact_sink(
        hit_needle: str,
        occ: _Occurrence,
        path: str,
        meta: dict[str, Any] | None,
        zip_member: str | None = None,
    ) -> None:
        if rows.get(hit_needle, {}).get("total", 0) >= per_query_stop:
            return
        add_hit(hit_needle, _occurrence_entry(
            occ, "artifact", path, meta, zip_member=zip_member,
        ))

    for artifact in artifacts:
        if saturated():
            break
        with store._connect() as conn:
            row = conn.execute(
                "SELECT derived_text_path, stored_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
                (str(artifact.get("artifact_id")),),
            ).fetchone()
        candidates: list[str] = []
        if row:
            derived_rel = str(row["derived_text_path"] or "")
            if derived_rel:
                candidates.append(derived_rel)
            if str(artifact.get("kind") or "") in _SEARCHABLE_RAW_KINDS:
                stored_rel = str(row["stored_path"] or "")
                if stored_rel:
                    candidates.append(stored_rel)
        for rel in candidates:
            # 空候选只跳过本项（真机 #648526 缺陷）；与单查询一致，
            # 同一附件只扫第一个非空候选。
            if not rel or saturated():
                continue
            try:
                path = store.resolve_internal(rel)
            except ValueError:
                continue
            if not path.is_file():
                continue
            is_derived = bool(row) and rel == str(row["derived_text_path"] or "")
            if is_derived and str(artifact.get("kind") or "") == "archive":
                state = _ZipStreamState()
                filename = str(artifact.get("filename") or "")
                member_path = f"attachment:{filename}!"
                for member, member_text in _iter_zip_members(path, state):
                    if saturated():
                        break
                    for hit_needle, occ in _scan_text_multi(
                        iter([member_text]), needles,
                        stop_after=per_query_stop * len(needles),
                        with_lines=True,
                    ):
                        artifact_sink(hit_needle, occ, member_path + member,
                                      artifact, zip_member=member)
                if state.saw_frame:
                    size = path.stat().st_size
                    complete = state.complete and not state.truncated_frame
                    record_coverage(size if complete else state.chars_scanned,
                                    size, complete)
                    break
                # 旧版行级标记/无成员标记：截断兜底（旧格式兼容）。
                text = _read_legacy_derived_prefix(path)
                legacy_members = split_zip_derived_text(text)
                if legacy_members:
                    size = path.stat().st_size
                    raw_len = len(text.encode("utf-8"))
                    record_coverage(
                        min(raw_len, size), size,
                        raw_len < _LEGACY_DERIVED_PREFIX_BYTES,
                    )
                    for legacy_needle, legacy_occ in _scan_text_multi(
                        iter([text]), needles,
                        stop_after=per_query_stop * len(needles),
                        with_lines=False,
                    ):
                        # 旧格式无流式帧，无法可靠还原成员行号；只报
                        # 全文级命中，避免伪造 member 引用。
                        artifact_sink(legacy_needle, legacy_occ, "", artifact)
                    break
                # 非 v2 且无旧标记：按普通文本全文扫描。
            hits, complete, scanned, size = _scan_file_multi(
                path, needles, stop_after=per_query_stop * len(needles)
            )
            record_coverage(scanned, size, complete)
            for hit_needle, occ in hits:
                artifact_sink(hit_needle, occ, "", artifact)
            break

    queries_out = []
    total_matches = 0
    for needle in needles:
        row = rows[needle]
        row["matches"] = row["matches"][:per_query_limit]
        row["truncated"] = row["total"] > per_query_limit
        total_matches += len(row["matches"])
        queries_out.append(row)

    return {
        "success": True,
        "data": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "queries": queries_out,
            "total_matches": total_matches,
            "coverage_complete": coverage_complete,
            "scanned_bytes": scanned_bytes,
            "total_bytes": total_bytes,
            "index_complete": coverage_complete and not index_warnings,
            "index_warnings": index_warnings,
        },
    }


@router.get("/evidence/{snapshot_id}/search")
@handle_api_errors
async def search_evidence(
    snapshot_id: str,
    request: Request,
    q: str = Query(..., min_length=1, max_length=SEARCH_MAX_QUERY_CHARS),
    limit: int = Query(SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT),
):
    from features.redmine.evidence import EvidenceError

    # 搜索返回带上下文的原文片段，与 artifact 读取端点同界（#661066
    # 复审）：限定 redmine.read 的 Agent 不能借搜索提取无权读取的内容。
    require_agent_scope("redmine.read")(request)
    require_agent_scope("artifacts.read_own")(request)
    owner_id = _owner(request)
    try:
        snapshot = _get_snapshot(owner_id, snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)
    needle = q
    matches: list[dict[str, Any]] = []
    stop = limit + 1

    manifest = snapshot.get("manifest") or {}
    description = str(manifest.get("description") or "")
    _collect_text_match(matches, "description", "", description, needle, None)
    for journal in manifest.get("journals") or []:
        notes = str(journal.get("notes") or "")
        hit = _collect_text_match(matches, "journal", "", notes, needle, journal)
        if not hit:
            for detail in journal.get("details") or []:
                blob = json.dumps(detail, ensure_ascii=False)
                _collect_text_match(matches, "journal_detail", "", blob, needle, journal)

    store = owner_evidence_store(owner_id)
    artifacts = store.list_artifacts(snapshot["snapshot_id"])
    index_warnings = [
        {
            "artifact_id": artifact.get("artifact_id"),
            "filename": artifact.get("filename"),
            "warning": artifact.get("error"),
        }
        for artifact in artifacts
        if str(artifact.get("kind") or "") == "archive"
        and str(artifact.get("derived_status") or "") in {"partial", "missing"}
        and artifact.get("error")
    ]

    scanned_bytes = 0
    total_bytes = 0
    coverage_complete = True

    def record_coverage(scanned: int, total: int, complete: bool) -> None:
        nonlocal scanned_bytes, total_bytes, coverage_complete
        scanned_bytes += scanned
        total_bytes += total
        if not complete:
            coverage_complete = False

    for artifact in artifacts:
        if len(matches) >= stop:
            break
        with store._connect() as conn:
            row = conn.execute(
                "SELECT derived_text_path, stored_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
                (str(artifact.get("artifact_id")),),
            ).fetchone()
        candidates: list[str] = []
        if row:
            derived_rel = str(row["derived_text_path"] or "")
            if derived_rel:
                candidates.append(derived_rel)
            # 原文兜底仅限文本类 kind；全量扫描与派生文本同一内存窗口。
            if str(artifact.get("kind") or "") in _SEARCHABLE_RAW_KINDS:
                stored_rel = str(row["stored_path"] or "")
                if stored_rel:
                    candidates.append(stored_rel)
        for rel in candidates:
            # 空候选（如无派生文本）只跳过本项；用 break 会让后面的
            # stored_path 永远不被扫描（真机 #648526 验收发现的缺陷）。
            if not rel:
                continue
            if len(matches) >= stop:
                break
            try:
                path = store.resolve_internal(rel)
            except ValueError:
                continue
            if not path.is_file():
                continue
            # 与旧行为一致：同一附件只扫第一个非空候选（派生文本是原文
            # 的镜像，重复扫描只浪费 IO 不产生新命中）。
            is_derived = bool(row) and rel == str(row["derived_text_path"] or "")
            if is_derived and str(artifact.get("kind") or "") == "archive":
                state = _ZipStreamState()
                filename = str(artifact.get("filename") or "")
                for member, member_text in _iter_zip_members(path, state):
                    if len(matches) >= stop:
                        break
                    hits = _scan_text(
                        iter([member_text]), needle,
                        stop_after=stop - len(matches), with_lines=True,
                    )
                    for occ in hits:
                        matches.append(_occurrence_entry(
                            occ, "artifact",
                            f"attachment:{filename}!{member}",
                            artifact, zip_member=member,
                        ))
                if state.saw_frame:
                    size = path.stat().st_size
                    complete = state.complete and not state.truncated_frame
                    record_coverage(size if complete else state.chars_scanned,
                                    size, complete)
                    break
                # 旧版行级标记/无成员标记：截断兜底（旧格式兼容）。
                text = _read_legacy_derived_prefix(path)
                members = split_zip_derived_text(text)
                if members:
                    size = path.stat().st_size
                    _collect_zip_member_matches(matches, members, needle, artifact, stop)
                    raw_len = len(text.encode("utf-8"))
                    record_coverage(
                        min(raw_len, size), size,
                        raw_len < _LEGACY_DERIVED_PREFIX_BYTES,
                    )
                    break
                # 派生文本既非 v2 也无旧标记：按普通文本全文扫描。
            hits, complete, scanned, size = _scan_file(
                path, needle, stop_after=stop - len(matches)
            )
            record_coverage(scanned, size, complete)
            matches.extend(
                _occurrence_entry(occ, "artifact", "", artifact) for occ in hits
            )
            break

    found_total = len(matches)
    matches = matches[:limit]
    return {
        "success": True,
        "data": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "query": q,
            "total": len(matches),
            "limited": found_total > limit,
            "index_complete": coverage_complete and not index_warnings,
            "index_warnings": index_warnings,
            "coverage_complete": coverage_complete,
            "scanned_bytes": scanned_bytes,
            "total_bytes": total_bytes,
            "matches": matches,
        },
    }
