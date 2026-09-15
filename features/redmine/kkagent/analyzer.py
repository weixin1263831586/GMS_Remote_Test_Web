"""KkAgentRedmineAnalyzer：通过 headless kkagent 做单 issue AI 分析。

编排职责：一次分析 = 启动进程 → 实时消费 stream-json → 证据门禁 →
（门禁/schema 未过）精确 resume 修复。进程管理、stderr 分类、轨迹、
解析、预检分别位于本包的 process/errors/trace/output/auth_preflight/
evidence_gate 模块。

调用约束（docs/architecture/adr/0008-daily-brief-triage-and-diagnosis.md）：
- ``asyncio.create_subprocess_exec``，禁止 shell=True；
- 禁止 --yolo/--auto/--disable-sandbox；
- 晨报不设分析步数或耗时硬预算，保留人工取消；
- 批量 triage 使用 schema JSON；单项诊断保留最终 Markdown 总结
  （docs/architecture/adr/0009-native-diagnostic-summary.md）；
- prompt 明确 Redmine 内容为不可信数据，不得作为指令执行。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from ..daily_brief_prompt import issue_result_schema_json, prompt_template_for
from .errors import classify_failure
from .evidence_gate import gate_and_errors
from .native_summary import native_summary_result
from .output import parse_issue_result
from .process import (
    STREAM_LINE_LIMIT_BYTES,
    child_env,
    settle_reader_future,
    terminate_process_tree,
)
from .trace import KkAgentTrace, ToolTrace, consume_line


# 与 __init__ 共享的常量（避免包级循环 import，直接定义于 process/本模块）。
KKAGENT_BINARY = "kkagent"
DAILY_BRIEF_MCP_TOOLSETS = "evidence"

logger = logging.getLogger(__name__)

# Prompt 版本随 runtime-owned evidence/schema repair 语义升级。
PROMPT_VERSION = "redmine_daily_triage_v13"

REPAIR_MAX_TURNS = 0
# 首次修复仍可能被模型原样重放（线上曾出现完整取证后连续漏掉
# confidence）。允许在同一 session 内再纠正一次；不重开会话、不重做取证。
REPAIR_RESUME_RETRIES = 2

REPAIR_PROMPT_TEMPLATE = """The previous analysis output did not pass validation.

Validation findings:
{findings}

Continue the existing analysis. Fix exactly these findings:
- Do NOT repeat evidence gathering that has already succeeded.
- Complete any missing evidence checks with the read-only GMS MCP tools.
- Schema findings (missing/invalid fields) are fixed by returning the
  corrected JSON only; do not invent new facts for missing evidence.
- "confidence" MUST be a JSON number between 0.0 and 1.0 (for example
  0.75). Never put an enum word like "likely" there — enum words belong
  to "root_cause_type" only.
- Then return the full corrected Daily Brief JSON object only
  (same schema as before, no prose, no markdown fences).
"""

SCHEMA_REPAIR_PROMPT_TEMPLATE = """The previous JSON output failed schema validation.

Remaining schema findings:
{findings}

Correct the existing answer in this exact session and return the FULL corrected
JSON object only. Do not call tools: the evidence gathering in this session is
already complete. Preserve all supported analysis and evidence from the previous
answer, changing only what is required to satisfy the schema.

Hard requirements:
- Include every field from the original Daily Brief JSON schema.
- `confidence` is mandatory and must be a JSON number from 0.0 to 1.0. Derive it
  from the evidence already gathered; do not use null and do not omit the field.
- Do not output `history_checked`; it is owned and injected by the runtime from
  successful history-search tool traces.
