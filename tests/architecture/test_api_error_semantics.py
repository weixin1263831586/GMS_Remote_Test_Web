"""API error semantics gate: exception text must not leak into responses.

Unknown-exception ``str(e)`` can embed filesystem paths, SSH commands,
remote hostnames or credential fragments, so it must never reach an HTTP
response (or a persistent task error payload).  Curated domain errors raised
by narrow handlers (``ValueError``, feature ``*Error`` types) are fine —
this gate only flags ``except Exception`` (and bare ``except``) handlers
that pass the caught exception — or its string form — into a response
constructor:

    except Exception as e:                    # broad handler
        return error_response(str(e), 500)    # leak: flagged

The sanctioned replacement is ``foundation.error_model.record_internal_error``
plus a generic request_id-backed message (see ADR-anchored guidance in
``foundation/error_model.py``).  ``logger.exception`` / ``logger.error`` with
exception text remains allowed: full detail belongs in server logs only.
"""

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

SCAN_ROOTS = ("features", "foundation", "bootstrap")

# Response-constructor call names whose arguments are client-visible.
RESPONSE_CALL_NAMES = frozenset({"error_response", "JSONResponse", "HTTPException"})

# ApiError constructors (ApiError.malformed / .internal / .upstream_failure /
# ...) produce client-visible envelopes too.
API_ERROR_CLASS = "ApiError"


def _is_broad_handler(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    return isinstance(handler.type, ast.Name) and handler.type.id == "Exception"


def _is_response_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in RESPONSE_CALL_NAMES or func.id == API_ERROR_CLASS
    if isinstance(func, ast.Attribute):
        return (
            isinstance(func.value, ast.Name) and func.value.id == API_ERROR_CLASS
        )
    return False


def _references_exception(node: ast.AST, alias: str) -> bool:
    """True when ``node`` contains ``alias`` directly or as ``str(alias)``."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == alias:
            return True
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "str"
            and len(sub.args) == 1
            and isinstance(sub.args[0], ast.Name)
            and sub.args[0].id == alias
        ):
            return True
    return False


class ApiErrorSemanticsTests(unittest.TestCase):
    def test_broad_exception_text_never_reaches_api_responses(self):
        offenders = []
        for scan_root in SCAN_ROOTS:
            for path in (ROOT / scan_root).rglob("*.py"):
                relative = path.relative_to(ROOT).as_posix()
                if "/tests/" in relative or "__pycache__" in relative:
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ExceptHandler):
                        continue
                    if not _is_broad_handler(node):
                        continue
                    alias = node.name
                    if not alias:
                        continue
                    for sub in ast.walk(node):
                        if not isinstance(sub, ast.Call):
                            continue
                        if not _is_response_call(sub):
                            continue
                        args = [*sub.args, *sub.keywords]
                        for arg in args:
                            if arg is None:
                                continue
                            if _references_exception(arg, alias):
                                offenders.append(
                                    f"{relative}:{getattr(sub, 'lineno', 0)}"
                                )
                                break
        self.assertEqual(
            offenders,
            [],
            "broad 'except Exception' handlers must not pass the caught "
            "exception (or str(e)) into client-visible responses; use "
            "foundation.error_model.record_internal_error + a generic "
            "message instead (exception detail stays in server logs).",
        )


class RecordInternalErrorContractTests(unittest.TestCase):
    """record_internal_error 只接受 (logger, action, log_context) 位置参数。

    旧 API 的 ``*context_args`` 曾鼓励 "log_context 里写 %s + 位置参数"
    的 logging 占位符用法：占位符数量错配会触发 logging 的
    "not all arguments converted"，未 format 时字面 ``%s`` 残留进日志。
    现在附加定位信息一律走 keyword-only ``context={...}``（预渲染为
    ``key=value`` 追加），此门禁防止旧用法回潮。
    """

    def test_call_sites_use_keyword_context_and_no_placeholders(self):
        offenders = []
        for scan_root in SCAN_ROOTS:
            for path in (ROOT / scan_root).rglob("*.py"):
                relative = path.relative_to(ROOT).as_posix()
                if "__pycache__" in relative:
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    name = getattr(func, "id", getattr(func, "attr", ""))
                    if name != "record_internal_error":
                        continue
                    star_args = [a for a in node.args if isinstance(a, ast.Starred)]
                    if len(node.args) > 3 or star_args:
                        offenders.append(
                            f"{relative}:{node.lineno} positional args beyond "
                            "log_context; use context={...}"
                        )
                    if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
                        text = node.args[2].value
                        if isinstance(text, str) and "%" in text:
                            offenders.append(
                                f"{relative}:{node.lineno} log_context contains "
                                "'%'; placeholders are no longer formatted, "
                                "move the value into context={...}"
                            )
        self.assertEqual(
            offenders,
            [],
            "record_internal_error call sites must be "
            "(logger, action, log_context[, context={...}]) with literal "
            "log_context free of '%' placeholders.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
