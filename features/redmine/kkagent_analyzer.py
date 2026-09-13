"""KkAgentRedmineAnalyzer：通过 headless kkagent 做单 issue AI 分析。

调用约束（docs/plans/redmine-ai-daily-brief.md）：
- ``asyncio.create_subprocess_exec``，禁止 shell=True；
- 禁止 --yolo/--auto/--disable-sandbox；
- 超时由调用方配置（默认 600s）；
- 输出必须是指定 schema 的 JSON，非法 JSON 一律 invalid_ai_output，
  不做 regex 猜测修复；
- prompt 明确 Redmine 内容为不可信数据，不得作为指令执行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from dataclasses import dataclass
from typing import Any

from .daily_brief_models import validate_issue_result
from .daily_brief_prompt import PROMPT_TEMPLATE


logger = logging.getLogger(__name__)

PROMPT_VERSION = "redmine_daily_triage_v5"

# kkagent 可执行文件名（PATH 查找）；可通过配置覆盖绝对路径。
KKAGENT_BINARY = "kkagent"

# 认证预检：GMS agent token 被吊销/过期时，headless 分析会把整轮 turn
# 预算消耗在 MCP 认证失败上，不如在 run
# 开始前用 selfcheck 快速失败并给出重注册指引。
# 必须走正典安装路径而非 PATH 上的 gms-rt-* 包装器：包装器内嵌安装时
# 的绝对路径，若安装发生在 mktemp 目录会整体失效。
GMS_SELFCHECK_SCRIPT = os.path.expanduser(
    "~/.local/share/gms-remote-test/current/scripts/gms-remote-test.sh"
)
GMS_SELFCHECK_BINARY = "gms-rt-system-selfcheck"
SELFCHECK_TIMEOUT_SECONDS = 30.0

# ANSI 转义序列（颜色/光标控制/回车）：kkagent 的 stderr 日志即使设了
# NO_COLOR 也可能残留控制符，入库前统一剥除，避免 Web 端显示乱码。
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text).strip()


# stderr 摘要长度上限。
STDERR_SUMMARY_LIMIT = 500
PROCESS_STOP_GRACE_SECONDS = 5.0

# 子进程输出捕获上限：流式读取，固定保留头部 + 尾部，
# 中间部分丢弃，避免冗长日志把 stdout/stderr 全量吃进内存后才截断。
# JSON 信封在输出末尾、启动信息在开头，两端都有用；256KB 尾部足够
# 覆盖最终 JSON 与最近的错误日志。
CAPTURE_HEAD_BYTES = 16 * 1024
CAPTURE_TAIL_BYTES = 256 * 1024


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """TERM→grace→KILL 并回收独立进程组。"""
    if process.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:  # pragma: no cover - Windows fallback
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_STOP_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:  # pragma: no cover - Windows fallback
        process.kill()
    await process.wait()


async def _settle_reader_future(reader: asyncio.Future[Any]) -> None:
    """回收 communicate/gather，避免其异常在 event loop 关闭后泄漏。"""
    if not reader.done():
        # 子进程已在调用前收敛；此时取消仅剩的 pipe reader
        # 比无界等待更稳健，也不会遗留孤儿子进程。
        reader.cancel()
    try:
        await reader
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def read_stream_capped(
    stream: asyncio.StreamReader,
    head: int = CAPTURE_HEAD_BYTES,
    tail: int = CAPTURE_TAIL_BYTES,
) -> bytes:
    """读取子进程输出到 EOF，内存占用上限约 head+tail 字节。"""
    head_buf = bytearray()
    tail_buf = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if len(head_buf) < head:
            take = min(head - len(head_buf), len(chunk))
            head_buf.extend(chunk[:take])
            chunk = chunk[take:]
            if not chunk:
                continue
        tail_buf.extend(chunk)
        if len(tail_buf) > tail:
            del tail_buf[: len(tail_buf) - tail]
            truncated = True
    if truncated:
        # 丢弃点做标记：下游解析失败时能看出输出被截断过。
        return bytes(head_buf) + b"\n...[truncated]...\n" + bytes(tail_buf)
    return bytes(head_buf) + bytes(tail_buf)


# kkagent stderr 日志里的 RFC3339 时间戳(2026-09-13T10:31:49.070374Z)
# 对 UI 展示不友好:入库前统一转成 "2026-09-13 10:31:49.070"。
_ISO_TS_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?Z?\b"
)


def _normalize_log_timestamps(text: str) -> str:
    def _fmt(match: re.Match[str]) -> str:
        frac = (match.group(3) or "")[:3]
        return f"{match.group(1)} {match.group(2)}" + (f".{frac}" if frac else "")

    return _ISO_TS_RE.sub(_fmt, text)


def _summarize_stderr(text: str) -> str:
    """从 kkagent 的日志流里提取可读的失败原因。

    失败的真实原因（LLM 限流、配置错误等）几乎总在 ERROR 级行里；头部
    的 INFO/WARN 启动信息对排障没用。优先 ERROR 行，不足再从日志
    **尾部**补齐（越靠后越接近失败点），绝不只截开头。

    展示约束：行与行之间保留真实换行（详情弹窗按 pre-wrap 渲染，
    单行拼接 " | " 会把多行日志糊成一条无法阅读的长串）；RFC3339
    时间戳统一转为本地友好的 "YYYY-MM-DD HH:MM:SS.mmm" 格式。
    """
    lines = [
        _normalize_log_timestamps(ln.strip())
        for ln in _strip_ansi(text).splitlines()
        if ln.strip()
    ]
    error_lines = [
        ln for ln in lines if " ERROR " in ln or ln.startswith("ERROR")
    ]
    if error_lines:
        return "\n".join(error_lines)[:STDERR_SUMMARY_LIMIT]
    tail: list[str] = []
    for ln in reversed(lines):
        candidate = "\n".join([ln, *tail])
        if len(candidate) > STDERR_SUMMARY_LIMIT:
            break
        tail.insert(0, ln)
    return "\n".join(tail)[:STDERR_SUMMARY_LIMIT]

# kkagent 子进程不得继承宿主进程的 Agent 身份环境：多 owner 分析时，
# 泄漏的 GMS_RT_PROFILE / token 路径会让 MCP 以错误 owner（或 gms 服务
# 账号）取证。身份只能经 env_extra 显式注入（见 DailyBriefService）。
MCP_IDENTITY_ENV_KEYS = frozenset({
    "GMS_RT_PROFILE",
    "GMS_AGENT_PROFILE",
    "GMS_AGENT_CLIENT",
    "GMS_REMOTE_TEST_SERVER",
    "GMS_AUTH_TOKEN_FILE",
    "GMS_AGENT_AUTH_MODE",
})


def _child_env(env_extra: dict[str, str]) -> dict[str, str]:
    """kkagent 子进程环境：剥离继承的 Agent 身份，再显式注入目标身份。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in MCP_IDENTITY_ENV_KEYS
    }
    env.update(env_extra)
    env.setdefault("NO_COLOR", "1")
    return env


