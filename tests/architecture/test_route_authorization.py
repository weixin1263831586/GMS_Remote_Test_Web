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
    ("features/cluster/job_control_api.py", "/jobs/{job_id}/cancel"),
    ("features/cluster/transfer_ingest_api.py", "/transfers/{transfer_id}/report-analysis"),
    ("features/devices/api.py", "/api/device-groups/auto"),
    ("features/gerrit/api.py", "/config"),
    ("features/gerrit/api.py", "/department-profiles"),
    ("features/gerrit/api.py", "/department-profiles/{profile_id}/owners"),
    ("features/gerrit/api.py", "/personal-profiles"),
    ("features/gerrit/api.py", "/sync-redmine-members"),
    ("features/redmine/api.py", "/config/credentials"),
    ("features/redmine/api.py", "/config/email"),
    ("features/redmine/api.py", "/config/stats"),
    ("features/redmine/api.py", "/dashboard/profiles"),
    ("features/redmine/api.py", "/dashboard/projects"),
    ("features/redmine/api.py", "/issues/{issue_id}/fetch"),
    ("features/redmine/api.py", "/issues/{issue_id}/metadata"),
    ("features/redmine/api.py", "/reminders/email"),
    ("features/redmine/api.py", "/runs"),
    ("features/redmine/api.py", "/sync"),
    ("features/redmine/api.py", "/users"),
    ("features/redmine/knowledge_api.py", "/issues/batch-import"),
    ("features/redmine/knowledge_api.py", "/issues/import-recent"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/agent-reply"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/analyze-case"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/draft-reply"),
    ("features/redmine/api.py", "/issues/{issue_id}/fetch"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/evaluate-case"),
    ("features/redmine/knowledge_api.py", "/issues/{issue_id}/reference-output"),
    ("features/redmine/knowledge_api.py", "/mature-cases/build"),
    ("features/redmine/knowledge_api.py", "/search/similar"),
    ("features/redmine/reply_api.py", "/api/redmine/reply"),
    ("features/reports/analysis_api.py", "/api/reports/analyze-log-dir"),
    ("features/reports/weekly_report_api.py", "/api/reports/weekly-report/ai-summary"),
    ("features/system/assets.py", "/api/favicon/batch"),
    ("features/system/assets.py", "/api/opengrok/search"),
    ("features/system/integrations.py", "/api/ssh/ping"),
    ("features/system/integrations.py", "/api/vpn/connect"),
    ("features/system/integrations.py", "/api/vpn/disconnect"),
    ("features/system/utility_tools_api.py", "/api/tools/browse"),
    ("features/test_execution/parse_api.py", "/api/test/parse-args"),
    ("features/test_execution/suites_api.py", "/api/test/suites/diagnose-target"),
    ("features/test_execution/transfers_api.py", "/api/test/suites/add-local"),
    ("features/test_execution/transfers_api.py", "/api/test/suites/result"),
    ("features/test_execution/transfers_api.py", "/api/test/suites/extract"),
    ("features/users/users_api.py", "/api/users/detect"),
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
    return False


def _module_helper_bodies(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    """Map top-level helper function name -> node for same-module indirection."""
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }


def _mentions_marker(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """Direct mention of an auth call marker inside the function body."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in _AUTH_CALL_MARKERS:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in _AUTH_CALL_MARKERS:
            return True
    # Dependencies like Depends(require_...) also count.
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
    return False


def _handler_authorized(
    node: ast.AsyncFunctionDef | ast.FunctionDef,
    helpers: dict[str, ast.AsyncFunctionDef | ast.FunctionDef],
) -> bool:
    """Authorization check with one level of same-module helper indirection.

    Handlers commonly wrap principal resolution in a module-local helper
    (e.g. ``_user()`` -> ``get_client_id_from_request``). A helper that
    transitively resolves the authenticated principal counts as an
    in-handler principal check.
    """
    if _mentions_marker(node):
        return True
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            helper = helpers.get(sub.func.id)
            if helper is not None and _mentions_marker(helper):
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
            helpers = _module_helper_bodies(tree)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                for route in _route_info(node):
                    if (relative, route) in MIGRATION_ALLOWLIST:
                        continue
                    if not _handler_authorized(node, helpers):
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
            56,
            "the authorization migration allowlist must not grow",
        )


if __name__ == "__main__":
    unittest.main()
