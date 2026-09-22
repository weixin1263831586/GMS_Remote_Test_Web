"""Architecture guard: sensitive routes must carry an explicit authorization.

The platform defines ROLE_PERMISSIONS and ``require_permission`` but most
endpoints still rely on ad-hoc handler-internal checks.  This test keeps that
debt visible and ratchets it down:

- Every state-changing route under ``/api`` must either be bound to a
  ``Depends(require_...)`` dependency / call one of the access-control helpers
  inside the handler, or be listed in ``MIGRATION_ALLOWLIST`` below.
- The allowlist may only shrink: deleting an entry turns that route into an
  enforcement obligation.

Level 2 (capability semantics) ratchets on top of Level 1:

- ``HUMAN_ONLY_ROUTE_MANIFEST``: routes whose surface mutates orchestration
  itself (ATS runs, ADR 0012) must demand a HUMAN principal — an
  authentication-only check is not enough, because agent tokens authenticate
  fine while holding zero capabilities.
- Resource-owner identity hygiene: fields named ``owner``/``owner_id``/
  ``owner_user_id``/``created_for`` must never be compared with — or assigned
  from — a principal's actor ``id`` (ADR 0010); use the account-scoped
  ``resource_owner_id`` / ``principal_owner_id()`` accessors instead.
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
    # /agent-enroll is the pairing-code redemption boundary: it accepts a
    # one-shot enrollment code INSTEAD of a session by design (the build
    # server has no session yet). Scopes/ACLs/expiry come from the server-side
    # enrollment record, so a leaked code grants nothing beyond what the
    # admin approved. Fail-closed: unknown/used/expired code → 403.
    ("features/auth/agent_api.py", "/agent-enroll"),
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
    "require_human_principal",
)

# Handler-internal helpers that establish or check a principal.
_AUTH_CALL_MARKERS = (
    "require_permission",
    "require_role",
    "require_elevated_admin",
    "require_authenticated_user",
    "require_resource_owner",
    "require_human_principal",
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
        # 全量 features/ 树只索引一次（函数契约如此）；放循环内是 O(files²)。
        feature_index = _feature_module_index(parse_cache)
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
        # +1 for /agent-enroll (pairing-code redemption boundary —
        # the build server has no session yet; the one-shot code IS the
        # credential, scopes/ACLs/expiry come from the server-side record).
        self.assertLessEqual(
            len(MIGRATION_ALLOWLIST),
            7,
            "the authorization migration allowlist must not grow",
        )


# ---------------------------------------------------------------------------
# Level 2: human-only route semantics (ADR 0012).
# ---------------------------------------------------------------------------

# The ATS run surface mutates the orchestration pipeline itself. A
# zero-scope agent token authenticates fine, so Level-1 "is authenticated"
# is NOT the right gate here: runs must be created/triggered only by human
# principals, and the run's authority is snapshotted from that human
# creator at this single entry point (the worker derives its own machine
# principal from the snapshot — it never escalates beyond the creator).
HUMAN_ONLY_ROUTE_MANIFEST = {
    "features/automation/api.py": {
        "/runs",
        "/runs/preflight",
        "/runs/{run_id}/cancel",
        "/runs/{run_id}/retry",
    },
}

# Calls that correctly establish a HUMAN principal (agent/machine tokens
# are rejected / fail-closed). Matching is prefix-based so the
# *_when_auth_required variants count too.
_HUMAN_PRINCIPAL_CALLS = (
    "require_human_principal",
)

# Authentication-only helpers that do NOT prove a human principal; kept
# explicit so the failure message can teach the fix instead of just failing.
_AUTHENTICATION_ONLY_CALLS = (
    "require_authenticated_user",
    "get_authenticated_user",
    "require_agent_scope",
)


def _call_names(node: ast.AST) -> set[str]:
    """Names referenced as calls or passed around (e.g. inside Depends(...))."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            names.add(sub.func.id)
        elif isinstance(sub, ast.Name):
            names.add(sub.id)
    return names


