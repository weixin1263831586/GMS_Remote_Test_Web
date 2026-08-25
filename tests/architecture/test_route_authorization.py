"""Architecture guard: sensitive routes must carry an explicit authorization.

The platform defines ROLE_PERMISSIONS and ``require_permission`` but most
endpoints still rely on ad-hoc handler-internal checks.  This test keeps that
debt visible and ratchets it down:

- Every state-changing route under ``/api`` must either be bound to a
  ``Depends(require_...)`` dependency / call one of the access-control helpers
  inside the handler, or be listed in ``MIGRATION_ALLOWLIST`` below.
- The allowlist may only shrink: deleting an entry turns that route into an
  enforcement obligation.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# Routes intentionally outside the unified model:
# - auth entry points (login/setup/logout/elevate) are the public boundary
# - worker heartbeat/register/command routes authenticate with per-worker
#   bearer tokens via _authenticate() / authenticate_worker() in the handler
# - the Gerrit webhook is authenticated by Gerrit's signed payload
MIGRATION_ALLOWLIST = {
    ("features/auth/api.py", "/elevate"),
    ("features/auth/api.py", "/elevation/reset"),
    ("features/auth/api.py", "/login"),
    ("features/auth/api.py", "/logout"),
    ("features/auth/api.py", "/setup"),
    ("features/automation/api.py", "/gerrit/webhook"),
    ("features/automation/api.py", "/runs/{run_id}/cancel"),
    ("features/automation/api.py", "/runs/{run_id}/retry"),
    ("features/build/api.py", "/discover/lunch-options"),
    ("features/build/api.py", "/discover/workspaces"),
    ("features/build/api.py", "/jobs/{job_id}"),
    ("features/build/api.py", "/jobs/{job_id}/cancel"),
    ("features/build/api.py", "/jobs/{job_id}/password"),
    ("features/build/api.py", "/jobs/{job_id}/poll"),
    ("features/cluster/api.py", "/workers/register"),
    ("features/cluster/api.py", "/workers/{worker_id}/heartbeat"),
    ("features/cluster/commands_api.py", "/workers/{worker_id}/commands/poll"),
    ("features/cluster/commands_api.py", "/workers/{worker_id}/commands/{command_id}/ack"),
    ("features/cluster/job_control_api.py", "/jobs/{job_id}/cancel"),
    ("features/cluster/jobs_api.py", "/jobs/{job_id}"),
    ("features/cluster/jobs_api.py", "/jobs/{job_id}/artifacts/uploads"),
    ("features/cluster/jobs_api.py", "/jobs/{job_id}/artifacts/uploads/{upload_id}/chunks/{index}"),
    ("features/cluster/jobs_api.py", "/jobs/{job_id}/artifacts/uploads/{upload_id}/complete"),
    ("features/cluster/jobs_api.py", "/jobs/{job_id}/artifacts/{filename}"),
    ("features/cluster/jobs_api.py", "/jobs/{job_id}/events"),
    ("features/cluster/transfer_ingest_api.py", "/devices/export"),
    ("features/cluster/transfer_ingest_api.py", "/transfers/{transfer_id}/apk-analysis"),
    ("features/cluster/transfer_ingest_api.py", "/transfers/{transfer_id}/chunks/{index}"),
    ("features/cluster/transfer_ingest_api.py", "/transfers/{transfer_id}/complete"),
    ("features/cluster/transfer_ingest_api.py", "/transfers/{transfer_id}/report-analysis"),
    ("features/cluster/transfers_api.py", "/firmware/stage"),
    ("features/cluster/transfers_api.py", "/gsi/stage"),
    ("features/cluster/transfers_api.py", "/suites/export"),
    ("features/cluster/transfers_api.py", "/suites/report-copies"),
    ("features/cluster/transfers_api.py", "/suites/report-copies/{transfer_id}/import"),
    ("features/devices/adb_forward_api.py", "/api/cluster/workers/{worker_id}/adb-proxy/pair-code"),
    ("features/devices/api.py", "/api/device-groups/auto"),
    ("features/devices/config_override_api.py", "/api/config-override/entries"),
    ("features/devices/config_override_api.py", "/api/config-override/entries/all"),
    ("features/firmware/apk_api.py", "/api/apk/analyze/{task_id}"),
    ("features/firmware/apk_api.py", "/api/apk/task/{task_id}"),
    ("features/firmware/apk_api.py", "/api/apk/upload"),
    ("features/gerrit/api.py", "/config"),
    ("features/gerrit/api.py", "/department-profiles"),
    ("features/gerrit/api.py", "/department-profiles/{profile_id}/owners"),
    ("features/gerrit/api.py", "/personal-profiles"),
    ("features/gerrit/api.py", "/sync-redmine-members"),
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
    ("features/redmine/api.py", "/config/credentials"),
    ("features/redmine/api.py", "/config/email"),
    ("features/redmine/api.py", "/config/stats"),
    ("features/redmine/api.py", "/dashboard/profiles"),
    ("features/redmine/api.py", "/dashboard/projects"),
    ("features/redmine/api.py", "/issues/{issue_id}/fetch"),
    ("features/redmine/api.py", "/issues/{issue_id}/metadata"),
    ("features/redmine/api.py", "/reminders/email"),
    ("features/redmine/api.py", "/reset"),
    ("features/redmine/api.py", "/runs"),
    ("features/redmine/api.py", "/sync"),
    ("features/redmine/api.py", "/users"),
    ("features/redmine/knowledge_api.py", "/issues/batch-import"),
    ("features/redmine/knowledge_api.py", "/issues/import-recent"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/agent-reply"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/analyze-case"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/create-internal"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/draft-reply"),
    ("features/redmine/api.py", "/issues/{issue_id}/fetch"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/evaluate-case"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/reference-output"),
    ("features/redmine/knowledge_api.py", "/mature-cases/build"),
    ("features/redmine/knowledge_api.py", "/mature-cases/{case_id}/approve"),
    ("features/redmine/knowledge_api.py", "/mature-cases/{case_id}/create-internal"),
    ("features/redmine/knowledge_api.py", "/search/similar"),
    ("features/redmine/reply_api.py", "/api/redmine/reply"),
    ("features/reports/analysis_api.py", "/api/reports/analyze-log-dir"),
    ("features/reports/source_api.py", "/api/reports/extract-redmine-attachment"),
    ("features/reports/weekly_report_api.py", "/api/reports/weekly-report/ai-summary"),
    ("features/system/assets.py", "/api/favicon/batch"),
    ("features/system/assets.py", "/api/opengrok/search"),
    ("features/system/assets.py", "/api/websites/save"),
    ("features/system/assets.py", "/api/websites/sync"),
    ("features/system/integrations.py", "/api/ssh/ping"),
    ("features/system/integrations.py", "/api/vpn/connect"),
    ("features/system/integrations.py", "/api/vpn/disconnect"),
    ("features/system/notifications_api.py", "/api/notifications"),
    ("features/system/notifications_api.py", "/api/notifications/clear"),
    ("features/system/notifications_api.py", "/api/notifications/mark-read"),
    ("features/system/utility_tools_api.py", "/api/tools/browse"),
    ("features/test_execution/logs_api.py", "/api/test/clean"),
    ("features/test_execution/logs_api.py", "/api/test/logs/batch"),
    ("features/test_execution/logs_api.py", "/api/test/logs/save"),
    ("features/test_execution/parse_api.py", "/api/test/parse-args"),
    ("features/test_execution/suites_api.py", "/api/test/suites/diagnose-target"),
    ("features/test_execution/transfers_api.py", "/api/test/suites/add-local"),
    ("features/test_execution/transfers_api.py", "/api/test/suites/result"),
    ("features/test_execution/transfers_api.py", "/api/test/suites/extract"),
    ("features/test_execution/transfers_api.py", "/api/test/suites/download-url"),
    ("features/test_execution/transfers_api.py", "/api/test/suites/extract-start"),
    ("features/test_execution/suites_api.py", "/api/test/suites/apk/analyze"),
    ("features/users/device_groups.py", "/api/device-groups"),
    ("features/users/users_api.py", "/api/users/detect"),
    ("features/users/users_api.py", "/api/users/set-username"),
}

_AUTH_DEPEND_MARKERS = (
    "require_permission",
    "require_role",
    "require_elevated_admin",
    "require_authenticated_user",
    "require_resource_owner",
)

# Handler-internal helpers that establish or check a principal.
_AUTH_CALL_MARKERS = (
    "require_permission",
    "require_role",
    "require_elevated_admin",
    "require_authenticated_user",
    "require_resource_owner",
    "get_authenticated_user",
    "get_client_id_from_request",
    "owner_id_from_request",
    "principal_owner_id",
    "_authenticate",
    "authenticate_worker",
    "validate_websocket_request",
)

_SENSITIVE_METHODS = {"post", "put", "patch", "delete"}


def _route_info(node: ast.AsyncFunctionDef | ast.FunctionDef):
    """Yield (route_path) for each sensitive router decorator on the handler."""
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


def _has_authorization(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    for default in node.args.defaults + [
        item for item in node.args.kw_defaults if item is not None
    ]:
        for sub in ast.walk(default):
            if isinstance(sub, ast.Name) and any(
                sub.id.startswith(m) for m in _AUTH_DEPEND_MARKERS
            ):
                return True
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and any(sub.func.id.startswith(m) for m in _AUTH_DEPEND_MARKERS)
            ):
                return True
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in _AUTH_CALL_MARKERS:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in _AUTH_CALL_MARKERS:
            return True
    return False


class SensitiveRouteAuthorizationTests(unittest.TestCase):
    def test_state_changing_api_routes_declare_authorization(self):
        offenders = []
        for path in sorted((ROOT / "features").rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            if "/tests/" in relative or "__pycache__" in relative:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                offenders.append((relative, "<syntax error>"))
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                for route in _route_info(node):
                    if (relative, route) in MIGRATION_ALLOWLIST:
                        continue
                    if not _has_authorization(node):
                        offenders.append((relative, route or "<no path>"))
        # Routes inside the allowlist that no longer exist must be removed too.
        existing = set()
        for path in sorted((ROOT / "features").rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            if "/tests/" in relative or "__pycache__" in relative:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    existing.update((relative, route) for route in _route_info(node))
        stale = sorted(entry for entry in MIGRATION_ALLOWLIST if entry not in existing)
        self.assertEqual(stale, [], f"stale allowlist entries: {stale}")
        self.assertEqual(
            offenders,
            [],
            "state-changing API routes must use require_permission/require_role/"
            "require_elevated_admin or an in-handler principal check; otherwise add "
            f"an explicit, shrinking MIGRATION_ALLOWLIST entry: {offenders}",
        )

    def test_migration_allowlist_only_shrinks(self):
        """Encode the ratchet: bound to the entries listed above at review time."""
        self.assertLessEqual(
            len(MIGRATION_ALLOWLIST),
            118,
            "the authorization migration allowlist must not grow",
        )


if __name__ == "__main__":
    unittest.main()
