#!/usr/bin/env python3
"""Minimal stdio MCP adapter for the bundled codesearch client.

The adapter and the client it drives (scripts/codesearch.py plus
config/config.json) live inside the plugin directory, so the plugin is
self-contained. stdout is reserved for newline-delimited JSON-RPC; child
stdout/stderr is captured and returned as MCP tool content.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SERVER_NAME = "codesearch"
SERVER_VERSION = "0.8.1"
DEFAULT_TIMEOUT_SECONDS = 75
MAX_OUTPUT_BYTES = 1024 * 1024


def codesearch_script() -> Path:
    script = Path(__file__).resolve().parent / "codesearch.py"
    if not script.is_file():
        raise FileNotFoundError(f"bundled codesearch client is missing: {script}")
    return script


def bounded_text(data) -> str:
    """Truncate long output, keeping head and tail. Accepts str or bytes."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    if len(data) <= MAX_OUTPUT_BYTES:
        return data
    head = data[: MAX_OUTPUT_BYTES // 2]
    tail = data[-MAX_OUTPUT_BYTES // 2 :]
    return (
        head
        + "\n...[output truncated by codesearch MCP adapter]...\n"
        + tail
    )


_CLIENT_MODULE: Any = None


def load_client_module():
    """Import the bundled client once and reuse it across tool calls."""
    global _CLIENT_MODULE
    if _CLIENT_MODULE is None:
        script = codesearch_script()
        spec = importlib.util.spec_from_file_location("codesearch_client", script)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load codesearch client: {script}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CLIENT_MODULE = module
    return _CLIENT_MODULE


def run_codesearch_inprocess(arguments: list[str]) -> tuple[str, bool] | None:
    """Run the bundled client in-process (no per-call interpreter startup).

    Returns None when in-process mode is unusable so the caller can fall
    back to the isolated subprocess path.
    """
    try:
        module = load_client_module()
    except Exception as error:
        print(f"codesearch MCP adapter: cannot load client in-process, using subprocess: {error}", file=sys.stderr)
        return None

    out_buf, err_buf = io.StringIO(), io.StringIO()
    old_argv = sys.argv
    sys.argv = [str(codesearch_script()), *arguments]
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            module.main()
    except SystemExit as exc:
        # argparse errors (exit code 2) and client exits land here.
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if code != 0:
            detail = err_buf.getvalue().strip() or out_buf.getvalue().strip() or f"codesearch.py exited with {code}"
            return f"search failed: {bounded_text(detail)}", True
    except ValueError as error:
        # Client-side argument validation must stay loud, not fall back silently.
        return f"search failed: {error}", True
    except Exception as error:
        print(f"codesearch MCP adapter: in-process run failed, falling back to subprocess: {error}", file=sys.stderr)
        return None
    finally:
        sys.argv = old_argv

    stdout = bounded_text(out_buf.getvalue()).strip()
    stderr = bounded_text(err_buf.getvalue()).strip()
    if stderr:
        # Keep warnings (e.g. per-request degradations) visible without failing the call.
        return (stdout or "No results") + (f"\n[stderr] {stderr}" if stderr else ""), False
    return stdout or "No results", False


def run_codesearch(arguments: list[str]) -> tuple[str, bool]:
    result = run_codesearch_inprocess(arguments)
    if result is not None:
        return result

    command = [sys.executable, str(codesearch_script()), *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=os.getcwd(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            f"search timed out after {DEFAULT_TIMEOUT_SECONDS} seconds",
            True,
        )

    stdout = bounded_text(completed.stdout).strip()
    stderr = bounded_text(completed.stderr).strip()
    if completed.returncode == 0:
        return stdout or "No results", False
    detail = stderr or stdout or f"codesearch.py exited with {completed.returncode}"
    return f"search failed: {detail}", True


def search_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    keywords = str(arguments.get("keywords", "")).strip()
    if not keywords:
        return "Missing required argument: keywords", True

    command = ["search", "--keywords", keywords]
    optional_strings = (
        ("keyword_mode", "--keyword-mode"),
        ("search_field", "--search-field"),
        ("project", "--project"),
        ("type", "--type"),
        ("path", "--path"),
    )
    for key, flag in optional_strings:
        value = arguments.get(key)
        if value is not None and str(value).strip():
            command.extend([flag, str(value).strip()])

    if arguments.get("limit") is not None:
        try:
            limit = max(1, min(50, int(arguments["limit"])))
        except (TypeError, ValueError):
            return "limit must be an integer", True
        command.extend(["--limit", str(limit)])
    return run_codesearch(command)


def tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "search",
            "description": (
                "Search configured remote source indexes for definitions, references, "
                "paths, or text. Results contain project-relative paths and line numbers. "
                "Prefer this over local grep for large repos. Use path (e.g. "
                "'frameworks/base/services', 'drivers/android') to narrow a previous hit "
                "to a subsystem. Wrap multi-word phrases in double quotes inside keywords."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "string",
                        "description": (
                            "One token or comma-separated tokens; wrap an exact "
                            'multi-word phrase in double quotes, e.g. "bind to service"'
                        ),
                    },
                    "keyword_mode": {
                        "type": "string",
                        "enum": ["and", "or"],
                        "default": "and",
                    },
                    "search_field": {
                        "type": "string",
                        "enum": ["smart", "def", "symbol", "path", "full"],
                        "default": "smart",
                    },
                    "project": {
                        "type": "string",
                        "description": "Optional comma-separated project names",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional path scope, AND-matched against file paths: "
                            "'frameworks/base', 'drivers/net/ethernet'. Combine with "
                            "full/def/symbol/path fields to focus on a subsystem."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "enum": [
                            "c",
                            "cxx",
                            "java",
                            "kotlin",
                            "python",
                            "sh",
                            "golang",
                            "rust",
                        ],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["keywords"],
                "additionalProperties": False,
            },
        },
        {
            "name": "projects",
            "description": "List projects available from the configured code-search service.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]


def response(request_id: Any, result: Any = None, error: Any = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        payload["result"] = result
    else:
        payload["error"] = error
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return

    if method == "initialize":
        requested = message.get("params", {}).get("protocolVersion")
        response(
            request_id,
            {
                "protocolVersion": requested or "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return
    if method == "ping":
        response(request_id, {})
        return
    if method == "tools/list":
        response(request_id, {"tools": tools()})
        return
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if name == "search":
                text, is_error = search_tool(arguments)
            elif name == "projects":
                text, is_error = run_codesearch(["list_projects"])
            else:
                response(
                    request_id,
                    error={"code": -32601, "message": f"Unknown tool: {name}"},
                )
                return
            response(
                request_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": is_error,
                },
            )
        except Exception as error:  # MCP boundary: convert failures to tool errors.
            response(
                request_id,
                {
                    "content": [{"type": "text", "text": f"search failed: {error}"}],
                    "isError": True,
                },
            )
        return
    response(
        request_id,
        error={"code": -32601, "message": f"Method not found: {method}"},
    )


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                handle(message)
        except Exception as error:
            print(f"codesearch MCP protocol error: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
