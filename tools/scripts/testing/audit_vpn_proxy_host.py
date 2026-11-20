"""Read-only on-host audit of vpn-proxy installed runtime and GMS integration."""
import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests


UNITS = {"proxy": "rk-vpn-proxy.service",
         "firewall": "rk-vpn-proxy-firewall.service",
         "stats": "rk-vpn-proxy-stats.service",
         "installer": "rk-vpn-proxy-installer.service"}


def cmd(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=6)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(65536), b""):
            h.update(part)
    return h.hexdigest()


def audit(args):
    report = {"schema": 1, "time_utc": datetime.now(timezone.utc).isoformat(),
              "mode": "read only; no service restart or client ACL changes",
              "checks": []}

    def add(name, ok, message):
        state = "PASS" if ok is True else ("WARN" if ok is None else "FAIL")
        report["checks"].append({"name": name, "state": state, "note": message})
        print(f"[{state}] {name}: {message}", flush=True)

    add("runner", os.getenv("RUNNER_NAME") == "ats-041055-64g"
        and cmd("id", "-un")[1] == "hcq", "dedicated on-prem runner identity")

    rc, branch = cmd("git", "-C", str(args.vpn_root), "branch", "--show-current")
    add("vpn_branch", rc == 0 and branch == "master", "local branch master" if branch == "master" else "local branch not master")
    rc, head = cmd("git", "-C", str(args.vpn_root), "rev-parse", "HEAD")
    report["local_vpn_sha"] = head if rc == 0 else ""
    report["reviewed_vpn_sha"] = args.master_sha
    add("vpn_revision", rc == 0 and head == args.master_sha,
        "local SHA matches reviewed vpn-proxy/master revision" if head == args.master_sha else "local checkout differs from reviewed vpn-proxy/master revision")
    rc, dirty = cmd("git", "-C", str(args.vpn_root), "status", "--porcelain")
    add("vpn_worktree", True if rc == 0 and not dirty else None,
        "clean" if rc == 0 and not dirty else "local uncommitted edits or status unavailable")

    for label, local, installed in [
        ("client_control_deployed", args.vpn_root / "scripts/client_control.py",
         Path("/usr/local/libexec/rk-vpn-proxy-client-control")),
        ("stats_collector_deployed", args.vpn_root / "scripts/client_stats.py",
         Path("/usr/local/libexec/rk-vpn-proxy-client-stats")),
    ]:
        try:
            mode = installed.lstat()
            safe = stat.S_ISREG(mode.st_mode) and mode.st_uid == 0 and not(mode.st_mode & 0o022)
            same = digest(local) == digest(installed)
            add(label, safe and same, "root-owned installed copy matches local source" if safe and same else "missing, unsafe or older installed copy")
        except OSError:
            add(label, False, "local source or installed copy is unavailable")

    config = {}
    config_path = Path("/etc/rk-vpn-proxy/config.json")
    try:
        if config_path.is_symlink() or config_path.stat().st_size > 65536:
            raise ValueError("invalid config file")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("invalid config")
        add("installed_config", True, "deployed config readable")
    except (OSError, ValueError):
        add("installed_config", None, "not readable by Runner; GMS API checks it separately")
        config = {}

    for name, unit in UNITS.items():
        _, response = cmd("systemctl", "is-active", unit)
        if name == "installer" and config.get("installer_enabled") is False:
            add("service_" + name, None,
                f"{unit}: optional installer explicitly disabled")
        else:
            add("service_" + name, response == "active", f"{unit}: {response or 'unknown'}")

    port = config.get("stats_port", 17898)
    if not isinstance(port, int) or not (1 <= port <= 65535):
        port = 17898
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(f"http://127.0.0.1:{port}/api/stats", timeout=4)
            payload = response.json() if response.status_code == 200 else {}
        sources = payload.get("sources", {}) if isinstance(payload, dict) else {}
        fresh = -5 <= time.time() - float(payload.get("time", 0)) <= 45
        healthy = (response.status_code == 200 and not payload.get("failed")
                   and not payload.get("error") and fresh
                   and sources.get("connections") is True
                   and sources.get("access_log") is True
                   and isinstance(payload.get("connected"), list)
                   and isinstance(payload.get("recent"), list))
        add("stats_api", healthy, "fresh stats with healthy socket/log sources" if healthy else "not ready or stale; no clients assumed")
        if healthy:
            report["client_counts"] = {
                "connected": len(payload["connected"]), "recent": len(payload["recent"])
            }
    except (requests.RequestException, ValueError, TypeError):
        add("stats_api", False, "loopback statistics endpoint unavailable")

    _, gms_sha = cmd("git", "-C", str(args.gms_root), "rev-parse", "HEAD")
    report["gms_host_sha"] = gms_sha
    add("gms_source", (args.gms_root / "features/system/vpn_proxy_api.py").is_file()
        and (args.gms_root / "web/static/js/shell/vpn-proxy-panel.js").is_file(),
        "management API/UI source present in host checkout")
    add("gms_revision", True if gms_sha == os.getenv("GITHUB_SHA") else None,
        "matches audit SHA" if gms_sha == os.getenv("GITHUB_SHA") else "GMS checkout does not match audit SHA; running version not proven")

    base = os.getenv("GMS_SOAK_BASE_URL", "").rstrip("/")
    if urlsplit(base).hostname not in {"127.0.0.1", "localhost"}:
        add("gms_status_api", False, "refusing to send credentials to non-loopback URL")
    else:
        session = requests.Session()
        session.trust_env = False
        # trust_env=False also disables REQUESTS_CA_BUNDLE auto-discovery.
        # Explicitly pin the on-host Controller CA while still bypassing any
        # inherited HTTP_PROXY/HTTPS_PROXY that could exfiltrate credentials.
        session.verify = os.getenv("REQUESTS_CA_BUNDLE") or True
        try:
            token = os.getenv("GMS_SOAK_SESSION_TOKEN")
            if token:
                session.cookies.set("gms_session", token)
            else:
                user, pwd = os.getenv("GMS_SOAK_USERNAME"), os.getenv("GMS_SOAK_PASSWORD")
                if not user or not pwd:
                    raise ValueError("missing credentials")
                for path in ("/api/auth/login", "/api/auth/elevate"):
                    r = session.post(base + path,
                                     json={"username": user, "password": pwd}, timeout=15)
                    if r.status_code != 200:
                        raise ValueError("login refused")
            resp = session.get(base + "/api/tools/vpn-proxy/status", timeout=10)
            if resp.status_code != 200:
                add("gms_status_api", False, f"HTTP {resp.status_code} (authenticated)")
            else:
                body = resp.json()
                d = body.get("data") if isinstance(body, dict) else None
                if not isinstance(d, dict) or not body.get("success"):
                    add("gms_status_api", False, "invalid management response")
                else:
                    present = d.get("installed") is True
                    service = d.get("services", {}).get("proxy") == "active"
                    access = d.get("client_access", {}).get("available") is True
                    stats_ok = isinstance(d.get("clients"), dict) and not d.get("stats_error")
                    all_ok = present and service and access and stats_ok
                    add("gms_status_api", all_ok,
                        f"installed={present} proxy_active={service} client_control={access} stats={stats_ok}")
                    report["gms_live"] = {
                        "installed": present, "proxy_active": service,
                        "client_control": access, "stats_healthy": stats_ok
                    }
        except (requests.RequestException, ValueError, TypeError, KeyError):
            add("gms_status_api", False, "authenticated GET unsuccessful; credentials not logged")
        finally:
            session.close()

    report["counts"] = {status: sum(x["state"] == status for x in report["checks"])
                        for status in ("PASS", "WARN", "FAIL")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Proxy on-host checks:", report["counts"])
    return 1 if report["counts"]["FAIL"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--vpn-root", type=Path, required=True)
    parser.add_argument("--gms-root", type=Path, required=True)
    parser.add_argument("--master-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    sys.exit(audit(parser.parse_args()))
