#!/usr/bin/env python3
"""Run repeatable soak/fault campaigns against a transport executor binary."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True, help="JSON transport executor command")
    parser.add_argument("--request", required=True, type=Path, help="request JSON file")
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--pause", type=float, default=0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stop-on-error", action="store_true")
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise SystemExit("request must be a JSON object")
    argv = shlex.split(args.command)
    if not argv:
        raise SystemExit("command is empty")
    samples = []
    for index in range(max(1, args.iterations)):
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                input=json.dumps(request, separators=(",", ":")),
                capture_output=True,
                text=True,
                timeout=args.timeout,
                check=False,
            )
            response = json.loads(completed.stdout or "{}")
            success = completed.returncode == 0 and response.get("success") is True
            error = "" if success else str(
                (response.get("error") or {}).get("message")
                if isinstance(response.get("error"), dict)
                else response.get("error") or completed.stderr
            )
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            success = False
            error = str(exc)
        samples.append({
            "iteration": index + 1,
            "success": success,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            "error": error[-1000:],
        })
        if not success and args.stop_on_error:
            break
        if args.pause > 0:
            time.sleep(args.pause)
    summary = {
        "requested_iterations": args.iterations,
        "completed_iterations": len(samples),
        "successes": sum(item["success"] for item in samples),
        "failures": sum(not item["success"] for item in samples),
        "maximum_elapsed_ms": max((item["elapsed_ms"] for item in samples), default=0),
        "samples": samples,
    }
    output = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0 if summary["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
