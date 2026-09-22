"""Architecture guard: authentication is not authorization (ADR 0006).

``test_route_authorization`` verifies that state-changing routes mention
*some* principal check. That keeps "no auth at all" out, but it cannot tell
``require_permission("build.execute")`` from a bare ``principal_owner_id``:
a valid Agent Service Token with zero scopes passes every authentication-only
marker. This gate separates the two semantics for state-changing routes:

- Strong markers verify *what* the principal may do:
  ``require_permission`` / ``require_agent_scope`` / ``require_role`` /
  ``require_elevated_admin`` / ``has_permission`` /
  ``require_human_principal`` (agent tokens fail closed server-side).
- Auth-only markers merely establish *who* the principal is:
  ``get_authenticated_user`` / ``principal_owner_id`` / ``owner_id_from_request``
  / ``_authenticate`` / ``authenticate_worker`` / ``require_resource_owner``.

A sensitive route whose evidence is only auth-family markers must be listed
in ``AUTH_ONLY_ALLOWLIST`` below with a justification. The allowlist may only
shrink; new routes must pick a strong gate (or a documented owner-ACL model
explicitly reviewed here).
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# (module, route) pairs still relying on authentication/owner-ACL semantics.
# BASELINE SNAPSHOT at the gate's introduction: every entry predates the gate
# and is migration debt, not endorsement. The list may only shrink — moving a
# route to a strong gate (require_permission / require_human_principal / ...)
# means deleting its entry here. New routes must NOT be added: pick a strong
# gate instead. Cluster worker/artifact/transfer routes authenticate via
# per-worker bearer tokens (ADR 0004); most user-surface routes below still
# owe an explicit permission or human-principal decision.
AUTH_ONLY_ALLOWLIST: set[tuple[str, str]] = {
    # public auth boundary: identity IS the credential here
    ("features/auth/api.py", "/login"),
    ("features/assistant/api.py", "/api/agent/chat"),
    ("features/assistant/api.py", "/api/agent/sessions/{session_id}/cancel"),
    ("features/auth/agent_api.py", "/approval-tokens"),
    ("features/auth/agent_api.py", "/approval-tokens/consume"),
    ("features/auth/api.py", "/elevate"),
    ("features/auth/api.py", "/elevation/reset"),
    ("features/automation/api.py", "/profiles/{profile_id}/dry-run"),
    ("features/automation/api.py", "/runs"),
    ("features/automation/api.py", "/runs/preflight"),
    ("features/cluster/api.py", "/suites/results"),
    ("features/cluster/api.py", "/workers/{worker_id}/heartbeat"),
    ("features/cluster/artifacts_api.py", "/jobs/{job_id}/artifacts/uploads"),
    ("features/cluster/artifacts_api.py", "/jobs/{job_id}/artifacts/uploads/{upload_id}/chunks/{index}"),
    ("features/cluster/artifacts_api.py", "/jobs/{job_id}/artifacts/uploads/{upload_id}/complete"),
    ("features/cluster/artifacts_api.py", "/jobs/{job_id}/artifacts/{filename}"),
    ("features/cluster/commands_api.py", "/workers/{worker_id}/commands/poll"),
    ("features/cluster/commands_api.py", "/workers/{worker_id}/commands/{command_id}/ack"),
    ("features/cluster/commands_api.py", "/workers/{worker_id}/commands/{command_id}/events"),
    ("features/cluster/job_control_api.py", "/jobs/{job_id}/cancel"),
    ("features/cluster/jobs_api.py", "/jobs/{job_id}/events"),
    ("features/cluster/transfer_ingest_api.py", "/devices/export"),
    ("features/cluster/transfer_ingest_api.py", "/transfers/{transfer_id}/apk-analysis"),
    ("features/cluster/transfer_ingest_api.py", "/transfers/{transfer_id}/chunks/{index}"),
    ("features/cluster/transfer_ingest_api.py", "/transfers/{transfer_id}/complete"),
    ("features/cluster/transfers_api.py", "/suites/export"),
    ("features/cluster/transfers_api.py", "/suites/report-copies"),
    ("features/cluster/transfers_api.py", "/suites/report-copies/{transfer_id}/import"),
    ("features/devices/adb_forward_api.py", "/api/cluster/workers/{worker_id}/adb-proxy/pair-code"),
    ("features/devices/api.py", "/api/device-groups/auto"),
    ("features/devices/bootloader_api.py", "/api/devices/bootloader-status"),
    ("features/devices/bootloader_api.py", "/api/devices/info"),
    ("features/devices/config_explorer_api.py", "/api/config-explorer/decompile"),
    ("features/devices/config_override_api.py", "/api/config-override/entries"),
    ("features/devices/config_override_api.py", "/api/config-override/entries/all"),
    ("features/devices/operations_api.py", "/api/devices/reboot"),
    ("features/devices/operations_api.py", "/api/devices/remount"),
    ("features/devices/operations_api.py", "/api/devices/shell"),
    ("features/devices/operations_api.py", "/api/devices/wifi"),
    ("features/devices/screens_api.py", "/api/devices/scrcpy"),
    ("features/devices/ui_control_api.py", "/api/devices/ui/layout"),
    ("features/devices/ui_control_api.py", "/api/devices/ui/screenshot"),
    ("features/devices/ui_control_api.py", "/api/devices/ui/tap"),
    ("features/knowledge/api.py", "/ask"),
    ("features/knowledge/api.py", "/docs"),
    ("features/knowledge/api.py", "/docs/{doc_id}"),
    ("features/knowledge/api.py", "/docs/{doc_id}/attachments"),
    ("features/knowledge/api.py", "/docs/{doc_id}/versions/{version_id}/restore"),
    ("features/knowledge/api.py", "/folders"),
    ("features/knowledge/api.py", "/nodes/{node_id}"),
    ("features/knowledge/api.py", "/nodes/{node_id}/move"),
    ("features/knowledge/api.py", "/spaces"),
    ("features/knowledge/api.py", "/upload"),
    ("features/redmine/api.py", "/reset"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/create-internal"),
    ("features/redmine/knowledge_api.py", "/mature-cases/{case_id}/approve"),
    ("features/redmine/knowledge_api.py", "/mature-cases/{case_id}/create-internal"),
    ("features/redmine/reply_api.py", "/api/redmine/reply"),
    ("features/reports/analysis_api.py", "/api/reports/analyze-log-dir"),
    ("features/reports/analysis_api.py", "/api/reports/delete"),
    ("features/reports/source_api.py", "/api/reports/analyze-url"),
    ("features/reports/source_api.py", "/api/reports/extract-redmine-attachment"),
    ("features/reports/weekly_report_api.py", "/api/reports/weekly-report/ai-summary"),
    ("features/system/tools_data_api.py", "/api/websites/save"),
    ("features/system/tools_data_api.py", "/api/websites/sync"),
    ("features/system/audit.py", "/api/security-audit/page-view"),
    ("features/system/notifications_api.py", "/api/notifications"),
    ("features/system/notifications_api.py", "/api/notifications/clear"),
    ("features/system/notifications_api.py", "/api/notifications/mark-read"),
    ("features/system/utility_tools_api.py", "/api/tools/browse"),
    ("features/test_execution/logs_api.py", "/api/test/clean"),
    ("features/test_execution/logs_api.py", "/api/test/logs/batch"),
    ("features/test_execution/logs_api.py", "/api/test/logs/save"),
    ("features/test_execution/parse_api.py", "/api/test/parse-args"),
    ("features/test_execution/suites_api.py", "/api/test/suites/apk/analyze"),
    ("features/test_execution/suites_api.py", "/api/test/suites/diagnose-target"),
    ("features/test_execution/transfers_api.py", "/api/test/suites/download-url"),
    ("features/test_execution/transfers_api.py", "/api/test/suites/extract-start"),
    ("features/users/config_api.py", "/api/sidebar-order"),
    ("features/users/device_groups.py", "/api/device-groups"),
    ("features/users/users_api.py", "/api/users/detect"),
    ("features/users/users_api.py", "/api/users/set-username"),
    ("features/users/workspace_context.py", ""),
}

# Markers that establish WHAT the principal may do.
_STRONG_MARKERS = (
    "require_permission",
    "require_agent_scope",
    "require_role",
    "require_elevated_admin",
    "require_human_principal",
    "has_permission",
)

# Markers that only establish WHO the principal is (or an owner ACL).
_AUTH_ONLY_MARKERS = (
    "require_authenticated_user",
    "get_authenticated_user",
    "principal_owner_id",
    "principal_display_name",
    "owner_id_from_request",
    "get_client_id_from_request",
    "require_resource_owner",
    "_authenticate",
    "authenticate_worker",
    "validate_websocket_request",
)

_SENSITIVE_METHODS = {"post", "put", "patch", "delete"}


def _route_paths(node: ast.AsyncFunctionDef | ast.FunctionDef):
    for decorator in node.decorator_list:
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.attr in _SENSITIVE_METHODS
        ):
            continue
        path = ""
        if isinstance(decorator, ast.Call) and decorator.args:
            first = decorator.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                path = first.value
        yield path


def _names_in(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def _module_helpers(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }


class AgentScopeBoundaryTests(unittest.TestCase):
    def test_state_changing_routes_use_authorization_not_mere_authentication(self):
        offenders: list[tuple[str, str]] = []
        for path in sorted((ROOT / "features").rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            if "/tests/" in relative or "__pycache__" in relative:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            helpers = _module_helpers(tree)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                for route in _route_paths(node):
                    if (relative, route) in AUTH_ONLY_ALLOWLIST:
                        continue
                    evidence = _names_in(node)
                    # One level of same-module helper indirection (e.g.
                    # _request_owner) so handler-local wrappers are covered.
                    for name in list(evidence):
                        helper = helpers.get(name)
                        if helper is not None:
                            evidence |= _names_in(helper)
                    # Dependencies (defaults) carry the Depends(...) markers.
                    for default in node.args.defaults + [
                        item for item in node.args.kw_defaults if item is not None
                    ]:
                        evidence |= _names_in(default)
                    has_strong = any(
                        any(n == m or n.startswith(m) for m in _STRONG_MARKERS)
                        for n in evidence
                    )
                    if has_strong:
                        continue
                    has_auth_only = any(
                        any(n == m or n.startswith(m) for m in _AUTH_ONLY_MARKERS)
                        for n in evidence
                    )
                    if has_auth_only:
                        offenders.append((relative, route or "<no path>"))
        self.assertEqual(
            offenders,
            [],
            "state-changing routes with authentication-only markers must adopt a "
            "strong gate (require_permission/require_agent_scope/require_role/"
            "require_elevated_admin/require_human_principal) or join the shrinking "
            f"AUTH_ONLY_ALLOWLIST with a justification: {offenders}",
        )

    def test_auth_only_allowlist_entries_still_exist(self):
        existing = set()
        for path in sorted((ROOT / "features").rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            if "/tests/" in relative or "__pycache__" in relative:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    existing.update(
                        (relative, route) for route in _route_paths(node)
                    )
        stale = sorted(
            entry for entry in AUTH_ONLY_ALLOWLIST if entry not in existing
        )
        self.assertEqual(stale, [], f"stale allowlist entries: {stale}")

    def test_auth_only_allowlist_never_grows(self):
        self.assertLessEqual(
            len(AUTH_ONLY_ALLOWLIST),
            83,
            "the auth-only baseline must not grow; migrate routes to strong gates",
        )


if __name__ == "__main__":
    unittest.main()