class HumanOnlyRouteSemanticsTests(unittest.TestCase):
    """Level-2 check: some routes demand a human principal, not just auth."""

    def test_human_only_manifest_routes_require_human_principal(self):
        offenders = []
        for relative, routes in sorted(HUMAN_ONLY_ROUTE_MANIFEST.items()):
            path = ROOT / relative
            tree = ast.parse(path.read_text(encoding="utf-8"))
            helpers = _module_helper_bodies(tree)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                for route in _route_info(node):
                    if route not in routes:
                        continue
                    calls = _call_names(node)
                    # Follow one level of same-module helper indirection
                    # (handlers wrap their principal checks in helpers).
                    for name in list(calls):
                        helper = helpers.get(name)
                        if helper is not None:
                            calls |= _call_names(helper)
                    if any(
                        name.startswith(_HUMAN_PRINCIPAL_CALLS)
                        for name in calls
                    ):
                        continue
                    offenders.append((relative, route, sorted(calls)))
        self.assertEqual(
            offenders,
            [],
            "human-only routes must call require_human_principal(_when_auth_required)"
            " directly or via a helper; authentication-only checks let zero-scope"
            f" agent tokens onto the orchestration surface: {offenders}",
        )

    def test_human_only_manifest_is_current(self):
        """Manifest entries must exist (no deleting routes to pass the gate)."""
        discovered = set()
        for relative in HUMAN_ONLY_ROUTE_MANIFEST:
            tree = ast.parse(
                (ROOT / relative).read_text(encoding="utf-8")
            )
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                discovered |= {
                    (relative, route) for route in _route_info(node)
                }
        stale = [
            (relative, route)
            for relative, routes in HUMAN_ONLY_ROUTE_MANIFEST.items()
            for route in routes
            if (relative, route) not in discovered
        ]
        self.assertEqual(
            stale,
            [],
            "human-only manifest lists routes that no longer exist; update the"
            f" manifest to match the real surface: {stale}",
        )


# ---------------------------------------------------------------------------
# Level 2b: resource-owner identity hygiene (ADR 0010).
# ---------------------------------------------------------------------------

# Persistence/authorization fields that store the resource-owner ACCOUNT.
_OWNER_FIELD_NAMES = ("owner", "owner_id", "owner_user_id", "created_for")

# Receivers whose ``.id`` is the ACTOR identity (an agent token yields a
# synthetic ``agent:<token_id>``); owner fields must instead be compared
# with / assigned from the account-scoped ``resource_owner_id``.
_PRINCIPAL_RECEIVERS = ("user", "current_user", "principal", "_user")

# Rotating exemptions out requires a real fix: the set must stay empty.
# (Test fixtures under features/**/tests/ are excluded from the scan.)
_OWNER_IDENTITY_EXCEPTIONS: set[tuple[str, int]] = set()


