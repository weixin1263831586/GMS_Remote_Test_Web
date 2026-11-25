"""Read-only live feature smoke tests of the deployed GMS Controller.

Never log HTTP bodies, tokens or cookies; probe only a reviewed list of GET
endpoints because other GET routes may synchronize or export data.
"""

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


# Each record: domain, stable GET path, must-pass.
PROBES = [
    ("system", "/api/system/health", True),
    ("system", "/api/system/health/live", True),
    ("auth", "/api/auth/status", True),
    ("cluster", "/api/cluster/status", True),
    ("cluster", "/api/cluster/workers", True),
    ("cluster", "/api/cluster/devices", True),
    ("cluster", "/api/cluster/suites", True),
    ("usbip", "/api/usbip/assignments", True),
    ("test", "/api/test/status", False),
    ("test", "/api/test/suites", False),
    ("devices", "/api/devices/list", False),
    ("firmware", "/api/firmware-shares", False),
    ("reports", "/api/reports/list", False),
    ("build", "/api/build/jobs", False),
    ("build", "/api/build/servers", False),
    ("automation", "/api/automation/dashboard", False),
    ("redmine", "/api/redmine-agent/status", False),
    ("gerrit", "/api/gerrit-dashboard/config", False),
    ("knowledge", "/api/knowledge/android-internals/status", False),
    ("knowledge", "/api/knowledgebase/stats", False),
    ("sdk", "/api/sdk/sources", False),
    ("ops", "/api/ops/status", False),
    ("gms-monitor", "/api/gms-update-center/summary", False),
    ("gms-radar", "/api/gms-update-radar/status", False),
    ("users", "/api/users/current", False),
    ("users", "/api/device-groups", False),
    ("tools", "/api/tools/list", False),
    ("apk", "/api/apk/tasks", False),
    ("email", "/api/email/outbox", False),
]


def verdict(code, required):
    if code is not None and 200 <= code < 300:
        return "PASS"
    if not required and code in {400, 401, 403, 404, 409, 422, 503}:
        return "UNAVAILABLE"
    return "FAIL"


def read_git_head(path):
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=8,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def audit(*, output, host_repo):
    base = os.getenv("GMS_SOAK_BASE_URL", "").rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ValueError("GMS_SOAK_BASE_URL missing")
    report = {
        "schema": 1,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "tested_sha": os.getenv("GITHUB_SHA", ""),
        "host_checkout_sha": read_git_head(host_repo),
        "authentication": "NOT_ATTEMPTED",
        "probes": [],
    }
    report["host_checkout_matches"] = bool(
        report["tested_sha"]
        and report["tested_sha"] == report["host_checkout_sha"]
    )
    failed = False
    session = requests.Session()
    try:
        token = os.getenv("GMS_SOAK_SESSION_TOKEN", "")
        user = os.getenv("GMS_SOAK_USERNAME", "")
        password = os.getenv("GMS_SOAK_PASSWORD", "")
        if token:
            session.cookies.set("gms_session", token)
        elif user and password:
            for path in ("/api/auth/login", "/api/auth/elevate"):
                res = session.post(
                    base + path, json={"username": user, "password": password},
                    timeout=25,
                )
                if res.status_code != 200:
                    raise RuntimeError("Platform admin authentication rejected")
        else:
            raise RuntimeError("Missing platform administrator credentials")
        report["authentication"] = "PASS"
        for domain, route, required in PROBES:
            start = time.monotonic()
            status = None
            try:
                response = session.get(base + route, timeout=15)
                status = response.status_code
                response.close()
            except requests.RequestException:
                pass
            state = verdict(status, required)
            report["probes"].append({
                "domain": domain, "endpoint": route,
                "required": required, "http": status,
                "verdict": state,
                "elapsed_ms": round(1000 * (time.monotonic() - start)),
            })
            print(f"[{state}] {domain} GET {route}: HTTP {status}")
            failed |= state == "FAIL"
    except (RuntimeError, requests.RequestException) as exc:
        # Intentionally do not print exc: network errors may include URLs
        # with credentials; never save API responses to the report.
        report["authentication"] = "FAIL"
        report["error_type"] = type(exc).__name__
        failed = True
        print("::error::Platform administrator authentication failed")
    finally:
        session.close()
        report["counts"] = {
            state: sum(x["verdict"] == state for x in report["probes"])
            for state in ("PASS", "UNAVAILABLE", "FAIL")
        }
        report["domains"] = len({x["domain"] for x in report["probes"]})
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print("Live result:", report["counts"], "domains:", report["domains"])
        if not report["host_checkout_matches"]:
            print("::warning::Host checkout SHA differs from audited SHA. "
                  "Running-service revision cannot be inferred from git alone.")
    return int(failed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--host-repo", type=Path, required=True)
    args = ap.parse_args()
    return audit(output=args.output, host_repo=args.host_repo)


if __name__ == "__main__":
    raise SystemExit(main())
