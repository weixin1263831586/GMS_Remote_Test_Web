from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ("runtime" if (ROOT / "runtime").is_dir() else "scripts")
spec = importlib.util.spec_from_file_location("deployment_tools_test", RUNTIME / "mcp_deployment_tools.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_deployment_requests_exact_target_and_no_copied_token():
    run = Mock(return_value=("{}", False))
    handlers = module.handlers(run)
    handlers["gms_rt_apk_deploy"]({"worker_id": "worker", "device": "SERIAL", "apk_file": "/tmp/a.apk"})
    run.assert_called_once_with("gms-rt-apk-deploy", ["worker", "SERIAL", "/tmp/a.apk"], timeout=300)
    schema = next(s for s in module.SCHEMAS if s["name"] == "gms_rt_apk_deploy")
    assert "approval_token" not in schema["inputSchema"]["properties"]
    assert "approval_url" in schema["description"]


def test_database_handler_uses_typed_columns_and_pagination():
    run = Mock(return_value=("{}", False))
    module.handlers(run)["gms_rt_devices_db_query"]({
        "worker_id": "worker", "device": "SERIAL", "package": "com.example.app", "database": "launcher.db",
        "table": "favorites", "columns": ["cellX", "cellY"], "limit": 20, "offset": 40,
    })
    run.assert_called_once_with("gms-rt-devices-db-query", [
        "worker", "SERIAL", "com.example.app", "launcher.db", "favorites",
        "--columns", "cellX,cellY", "--limit", "20", "--offset", "40",
    ], timeout=90)


def test_missing_targets_never_execute_cli():
    run = Mock()
    result = module.handlers(run)["gms_rt_apk_deploy"]({"apk_file": "/tmp/a.apk"})
    assert result[1] is True
    run.assert_not_called()
