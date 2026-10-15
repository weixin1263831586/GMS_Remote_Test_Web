"""Typed debug operations use a human-approved grant, never arbitrary shell."""

from __future__ import annotations


def schema(name, description, properties, required):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required, "additionalProperties": False}}


SESSION = {"session_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}}
SCHEMAS = [
    schema("gms_rt_debug_session_request",
           "Request one human-approved, finite debug session bound to this Agent, exact Worker/device, "
           "APK package and verified signing certificates. Return approval_url to the human; never copy tokens. "
           "Approval starts the first installation if install is requested. Request database_read explicitly for app database access. Duration defaults to 60 min, max 120. "
           "CLI: gms-rt-debug-session-request <worker_id> <device> <package> <apk_file> "
           "[--minutes N] [--operations install,launch,stop,tap,swipe,text,keyevent].",
           {"worker_id": {"type": "string"}, "device": {"type": "string"}, "package": {"type": "string"},
            "apk_file": {"type": "string"}, "duration_minutes": {"type": "integer", "minimum": 15, "maximum": 120, "default": 60},
            "operations": {"type": "array", "minItems": 1, "uniqueItems": True,
                           "items": {"type": "string", "enum": ["install", "launch", "stop", "tap", "swipe", "text", "keyevent", "database_read"]}}},
           ["worker_id", "device", "package", "apk_file"]),
    schema("gms_rt_debug_session_status", "Read grant status and recent operations; poll until active and not busy. "
           "CLI: gms-rt-debug-session-status <session_id>.", SESSION, ["session_id"]),
    schema("gms_rt_debug_session_revoke", "Revoke this Agent's exact debug session when requested by the user or "
           "when debugging is complete. Stops subsequent operations; does not undo commands already executing. "
           "CLI: gms-rt-debug-session-revoke <session_id>.", SESSION, ["session_id"]),
    schema("gms_rt_debug_action", "Run a bounded operation inside an approved debug session. No shell/SQL. "
           "launch/stop target only the approved app (user 0); tap/swipe/text/keyevent require that app's foreground window. "
           "Text supports ASCII letters/digits/spaces and @._%+,:/-. System dialogs require human handling. "
           "Use gms_rt_apk_deploy with debug_session_id for repeated installs. "
           "CLI: gms-rt-debug-action <session_id> <action> [--activity CLASS] [--x N] [--y N] "
           "[--end-x N] [--end-y N] [--duration-ms N] [--text VALUE] [--key KEY].",
           {**SESSION, "action": {"type": "string", "enum": ["launch", "stop", "tap", "swipe", "text", "keyevent"]},
            "activity": {"type": "string"},
            **{k: {"type": "integer", "minimum": 0, "maximum": 10000} for k in ("x", "y", "end_x", "end_y")},
            "duration_ms": {"type": "integer", "minimum": 1, "maximum": 2000},
            "text": {"type": "string", "minLength": 1, "maxLength": 200},
            "key": {"type": "string", "enum": ["BACK", "ENTER", "DEL", "TAB", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER"]}},
           ["session_id", "action"]),
]


def handlers(run_cli):
    def request(args):
        argv = [str(args.get(k) or "") for k in ("worker_id", "device", "package", "apk_file")]
        if not all(argv):
            return "worker_id, device, package and apk_file are required", True
        if "duration_minutes" in args:
            argv.extend(["--minutes", str(args["duration_minutes"])])
        if "operations" in args:
            operations = args["operations"]
            if not isinstance(operations, list) or not operations or not all(isinstance(op, str) for op in operations):
                return "operations must be a nonempty string array", True
            argv.extend(["--operations", ",".join(operations)])
        return run_cli("gms-rt-debug-session-request", argv, timeout=300)

    def status(args):
        return run_cli("gms-rt-debug-session-status", [str(args.get("session_id") or "")], timeout=30)

    def revoke(args):
        return run_cli("gms-rt-debug-session-revoke", [str(args.get("session_id") or "")], timeout=30)

    def action(args):
        argv = [str(args.get("session_id") or ""), str(args.get("action") or "")]
        if not all(argv):
            return "session_id and action are required", True
        for key in ("activity", "x", "y", "end_x", "end_y", "duration_ms", "text", "key"):
            if key in args:
                argv.extend(["--" + key.replace("_", "-"), str(args[key])])
        return run_cli("gms-rt-debug-action", argv, timeout=90)

    return {"gms_rt_debug_session_request": request, "gms_rt_debug_session_status": status,
            "gms_rt_debug_session_revoke": revoke, "gms_rt_debug_action": action}
