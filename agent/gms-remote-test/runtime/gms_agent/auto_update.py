"""Per-user automatic updates reuse the verified package lifecycle and exact profile."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import profile_store


UNIT = "gms-agent-update"


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def command(args):
    if args.action == "status":
        result = systemctl("show", UNIT + ".timer", "-p", "ActiveState", "-p", "UnitFileState",
                           "-p", "NextElapseUSecRealtime")
        state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        print(json.dumps({"ok": result.returncode == 0, "timer": state}, ensure_ascii=False))
        return 0 if result.returncode == 0 else 6
    if args.action == "disable":
        result = systemctl("disable", "--now", UNIT + ".timer")
        print(result.stdout or result.stderr, end="")
        return 0 if result.returncode == 0 else 6
    profile = args.profile or os.environ.get("GMS_RT_PROFILE", "")
    if not profile:
        candidates = profile_store.list_profiles()
        if len(candidates) == 1:
            profile = candidates[0]
    if not profile_store.validate_profile_name(profile):
        print("Error: auto-update enable requires an exact --profile", file=sys.stderr)
        return 2
    flat = profile_store.load_profile(profile)
    if not flat or not profile_store.controller_url(flat):
        print("Error: profile is missing or unconfigured", file=sys.stderr)
        return 2
    # Only a declarative name enters systemd; never inline profile/token contents.
    executable = Path(__file__).resolve().parents[1] / "gms-agent"
    if not executable.is_file() or systemctl("show-environment").returncode:
        print("Error: installed runtime and a running systemd user manager are required")
        return 6
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
    root.mkdir(parents=True, exist_ok=True)
    service = '''[Unit]
Description=Update GMS Agent package from the selected Controller

[Service]
Type=oneshot
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=GMS_RT_PROFILE=__GMS_PROFILE__
ExecStart=%h/.local/bin/gms-agent update --profile=__GMS_PROFILE__
ExecStartPost=%h/.local/bin/gms-agent doctor --client auto --json --profile=__GMS_PROFILE__
ExecStartPost=%h/.local/bin/gms-rt gms-rt-system-selfcheck --json --non-interactive
TimeoutStartSec=20min
UMask=0077
'''.replace("__GMS_PROFILE__", profile)
    timer = '''[Unit]
Description=Check for GMS Agent updates every hour

[Timer]
OnCalendar=hourly
RandomizedDelaySec=5min
Persistent=true
Unit=gms-agent-update.service

[Install]
WantedBy=timers.target
'''
    for name, content in ((UNIT + ".service", service), (UNIT + ".timer", timer)):
        path = root / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    for operation in (("daemon-reload",), ("enable", "--now", UNIT + ".timer")):
        result = systemctl(*operation)
        if result.returncode:
            print(result.stderr)
            return 6
    print("Automatic updates enabled (hourly). Reconnect MCP after updates; active clients are preserved.")
    print("For updates while logged out, the host administrator can enable user lingering.")
    return 0
