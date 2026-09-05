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
    ("features/cluster/transfer_ingest_api.py", "/transfers/{transfer_id}/report-analysis"),
    ("features/devices/api.py", "/api/device-groups/auto"),
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


def _imported_helper_sources(
    tree: ast.Module, relative: str
) -> dict[str, Path]:
    """Map imported helper name -> source file for cross-module indirection.

    Handlers frequently delegate principal resolution to another module
    (e.g. ``from .api import get_redmine_service_for_request``). Resolve
    the importing module so transitive checks can follow the chain.
    """
    sources: dict[str, Path] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ImportFrom, ast.Import)):
            continue
        module_name = ""
        names: list[tuple[str, str]] = []
        if isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            names = [(alias.name, alias.asname or alias.name) for alias in node.names]
        else:
            names = [(alias.name, alias.asname or alias.name) for alias in node.names]
        for imported, local in names:
            base = imported if isinstance(node, ast.Import) else (
                f"{module_name}.{imported}" if module_name else imported
            )
            # Handle relative imports first: from .api import x within a
            # feature package resolves to a sibling module.
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                package_dir = (ROOT / relative).parent
                for _ in range(node.level - 1):
                    package_dir = package_dir.parent
                candidates = [
                    package_dir / f"{module_name}.py" if module_name
                    else package_dir / "__init__.py",
                    package_dir / module_name / "__init__.py",
                ]
            else:
                if not base.startswith("features."):
                    continue
                parts = base.split(".")
                candidates = [
                    ROOT / Path(*parts) / "__init__.py",
                    ROOT / Path(*parts).with_suffix(".py"),
                ]
            for candidate in candidates:
                if candidate.is_file():
                    sources[local] = candidate
                    break
    return sources


def _helpers_for_module(path: Path, cache: dict[str, tuple]) -> tuple:
    """Return (helpers, dep_variables) for a module, parsing it once."""
    key = str(path)
    if key not in cache:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cache[key] = (_module_helper_bodies(tree), _module_dep_variables(tree))
    return cache[key]


def _module_dep_variables(tree: ast.Module) -> dict[str, ast.expr]:
    """Map top-level list/tuple variable name -> element node.

    Handlers commonly reference module-level dependency lists such as
    ``_AUTH_REQUIRED = [Depends(require_authenticated_user_...)]`` from the
    decorator's ``dependencies=`` argument.
    """
    variables: dict[str, ast.expr] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)) and node.value.elts:
            variables[target.id] = node.value.elts[0]
    return variables


