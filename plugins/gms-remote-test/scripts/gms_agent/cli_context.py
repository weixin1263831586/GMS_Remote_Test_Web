"""Local command discovery and explicit human profile selection for the CLI."""

from __future__ import annotations

import os
import shlex
import sys

from . import profile_store


LOCAL_COMMANDS = {
    "gms-rt-system-help", "gms-rt-system-version", "gms-rt-system-capabilities",
    "gms-rt-system-command-describe", "gms-rt-system-commands", "gms-rt-system-update",
}
GLOBAL_FLAGS = {"--json", "--quiet", "--no-color", "--non-interactive", "--yes", "--insecure"}
GLOBAL_VALUES = {"--timeout", "--server", "--ca-cert"}
HUMAN_COMMANDS = {
    "gms-rt-auth-login", "gms-rt-auth-logout", "gms-rt-auth-elevate",
    "gms-rt-auth-elevation-reset", "gms-rt-approval-create",
}


def local_invocation(argv):
    if not argv or argv[0] in LOCAL_COMMANDS:
        return True
    remaining = []
    skip = False
    for value in argv[1:]:
        if skip:
            skip = False
        elif value in GLOBAL_VALUES:
            skip = True
        elif value in GLOBAL_FLAGS or any(value.startswith(flag + "=") for flag in GLOBAL_VALUES):
            continue
        else:
            remaining.append(value)
    return remaining in (["--help"], ["-h"])


def choose_profile(server="", *, human=False):
    """Ask for an exact choice; never default to the first client or identity."""
    candidates = []
    transports = set()
    for name in profile_store.list_profiles():
        flat = profile_store.load_profile(name)
        url = profile_store.controller_url(flat)
        if url and (not server or url == server.rstrip("/")):
            transport = (url, profile_store.ca_cert(flat), flat.get("insecure", ""))
            if human and transport in transports:
                continue
            transports.add(transport)
            candidates.append((name, flat.get("client", ""), url))
    if not candidates:
        raise ValueError("没有匹配的 Agent profile，请先运行 gms-agent install。")
    print("请选择人工操作使用的 Controller/TLS 配置：" if human else "请选择本次使用的 Agent 身份：",
          file=sys.stderr)
    for index, (name, client, url) in enumerate(candidates, 1):
        label = f"{url}  |  TLS 配置: {name}" if human else f"{name}  |  {client}  |  {url}"
        print(f"  {index}. {label}", file=sys.stderr)
    print("选择编号（q 取消）：", end="", file=sys.stderr, flush=True)
    try:
        answer = sys.stdin.readline().strip()
    except KeyboardInterrupt:
        raise ValueError("已取消选择 Agent profile。") from None
    if len(answer) > 6 or not answer.isdigit() or not 1 <= int(answer) <= len(candidates):
        raise ValueError("未选择有效的 Agent profile，命令未执行。")
    return candidates[int(answer) - 1][0]


def interactive_allowed():
    return (sys.stdin.isatty() and sys.stderr.isatty()
            and os.environ.get("GMS_AGENT_PROCESS") != "1"
            and os.environ.get("GMS_RT_NON_INTERACTIVE") != "1"
            and os.environ.get("GMS_RT_OUTPUT") != "json")


def resolve_cli_context(profile, server, argv=None):
    # Library sourcing retains normal context resolution and never prompts.
    if argv is not None and local_invocation(argv):
        return {"mode": "local", "profile": "", "server": "", "ca_cert": "",
                "token_file": "", "insecure": "", "error": ""}
    unattended = (
        argv is None or "--json" in argv or "--non-interactive" in argv
        or os.environ.get("GMS_RT_OUTPUT") == "json"
        or os.environ.get("GMS_RT_NON_INTERACTIVE") == "1"
    )
    human = bool(not unattended and argv[0] in HUMAN_COMMANDS and interactive_allowed())
    context = (profile_store.resolve_direct_cli_context(profile, server, human_session=True)
               if human else profile_store.resolve_direct_cli_context(profile, server))
    if context["mode"] == "error" and not profile:
        if not unattended and interactive_allowed():
            try:
                selected = choose_profile(server, human=True) if human else choose_profile(server)
            except ValueError as error:
                context["error"] = str(error)
            else:
                context = (profile_store.resolve_direct_cli_context(selected, server, human_session=True)
                           if human else profile_store.resolve_direct_cli_context(selected, server))
                if context["mode"] != "error" and not human:
                    print(f"本次使用 {selected}；当前终端复用可执行：export GMS_RT_PROFILE={shlex.quote(selected)}",
                          file=sys.stderr)
        else:
            context["error"] += '\n当前终端只需选择一次：eval "$(gms-agent profile env)"'
    return context


def command_profile_env(args):
    """Emit only a quoted export so a human shell can explicitly retain its choice."""
    name = args.name or os.environ.get("GMS_RT_PROFILE", "")
    if not name:
        if not interactive_allowed():
            print("Error: 请指定 profile 名，或在交互终端运行此命令。运行 gms-agent profile list 查看名称。",
                  file=sys.stderr)
            return 2
        try:
            name = choose_profile()
        except ValueError as error:
            print("Error: " + str(error), file=sys.stderr)
            return 2
    context = profile_store.resolve_direct_cli_context(name)
    if context["mode"] == "error":
        print("Error: " + context["error"], file=sys.stderr)
        return 2
    print("export GMS_RT_PROFILE=" + shlex.quote(name))
    return 0