async def preflight_gms_auth(
    env_extra: dict[str, str],
    *,
    timeout_seconds: float = SELFCHECK_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """分析前校验 GMS agent token；返回 (通过或无法判定, 失效说明)。

    - 未配置 agent_profile 时直接放行（分析本就允许无 MCP 身份运行）；
    - selfcheck 不存在/超时/输出非法 → 放行（fail-open）：预检自身故障
      不得比没有预检更糟，兜底的 per-issue 超时与失败分类仍然有效；
    - selfcheck 明确报 authenticated=false → 拦截（fail-closed），避免
      逐条 issue 烧完整轮 turn 预算。
    """
    if not str(env_extra.get("GMS_RT_PROFILE") or "").strip():
        return True, ""
    command: list[str] | None = None
    if os.path.isfile(GMS_SELFCHECK_SCRIPT):
        command = ["bash", GMS_SELFCHECK_SCRIPT, "gms-rt-system-selfcheck", "--json"]
    else:
        import shutil

        if shutil.which(GMS_SELFCHECK_BINARY):
            command = [GMS_SELFCHECK_BINARY, "--json"]
    if command is None:
        return True, ""
    process: asyncio.subprocess.Process | None = None
    communication: asyncio.Future[tuple[bytes, bytes]] | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_child_env(env_extra),
            start_new_session=os.name == "posix",
        )
        communication = asyncio.ensure_future(process.communicate())
        stdout, _ = await asyncio.wait_for(
            asyncio.shield(communication), timeout=timeout_seconds
        )
    except OSError:
        return True, ""
    except asyncio.TimeoutError:
        if process is not None:
            await _terminate_process_tree(process)
        if communication is not None:
            await _settle_reader_future(communication)
        return True, ""
    except asyncio.CancelledError:
        if process is not None:
            cleanup = asyncio.create_task(_terminate_process_tree(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
        if communication is not None:
            await _settle_reader_future(communication)
        raise
    try:
        parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return True, ""
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, dict):
        return True, ""
    auth = data.get("auth")
    status = auth.get("status") if isinstance(auth, dict) else None
    if not isinstance(status, dict):
        # selfcheck --json 的 auth.status 是嵌套结构；宽容兼容直接挂在
        # data 上的扁平 authenticated 字段。
        status = data if isinstance(data.get("authenticated"), bool) else None
    if status is None or status.get("authenticated") is not False:
        return True, ""
    profile = str(data.get("profile") or env_extra.get("GMS_RT_PROFILE") or "")
    credential = data.get("credential")
    token_file = (
        str(credential.get("token_file") or "")
        if isinstance(credential, dict) else ""
    )
    message = (
        f"GMS agent token 失效（profile '{profile}'"
        + (f"，{token_file}" if token_file else "")
        + "）。请管理员在 Web UI 重新签发注册码，并在本机执行: "
        "gms-rt-agent-enroll <CODE>"
    )
    return False, message

@dataclass
class KkAgentAnalysisResult:
    ok: bool
    result: dict[str, Any] | None = None
    error: str = ""
    error_type: str = ""
    raw_output: str = ""
    exit_code: int | None = None


class KkAgentRedmineAnalyzer:
    """单 issue 分析器。无状态，可被多个 run 并发复用。"""

    def __init__(
        self,
        *,
        binary: str = KKAGENT_BINARY,
        max_turns: int = 12,
        timeout_seconds: int = 600,
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
        self.interrupted_retries = max(0, int(interrupted_retries))

    def build_prompt(self, entry: dict[str, Any]) -> str:
        return PROMPT_TEMPLATE.format(
            issue_id=entry.get("issue_id"),
            subject=str(entry.get("subject") or ""),
            status=str(entry.get("status_name") or ""),
            priority=str(entry.get("priority_name") or ""),
            buckets=", ".join(entry.get("buckets") or []),
            last_external_reply_at=str(entry.get("last_external_reply_at") or ""),
            unreplied_days=entry.get("unreplied_days", 0),
            attachment_count=entry.get("attachment_count", 0),
        )

    def build_command(self, prompt: str) -> list[str]:
        # 注意：kkagent 0.4.x 的 CLI 没有 --model 参数（传了会 exit 2）。
        # 模型经 KKAGENT_DEFAULT_MODEL 环境变量按次覆盖（见 analyze）。
        command = [
            self.binary,
            "--output-format", "json",
            "--max-turns", str(self.max_turns),
            "-p", prompt,
        ]
        return command

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

    async def _analyze_once(self, entry: dict[str, Any]) -> KkAgentAnalysisResult:
        """执行一次 headless 分析并校验 schema。"""
        prompt = self.build_prompt(entry)
        command = self.build_command(prompt)
        env = _child_env(self.env_extra)
        # 模型选择：CLI 无 --model 参数，用环境变量按次覆盖（不影响全局
        # 默认模型）。配置 model 为空时不注入，沿用 kkagent 自身默认。
        if self.model:
            env["KKAGENT_DEFAULT_MODEL"] = self.model

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # 隔离于 Web/Worker 的进程组。宿主重启时只取消 Worker，
                # 再由下面的受控清理终止 kkagent 及其 MCP 子进程树。
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError:
            return KkAgentAnalysisResult(
                ok=False, error=f"kkagent binary not found: {self.binary}",
                error_type="kkagent_unavailable",
            )
        readers = asyncio.gather(
            read_stream_capped(process.stdout),
            read_stream_capped(process.stderr),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(readers), timeout=self.timeout_seconds
            )
            await process.wait()
        except asyncio.TimeoutError:
            await self._terminate_process_tree(process)
            await _settle_reader_future(readers)
            return KkAgentAnalysisResult(
                ok=False,
                error=f"kkagent timed out after {self.timeout_seconds}s",
                error_type="timeout",
                exit_code=process.returncode,
            )
        except asyncio.CancelledError:
            # wait_for/调用方取消不得遗留 kkagent 或 stdio MCP 孤儿进程。
            cleanup = asyncio.create_task(self._terminate_process_tree(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            await _settle_reader_future(readers)
            raise

        raw = stdout.decode("utf-8", errors="replace").strip()
        exit_code = process.returncode
        if exit_code != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            summary = _summarize_stderr(stderr_text)
            # LLM 流式超时是根因时优先归类（kkagent 0.4.x 对长上下文
            # 请求有 300s 硬编码单请求超时，超时后 step 重试耗尽会连带
            # turn 预算用尽——信封 subtype=max_turns 只是表象）。
            if self._llm_stream_timeout(stderr_text):
                return KkAgentAnalysisResult(
                    ok=False,
                    error=(
                        "模型服务流式响应超时（大上下文请求超过服务端/客户端"
                        "时限）。将自动重试一次；若反复出现请在晨报设置中"
                        "换用更快的模型（如 glm-5.3-flash）。\n"
                        + (summary or f"exit {exit_code}")
                    ),
                    error_type="llm_timeout",
                    raw_output=raw[:20000],
                    exit_code=exit_code,
                )
            # 步数上限与中断是两种不同语义。kkagent 真正到达
            # --max-turns 会输出 subtype=max_turns 或明确的 limit reached
            # 日志；单独的 "Turn interrupted" 只表示 turn 被取消。
            if self._turn_limit_reached(raw, stderr_text):
                return KkAgentAnalysisResult(
                    ok=False,
                    error=(
                        f"kkagent 未在 {self.max_turns} 步预算内完成分析"
                        "（可在晨报设置中调大步数上限后重试）。\n"
                        + (summary or f"exit {exit_code}")
                    ),
                    error_type="max_turns",
                    raw_output=raw[:20000],
                    exit_code=exit_code,
                )
            interrupted = (
                self._interrupted_exit(exit_code, stderr_text)
                or self._turn_was_interrupted(stderr_text)
            )
            return KkAgentAnalysisResult(
                ok=False,
                error=(
                    (
                        f"kkagent turn interrupted ({self._signal_label(exit_code)})\n"
                        + summary
                    ).rstrip()
                    if interrupted else summary or f"exit {exit_code}"
                ),
                error_type="interrupted" if interrupted else "kkagent_error",
                raw_output=raw[:20000],
                exit_code=exit_code,
            )
        return self.parse_output(raw, exit_code)

    @staticmethod
    def _turn_limit_reached(raw: str, stderr_text: str) -> bool:
        """--max-turns 预算用尽。

        只接受结构化 subtype 或 kkagent 明确的上限日志；
        ``Turn interrupted`` 是取消语义，不能据此猜测步数用尽。
        """
        parsed = KkAgentRedmineAnalyzer._extract_json(raw)
        if isinstance(parsed, dict) and parsed.get("subtype") == "max_turns":
            return True
        return "Agent turn limit reached" in stderr_text

    @staticmethod
    def _turn_was_interrupted(stderr: str) -> bool:
        return "Turn interrupted" in stderr

    @staticmethod
    def _llm_stream_timeout(stderr: str) -> bool:
        """LLM 流式请求超时（kkagent stderr 实测形态）：

        ``LLM stream error: error sending request for url (...)
        [kind=request, kind=timeout]: operation timed out``
        （reqwest 总超时/连接超时都以 kind=timeout 呈现。）
        """
        if "LLM stream error" not in stderr:
            return False
        return "kind=timeout" in stderr or "operation timed out" in stderr

    @staticmethod
    def _interrupted_exit(exit_code: int | None, stderr: str) -> bool:
        if exit_code is None:
            return False
        signal_codes = {
            -signal.SIGINT,
            -signal.SIGTERM,
            128 + signal.SIGINT,
            128 + signal.SIGTERM,
        }
        return exit_code in signal_codes or "KeyboardInterrupt" in stderr

    @staticmethod
    def _signal_label(exit_code: int | None) -> str:
        if exit_code in (-signal.SIGINT, 128 + signal.SIGINT):
            return "SIGINT"
        if exit_code in (-signal.SIGTERM, 128 + signal.SIGTERM):
            return "SIGTERM"
        return f"exit {exit_code}"

    @staticmethod
    async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
        """TERM→grace→KILL 并回收独立进程组中的 kkagent/MCP。"""
        await _terminate_process_tree(process)

    def parse_output(self, raw: str, exit_code: int | None = None) -> KkAgentAnalysisResult:
        """解析并校验 kkagent 的 JSON 输出。"""
        parsed = self._extract_json(raw)
        if parsed is None:
            return KkAgentAnalysisResult(
                ok=False,
                error="kkagent output is not valid JSON",
                error_type="invalid_ai_output",
                raw_output=raw[:20000],
                exit_code=exit_code,
            )
        result = parsed.get("result") if isinstance(parsed.get("result"), dict) else None
        if result is None:
            # kkagent --output-format json 信封把模型最终回复放在 message
            # 字符串字段。可能是裸 JSON、markdown 围栏，或围栏前带说明散文
            # （如“证据不足，以下为保守分析”）——统一交给 _json_from_message
            # 提取；只做严格 json.loads，不做猜测修复。
            inner = self._json_from_message(parsed.get("message"))
            if isinstance(inner, dict):
                result = inner
        result = result if result is not None else parsed
        if not isinstance(result, dict):
            return KkAgentAnalysisResult(
                ok=False, error="kkagent output has no result object",
                error_type="invalid_ai_output", raw_output=raw[:20000],
                exit_code=exit_code,
            )
        errors = validate_issue_result(result)
        if errors:
            return KkAgentAnalysisResult(
                ok=False,
                error="schema validation failed: " + "; ".join(errors),
                error_type="schema_mismatch",
                raw_output=raw[:20000],
                exit_code=exit_code,
            )
        return KkAgentAnalysisResult(ok=True, result=result, raw_output=raw[:20000],
                                     exit_code=exit_code)

    @staticmethod
    def _json_from_message(message: Any) -> dict[str, Any] | None:
        """从 message 字符串提取 JSON 对象。

        接受三种形态（kkagent 0.4.x 实测均出现过）：
        - 裸 JSON 对象；
        - 整体包在 ```json ...``` 围栏内；
        - 说明散文 + 围栏 JSON（模型在证据不足时的常见输出）。

        只做严格 json.loads；提取不到返回 None，由上层报 invalid_ai_output。
        """
        if not isinstance(message, str):
            return None
        text = message.strip()
        fence_start = text.find("```")
        if fence_start >= 0:
            after = text[fence_start + 3:]
            if after.startswith("json"):
                after = after[4:]
            fence_end = after.find("```")
            if fence_end > 0:
                text = after[:fence_end].strip()
        if not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any] | None:
        """从输出中提取 JSON 对象；只接受首个平衡的 {...}，不做猜测修复。"""
        text = raw.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass
        # stream-json / 混合输出：找最后一个完整 JSON 对象行。
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue
        return None


__all__ = [
    "ANSI_ESCAPE_RE",
    "GMS_SELFCHECK_BINARY",
    "GMS_SELFCHECK_SCRIPT",
    "KKAGENT_BINARY",
    "MCP_IDENTITY_ENV_KEYS",
    "PROMPT_VERSION",
    "KkAgentAnalysisResult",
    "KkAgentRedmineAnalyzer",
    "preflight_gms_auth",
]