def _mentions_marker(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """Direct mention of an auth call marker inside the function body.

    Markers match by prefix: ``require_authenticated_user_when_auth_required``
    and ``require_resource_owner_when_auth_required`` are genuine principal
    checks even though their names extend the base markers.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and any(
            sub.id == marker or sub.id.startswith(marker)
            for marker in _AUTH_CALL_MARKERS
        ):
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in _AUTH_CALL_MARKERS:
            return True
    return False


def _mentions_marker_expr(node: ast.expr) -> bool:
    """Marker check for an arbitrary expression (dependency list element)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and any(
            sub.id == marker or sub.id.startswith(marker)
            for marker in _AUTH_DEPEND_MARKERS
        ):
            return True
    return False


def _resolve_imported(
    name: str,
    imported_sources: dict[str, Path],
    parse_cache: dict[str, tuple],
):
    """Resolve an imported helper name to (path, (helpers, deps))."""
    source = imported_sources.get(name)
    if source is not None and source.is_file():
        module_helpers, module_deps = _helpers_for_module(source, parse_cache)
        if name in module_helpers:
            return source, (module_helpers, module_deps)
    return None


def _feature_module_index(cache: dict[str, tuple]) -> dict[str, tuple[Path, tuple]]:
    """Index every top-level helper name in features/ -> (module path, entry).

    Built once per test run; used to resolve attribute-style cross-module
    calls (``helpers._require_transfer_access(...)``) whose receiver is a
    lazily imported module object.
    """
    index: dict[str, tuple[Path, tuple]] = {}
    for path in sorted((ROOT / "features").rglob("*.py")):
        relative = str(path.relative_to(ROOT))
        if "/tests/" in relative or "__pycache__" in relative:
            continue
        module_helpers, _deps = _helpers_for_module(path, cache)
        for name in module_helpers:
            index.setdefault(name, (path, _helpers_for_module(path, cache)))
    return index


def _handler_authorized(
    node: ast.AsyncFunctionDef | ast.FunctionDef,
    helpers: dict[str, ast.AsyncFunctionDef | ast.FunctionDef],
    dep_variables: dict[str, ast.expr] | None = None,
    imported_sources: dict[str, Path] | None = None,
    feature_index: dict[str, tuple[Path, tuple]] | None = None,
) -> bool:
    """Authorization check with transitive same/cross-module helper indirection.

    Handlers commonly wrap principal resolution in a module-local helper
    (e.g. ``_user()`` -> ``get_client_id_from_request``) or delegate to an
    imported helper in another feature module. A helper that transitively
    resolves the authenticated principal counts as an in-handler check.
    """
    dep_variables = dep_variables or {}
    imported_sources = imported_sources or {}
    feature_index = feature_index or {}
    if _mentions_marker(node):
        return True
    # Module-level dependency lists (e.g. dependencies=_AUTH_REQUIRED).
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            element = dep_variables.get(sub.id)
            if element is not None and _mentions_marker_expr(element):
                return True
    # Helper indirection, resolved transitively (same module + imported
    # feature modules) with a budget to bound the traversal.
    parse_cache: dict[str, tuple] = {}
    seen: set[tuple[str, str]] = set()
    frontier: list[tuple[ast.AST, dict, dict, str]] = [
        (node, helpers, dep_variables, "")]
    budget = 128
    while frontier and budget > 0:
        budget -= 1
        current, current_helpers, current_deps, origin = frontier.pop()
        for sub in ast.walk(current):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                # Attribute calls like helpers._require_transfer_access():
                # resolve the method name across feature modules.
                if sub.func.attr in _AUTH_CALL_MARKERS:
                    return True
                name = sub.func.attr
                if (origin, name) not in seen:
                    seen.add((origin, name))
                    entry = feature_index.get(name)
                    if entry is not None:
                        source, (module_helpers, module_deps) = entry
                        cross_helper = module_helpers.get(name)
                        if cross_helper is not None:
                            if _mentions_marker(cross_helper):
                                return True
                            frontier.append(
                                (cross_helper, module_helpers, module_deps,
                                 str(source)))
                continue
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)):
                continue
            name = sub.func.id
            key = (origin, name)
            if key in seen:
                continue
            seen.add(key)
            helper = current_helpers.get(name)
            if helper is None:
                # Name not defined in this module: it may be imported from
                # another feature module (module-level or function-level
                # import) — resolve via the feature-wide index and continue.
                entry = feature_index.get(name) or _resolve_imported(
                    name, imported_sources, parse_cache)
                if entry is not None:
                    source, (module_helpers, module_deps) = entry
                    cross_helper = module_helpers.get(name)
                    if cross_helper is not None:
                        if _mentions_marker(cross_helper):
                            return True
                        frontier.append(
                            (cross_helper, module_helpers, module_deps,
                             str(source)))
                continue
            if _mentions_marker(helper):
                return True
            # Follow helpers imported from other feature modules: resolve
            # the imported module once and continue the walk there.
            source = imported_sources.get(name)
            if source is not None:
                module_helpers, module_deps = _helpers_for_module(
                    source, parse_cache)
                frontier.append((helper, module_helpers, module_deps, str(source)))
            else:
                frontier.append((helper, current_helpers, current_deps, origin))
    return False


class SensitiveRouteAuthorizationTests(unittest.TestCase):
    def test_state_changing_api_routes_declare_authorization(self):
        offenders = []
        parse_cache: dict[str, tuple] = {}
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
            dep_variables = _module_dep_variables(tree)
            imported_sources = _imported_helper_sources(tree, relative)
            feature_index = _feature_module_index(parse_cache)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                for route in _route_info(node):
                    if (relative, route) in MIGRATION_ALLOWLIST:
                        continue
                    if not _handler_authorized(
                        node, helpers, dep_variables, imported_sources,
                        feature_index,
                    ):
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
            6,
            "the authorization migration allowlist must not grow",
        )


if __name__ == "__main__":
    unittest.main()