- Output raw JSON only: no prose and no markdown fences.
"""


@dataclass
class KkAgentAnalysisResult:
    """一次 issue 分析的最终结果（含可入库的执行轨迹摘要）。"""

    ok: bool
    result: dict[str, Any] | None = None
    error: str = ""
    error_type: str = ""
    raw_output: str = ""
    exit_code: int | None = None
    trace: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""


class KkAgentRedmineAnalyzer:
    """单 issue 分析器。无状态，可被多个 run 并发复用。"""

    def __init__(
        self,
        *,
        binary: str = KKAGENT_BINARY,
        max_turns: int = 0,
        timeout_seconds: int = 0,
        model: str = "",
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
        interrupted_retries: int = 1,
    ):
        self.binary = binary
        self.max_turns = int(max_turns)
        self.timeout_seconds = int(timeout_seconds)
        self.model = str(model or "").strip()
        self.cwd = cwd
        self.env_extra = dict(env_extra or {})
        # evidence-only MCP toolset：显式收敛子进程可见工具集（除非调用
        # 方已自带，例如测试注入更小集合）。
        self.env_extra.setdefault("GMS_MCP_TOOLSETS", DAILY_BRIEF_MCP_TOOLSETS)
        self.interrupted_retries = max(0, int(interrupted_retries))

    # ------------------------------------------------------------- command

    def build_prompt(self, entry: dict[str, Any]) -> str:
        prompt = prompt_template_for(entry).format(
            result_schema=issue_result_schema_json(),
            issue_id=entry.get("issue_id"),
            subject=str(entry.get("subject") or ""),
            status=str(entry.get("status_name") or ""),
            priority=str(entry.get("priority_name") or ""),
            buckets=", ".join(entry.get("buckets") or []),
            last_external_reply_at=str(entry.get("last_external_reply_at") or ""),
            unreplied_days=entry.get("unreplied_days", 0),
            attachment_count=entry.get("attachment_count", 0),
        )
        serial = str(entry.get("device_serial") or "").strip()
        if serial and entry.get("analysis_mode") != "triage":
            prompt += (
                f"\n\nLOCAL DEVICE (read-only diagnosis allowed): serial "
                f"`{serial}` is available to this analysis. You MAY use "
                f"gms_rt_devices_snapshot / gms_rt_logcat (dump mode) / "
                f"gms_rt_shell (read-only allowlist) on THIS serial only to "
                f"verify runtime facts (build fingerprint, kernel behavior, "
                f"logs). Describe the device observations in your final report from what you "
                f"actually observed; write 未检查本地设备 only if every "
                f"call failed. Never attempt to modify the device."
            )
        return prompt

    def build_command(self, prompt: str) -> list[str]:
        # 注意：kkagent 0.4.x 的 CLI 没有 --model 参数（传了会 exit 2）。
        # 模型经 KKAGENT_DEFAULT_MODEL 环境变量按次覆盖（见 analyze）。
        # stream-json：逐行 NDJSON 事件流——result 行很小且不会重复塞入
        # 全量 tool_calls[]，避免大日志把最终 JSON 挤出 head/tail 截断窗口。
        return [
            self.binary,
            "--output-format", "stream-json",
            *(["--max-turns", str(self.max_turns)] if self.max_turns > 0 else []),
            "-p", prompt,
        ]

    def build_repair_command(self, prompt: str, session_id: str, *, max_turns: int = REPAIR_MAX_TURNS) -> list[str]:
        """在**同一个** session 上继续：只补证据/修 JSON，不重开分析。"""
        return [
            self.binary,
            "--output-format", "stream-json",
            *(["--max-turns", str(max_turns)] if max_turns > 0 else []),
            "--resume", session_id,
            "-p", prompt,
        ]

    @staticmethod
    def build_repair_prompt(findings: list[str]) -> str:
        """按失败类型生成修复指令，schema-only 修复禁止重复取证。"""
        rendered = "\n".join(f"- {item}" for item in findings)
        if findings and all(item.startswith("schema") for item in findings):
            return SCHEMA_REPAIR_PROMPT_TEMPLATE.format(findings=rendered)
        return REPAIR_PROMPT_TEMPLATE.format(findings=rendered)

    # -------------------------------------------------------------- entry

    async def analyze(self, entry: dict[str, Any]) -> KkAgentAnalysisResult:
        """执行 headless 分析；可识别的 turn 中断/LLM 超时自动重试一次。"""
        outcome: KkAgentAnalysisResult | None = None
        for attempt in range(self.interrupted_retries + 1):
            outcome = await self._analyze_once(entry)
            retryable = outcome.error_type in ("interrupted", "llm_timeout")
            if not retryable or attempt >= self.interrupted_retries:
                return outcome
            logger.warning(
                "kkagent %s for issue %s; retrying once",
                outcome.error_type,
                entry.get("issue_id"),
            )
        return outcome  # pragma: no cover - loop always returns

    # ------------------------------------------------------------ process

    async def _run_stream(
        self, command: list[str]
    ) -> tuple[KkAgentTrace, _StreamFallback, bool]:
        """启动 kkagent 并实时消费 stream-json；返回 (轨迹, 原始兜底, 超时)。"""
        env = child_env(self.env_extra)
        if self.model:
            env["KKAGENT_DEFAULT_MODEL"] = self.model
        trace = KkAgentTrace()
        raw = _StreamFallback()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # 隔离于 Web/Worker 的进程组。宿主重启时只取消 Worker，
                # 再由受控清理终止 kkagent 及其 MCP 子进程树。
                start_new_session=os.name == "posix",
                limit=STREAM_LINE_LIMIT_BYTES,
            )
        except FileNotFoundError:
            trace.status = "kkagent_unavailable"
            trace.error = f"kkagent binary not found: {self.binary}"
            trace.error_type = "kkagent_unavailable"
            return trace, raw, False

        async def _pump_stderr() -> None:
            assert process.stderr is not None
            while True:
                chunk = await process.stderr.read(65536)
                if not chunk:
                    break
                raw.feed_stderr(chunk)

        readers = asyncio.ensure_future(_pump_stderr())
        try:
            assert process.stdout is not None
            while True:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=self.timeout_seconds or None
                )
                if not line:
                    break
                raw.feed_stdout(line)
                if not consume_line(trace, line):
                    continue
            await process.wait()
        except asyncio.TimeoutError:
            await terminate_process_tree(process)
            readers.cancel()
            await settle_reader_future(readers)
            trace.status = "timeout"
            trace.error_type = "timeout"
            trace.error = f"kkagent timed out after {self.timeout_seconds}s"
            return trace, raw, True
        except asyncio.CancelledError:
            # 取消不得遗留 kkagent 或 stdio MCP 孤儿进程。
            cleanup = asyncio.create_task(terminate_process_tree(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            readers.cancel()
            await settle_reader_future(readers)
            raise
        readers.cancel()
        await settle_reader_future(readers)
        trace.exit_code = process.returncode
        return trace, raw, False

    async def _analyze_once(self, entry: dict[str, Any]) -> KkAgentAnalysisResult:
        """执行一次 headless 分析：stream → gate → （失败时）resume 修复。"""
        prompt = self.build_prompt(entry)
        command = self.build_command(prompt)
        trace, raw, timed_out = await self._run_stream(command)

        if trace.error_type == "kkagent_unavailable":
            return self._failure(trace, raw)
        if timed_out:
            return self._failure(trace, raw)
        if trace.exit_code not in (0, None):
            stderr_tail = raw.stderr_tail()
            self._classify_nonzero_exit(trace, raw.text(), stderr_tail)
            return self._failure(trace, raw)

        if entry.get("analysis_mode") == "diagnostic":
            result = native_summary_result(trace, entry)
            if result is not None:
                trace.status = "completed"
                return self._success(result, trace, raw)
            trace.status = trace.error_type = "invalid_ai_output"
            trace.error = "kkagent 未返回最终分析总结。"
            return self._failure(trace, raw)

        result, errors = parse_issue_result(trace=trace, raw=raw.text())
        if result is None:
            # schema 失败（缺字段/类型不符）与 gate 失败一样是"可精确
            # 修复"的：findings 明确，--resume 同一 session 让模型补齐
            # JSON 即可。无法 resume 或修复轮仍失败时保持失败分类。
            schema_failed = bool(errors) and errors[0].startswith("schema")
            if schema_failed:
                repaired, trace = await self._repair(entry, trace, errors)
                if repaired is not None:
                    return repaired
            trace.error_type = (
                "schema_mismatch" if schema_failed else "invalid_ai_output"
            )
            trace.error = errors[0] if errors else "invalid output"
            return self._failure(trace, raw)

        gate, gate_errors_list = gate_and_errors(trace, entry, result)
        if not gate_errors_list:
            trace.status = "completed"
            return self._success(result, trace, raw)

        # Runtime gate 未过：在**同一 session** 上精确 resume 修复一次。
        repaired, trace = await self._repair(entry, trace, gate_errors_list)
        if repaired is not None:
            return repaired
        gate, gate_errors_list = gate_and_errors(trace, entry, result)
        trace.status = "evidence_gate_failed"
        trace.error_type = "evidence_gate_failed"
        trace.error = "; ".join(gate_errors_list)
        result["history_checked"] = bool(gate.get("history_checked"))
        return self._failure(trace, raw)

    async def _repair(
        self,
        entry: dict[str, Any],
        trace: KkAgentTrace,
        gate_errors_list: list[str],
    ) -> tuple[KkAgentAnalysisResult | None, KkAgentTrace]:
        """--resume <session_id> 补证据/修 schema，并保留全部修复轨迹。

        ``gate_errors_list`` 同时承载两类 findings：evidence gate 错误与
        schema 校验错误（"schema validation failed: ..."）。修复轮重新
        parse + 重新 gate，任何一类仍有残余即视为修复失败。
        """
        session_id = trace.session_id
        if not session_id:
            # 启动失败/认证失败/session 未创建：没有可 resume 的对象。
            return None, trace

        findings = list(gate_errors_list)
        merged = trace
        for _attempt in range(REPAIR_RESUME_RETRIES):
            repair_prompt = self.build_repair_prompt(findings)
            repair_trace, raw, timed_out = await self._run_stream(
                self.build_repair_command(repair_prompt, session_id)
            )
            repair_trace.repair_attempts = 1
            if repair_trace.session_id and repair_trace.session_id != session_id:
                repair_trace.status = "repair_failed"
                repair_trace.error_type = "repair_failed"
                repair_trace.error = "kkagent repair resumed a different session"
                repair_trace.session_id = session_id
            elif timed_out:
                repair_trace.error = repair_trace.error or "repair timed out"
            elif repair_trace.exit_code not in (0, None):
                self._classify_nonzero_exit(
                    repair_trace, raw.text(), raw.stderr_tail()
                )
            merged = _merge_traces(merged, repair_trace)
            if repair_trace.error_type:
                findings = [repair_trace.error or repair_trace.error_type]
                continue

            result, errors = parse_issue_result(trace=repair_trace, raw=raw.text())
            if result is None:
                findings = errors or ["repair output is invalid"]
                merged.errors.extend(findings[:10])
                continue
            _gate, findings = gate_and_errors(merged, entry, result)
            if findings:
                merged.errors.extend(findings[:10])
                continue
            merged.status = "completed"
            merged.error_type = ""
            merged.error = ""
            return self._success(result, merged, raw), merged
        return None, merged

    # ------------------------------------------------------------ helpers

    def _classify_nonzero_exit(self, trace: KkAgentTrace, raw: str, stderr_text: str) -> None:
        envelope_subtype = ""
        if isinstance(trace.final_event, dict):
            envelope_subtype = str(trace.final_event.get("subtype") or "")
        error_type, message = classify_failure(
            trace.exit_code, raw, stderr_text,
            envelope_subtype=envelope_subtype, max_turns=self.max_turns,
        )
        trace.status = error_type
        trace.error_type = error_type
        trace.error = message

    def _success(
        self, result: dict[str, Any], trace: KkAgentTrace, raw: _StreamFallback
    ) -> KkAgentAnalysisResult:
        return KkAgentAnalysisResult(
            ok=True,
            result=result,
            raw_output=raw.text()[:20000],
            exit_code=trace.exit_code,
            trace=trace.to_summary(),
            session_id=trace.session_id,
        )

    def _failure(
        self, trace: KkAgentTrace, raw: _StreamFallback
    ) -> KkAgentAnalysisResult:
        return KkAgentAnalysisResult(
            ok=False,
            error=trace.error or trace.error_type or "analysis failed",
            error_type=trace.error_type or "kkagent_error",
            raw_output=raw.text()[:20000],
            exit_code=trace.exit_code,
            trace=trace.to_summary(),
            session_id=trace.session_id,
        )


def _merge_traces(first: KkAgentTrace, second: KkAgentTrace) -> KkAgentTrace:
    """初跑 + 修复轮的轨迹合并（gate 用并集，usage 求和）。"""
    merged = KkAgentTrace(
        session_id=second.session_id or first.session_id,
        kkagent_version=first.kkagent_version or second.kkagent_version,
        subtype=second.subtype or first.subtype,
        exit_code=second.exit_code,
        resumed=True,
        duration_ms=first.duration_ms + second.duration_ms,
        rounds=first.rounds + second.rounds,
        turns=first.turns + second.turns,
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cache_read_tokens=first.cache_read_tokens + second.cache_read_tokens,
        cache_creation_tokens=(
            first.cache_creation_tokens + second.cache_creation_tokens
        ),
        llm_retries=first.llm_retries + second.llm_retries,
        repair_attempts=first.repair_attempts + second.repair_attempts,
        errors=[*first.errors, *second.errors],
        final_event=second.final_event or first.final_event,
        status=second.status or first.status,
        error_type=second.error_type,
        error=second.error,
    )
    seen_ids = {call.tool_call_id for call in first.tool_calls if call.tool_call_id}
    merged.tool_calls = list(first.tool_calls)
    for call in second.tool_calls:
        if call.tool_call_id and call.tool_call_id in seen_ids:
            continue
        merged.tool_calls.append(call)
    return merged


class _StreamFallback:
    """stdout/stderr 原始字节的有界兜底缓存。

    stream-json 主路径逐行解析；解析失败（老版本 kkagent、二进制杂讯、
    非 NDJSON 输出）时用这里保留的头尾文本走 extract_json 兜底。
    """

    def __init__(self) -> None:
        from .process import CappedCapture

        self.stdout = CappedCapture()
        self.stderr = CappedCapture()

    def feed_stdout(self, chunk: bytes | str) -> None:
        self.stdout.feed(chunk)

    def feed_stderr(self, chunk: bytes | str) -> None:
        self.stderr.feed(chunk)

    def text(self) -> str:
        return self.stdout.text()

    def stderr_tail(self) -> str:
        return self.stderr.text()


__all__ = [
    "PROMPT_VERSION",
    "REPAIR_MAX_TURNS",
    "KkAgentAnalysisResult",
    "KkAgentRedmineAnalyzer",
    "ToolTrace",
]