def _is_owner_field_ref(node: ast.AST) -> bool:
    """Owner-field reference: attribute, dict key, or conventional variable.

    Covers ``x.owner_id``, ``record["owner_id"]`` and local aliases such as
    ``owner_id`` / ``claim_owner_id`` (exact or ``*_owner_id`` naming) that
    handlers copy owner fields into before comparing.
    """
    if isinstance(node, ast.Attribute) and node.attr in _OWNER_FIELD_NAMES:
        return True
    if isinstance(node, ast.Name):
        return any(
            node.id == name or node.id.endswith("_" + name)
            for name in _OWNER_FIELD_NAMES
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        # dict.get("owner_id") / mapping getter for an owner field.
        return node.func.attr in {"get", "get_int", "get_str"} and any(
            isinstance(arg, ast.Constant) and arg.value in _OWNER_FIELD_NAMES
            for arg in node.args
        )
    if isinstance(node, ast.Subscript):
        slice_value = getattr(node.slice, "value", None)
        return isinstance(slice_value, str) and slice_value in _OWNER_FIELD_NAMES
    return False


def _is_principal_id_ref(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "id"
        and isinstance(node.value, ast.Name)
        and node.value.id in _PRINCIPAL_RECEIVERS
    )


class ResourceOwnerIdentityHygieneTests(unittest.TestCase):
    """Owner fields must never bind to the (mutable) actor identity.

    Regression guard for the class of bugs where a job/report/command was
    stored or filtered with ``user.id`` — an agent token would then orphan
    its own records on the next rotation (ADR 0010). The scan flags
    comparisons (``==``/``!=``/``is``), attribute/key assignments and
    keyword arguments that mix an owner field with a principal actor id.
    """

    def test_owner_fields_never_compare_or_assign_actor_id(self):
        offenders = []
        for base in ("features", "foundation"):
            for path in sorted((ROOT / base).rglob("*.py")):
                relative = str(path.relative_to(ROOT))
                if "/tests/" in relative or "__pycache__" in relative:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Compare):
                        operands = [node.left, *node.comparators]
                        if any(
                            _is_owner_field_ref(op) for op in operands
                        ) and any(_is_principal_id_ref(op) for op in operands):
                            offenders.append((relative, node.lineno))
                    elif isinstance(node, ast.Assign):
                        if any(
                            _is_owner_field_ref(target)
                            for target in node.targets
                        ) and _is_principal_id_ref(node.value):
                            offenders.append((relative, node.lineno))
                    elif isinstance(node, ast.keyword):
                        if (
                            node.arg in _OWNER_FIELD_NAMES
                            and _is_principal_id_ref(node.value)
                        ):
                            offenders.append((relative, node.lineno))
        unexpected = [
            entry for entry in offenders
            if entry not in _OWNER_IDENTITY_EXCEPTIONS
        ]
        stale = [
            entry for entry in _OWNER_IDENTITY_EXCEPTIONS
            if entry not in offenders
        ]
        self.assertEqual(
            unexpected,
            [],
            "owner fields must use the resource-owner ACCOUNT identity "
            "(user.resource_owner_id / principal_owner_id(request)), never the "
            f"actor id (user.id) — token rotation orphans the record: {unexpected}",
        )
        self.assertEqual(
            stale,
            [],
            f"stale owner-identity exceptions must be removed: {stale}",
        )
        self.assertEqual(
            _OWNER_IDENTITY_EXCEPTIONS,
            set(),
            "the owner-identity exception set must stay empty; fix the code "
            "instead of adding exemptions",
        )


class OwnerIdentityTaintCheckTests(unittest.TestCase):
    """Business modules must never read the raw principal ``.id`` attribute.

    升级 ResourceOwnerIdentityHygieneTests 的语法扫描：
    只要业务代码出现 ``user.id`` 这类原始读，AST 就已丢失"这个值之后会
    流向 owner 字段还是 audit 归因"的语义，别名/转发/位置参数都能逃过
    sink 匹配。因此直接把"未分类的主体身份读"整体设为违规——调用方必须
    显式声明身份类别：

    - actor（audit / WebSocket session / 运行时锁持有者）：
      ``principal_actor_id(request)`` 或 ``principal.actor_id``
    - resource owner（落库的 owner/claim/report/artifact 分区键）：
      ``principal_owner_id(request)`` 或 ``principal.resource_owner_id``

    身份的定义与装配管道（``features/auth/**``、
    ``features/users/clients.py`` 的 ``get_client_id_from_request``）豁免；
    tests 目录沿用既有卫生测试的排除规则。
    """

    _PRINCIPAL_ID_PIPELINES = (
        "features/auth/",
        "features/users/clients.py",
    )

    def test_business_modules_never_read_raw_principal_id(self):
        offenders = []
        for base in ("features", "foundation", "bootstrap", "worker_agent"):
            for path in sorted((ROOT / base).rglob("*.py")):
                relative = str(path.relative_to(ROOT))
                if "/tests/" in relative or "__pycache__" in relative:
                    continue
                if any(
                    relative.startswith(prefix)
                    for prefix in self._PRINCIPAL_ID_PIPELINES
                ):
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Attribute)
                        and node.attr == "id"
                        and isinstance(node.value, ast.Name)
                        and node.value.id in _PRINCIPAL_RECEIVERS
                    ):
                        offenders.append((relative, node.lineno))
        self.assertEqual(
            offenders,
            [],
            "raw principal .id reads are forbidden outside the identity "
            "pipelines — classify the identity explicitly: actor → "
            "principal_actor_id(request)/.actor_id, resource owner → "
            f"principal_owner_id(request)/.resource_owner_id: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
