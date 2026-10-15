"""Typed deployment requests never mint approvals; DB reads never accept raw SQL."""

from __future__ import annotations


def schema(name, description, properties, required):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required, "additionalProperties": False}}


TARGET = {"worker_id": {"type": "string", "description": "Exact owning Worker from cluster inventory."},
          "device": {"type": "string", "description": "Exact device serial."}}
SCHEMAS = [
    schema("gms_rt_apk_deploy",
           "Upload a local APK and request one human approval for transfer + pm install. "
           "Returns deployment.id and approval_url; give the link to the user, then poll "
           "gms_rt_apk_deployment_status. Never ask for a copied approval token. "
           "With an approved debug_session_id, installs immediately within its package/signature/target scope. "
           "CLI: gms-rt-apk-deploy <worker_id> <device> <apk_file> [--no-replace] [--debug-session ID].",
           {**TARGET, "apk_file": {"type": "string"}, "replace_existing": {"type": "boolean", "default": True},
            "debug_session_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}},
           ["worker_id", "device", "apk_file"]),
    schema("gms_rt_apk_deployment_status", "Read one owner-scoped deployment's approval/install status. "
           "CLI: gms-rt-apk-deployment-status <request_id>.",
           {"request_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}}, ["request_id"]),
    schema("gms_rt_devices_db_query", "Read user 0 app SQLite rows with -readonly, using run-as "
           "or the device shell's existing access (no adb root/su). "
           "Omit table to list tables; specify columns and pagination to check coordinates. "
           "Requires devices.database.read plus an active debug session with database_read permission for this app. No raw SQL, shell, root, or approval token. Requires device sqlite3. "
           "CLI: gms-rt-devices-db-query <worker_id> <device> <package> <database> [table] "
           "--debug-session ID [--columns a,b] [--limit N] [--offset N].",
           {**TARGET, "package": {"type": "string"}, "database": {"type": "string"},
            "debug_session_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
            "table": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            "offset": {"type": "integer", "minimum": 0, "maximum": 1000000, "default": 0}},
           ["worker_id", "device", "package", "database", "debug_session_id"]),
]


def handlers(run_cli):
    def deploy(args):
        argv = [str(args.get(key) or "") for key in ("worker_id", "device", "apk_file")]
        if not all(argv):
            return "worker_id, device and apk_file are required", True
        if args.get("replace_existing") is False:
            argv.append("--no-replace")
        if args.get("debug_session_id"):
            argv.extend(["--debug-session", str(args["debug_session_id"])])
        return run_cli("gms-rt-apk-deploy", argv, timeout=300)

    def status(args):
        return run_cli("gms-rt-apk-deployment-status", [str(args.get("request_id") or "")], timeout=30)

    def query(args):
        argv = [str(args.get(key) or "") for key in ("worker_id", "device", "package", "database")]
        if not all(argv):
            return "worker_id, device, package and database are required", True
        if not args.get("debug_session_id"):
            return "debug_session_id is required", True
        if args.get("table"):
            argv.append(str(args["table"]))
        argv.extend(["--debug-session", str(args["debug_session_id"])])
        if args.get("columns"):
            if not isinstance(args["columns"], list) or not all(isinstance(c, str) for c in args["columns"]):
                return "columns must be a string array", True
            argv.extend(["--columns", ",".join(args["columns"])])
        for key in ("limit", "offset"):
            if key in args:
                argv.extend(["--" + key, str(args[key])])
        return run_cli("gms-rt-devices-db-query", argv, timeout=90)

    return {"gms_rt_apk_deploy": deploy, "gms_rt_apk_deployment_status": status,
            "gms_rt_devices_db_query": query}
