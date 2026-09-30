"""Assistant dynamic-import boundary gate (AGENTS.md hard rule).

``features/assistant`` reaches platform capabilities through *string* refs:
``executor_ref="features.devices.api:start_usbip"`` and
``self._fetch_router_json("features.system.integrations", "get_vpn_status")``.
Those never appear in the AST import graph, so the cross-feature import gate
(test_dependency_rules) cannot see them — without this test the assistant can
quietly re-couple to any feature internal.

Rules enforced here:

1. Every dynamic target module must exist and every referenced symbol must
   resolve on that module (including ``from .x import y as y`` re-exports).
   Private ``_``-prefixed symbols are forbidden.
2. Dynamic calls may only target features listed in the frozen
   ``ALLOWED_DYNAMIC_TARGET_FEATURES``; a new feature must extend the set in
   review.  The legacy ``routers.*`` / ``core.*`` / ``modules.*`` channels are
   banned outright (they no longer exist).
3. Shrink-only facade ratchet: a dynamically imported cross-feature module
   must either be exported from the owning feature's ``__init__.py`` facade
   (the sanctioned public surface) or appear in ``FACADE_PENDING_MODULES``.
   Once the facade declares the module the pending entry must be deleted —
   pending entries whose facade already declares them fail here.
"""

import ast
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]

SCAN_DIR = ROOT / "features" / "assistant"

# Features the assistant may call dynamically (executor_ref / _fetch_router_json).
ALLOWED_DYNAMIC_TARGET_FEATURES = frozenset(
    {
        "automation",
        "build",
        "cluster",
        "devices",
        "firmware",
        "gerrit",
        "knowledge",
        "redmine",
        "reports",
        "system",
        "test_execution",
        "users",
    }
)

# Cross-feature modules referenced dynamically but not yet exported from
# their feature facade.  Shrink-only: remove an entry as soon as the facade
# declares the module (the facade-declared case is verified below and an
# entry left behind fails the ratchet test).
FACADE_PENDING_MODULES = frozenset(
    {
        "automation.api",
        "build.api",
        "cluster.devices_api",
        "cluster.deployment_api",
        "cluster.jobs_api",
        "devices.adb_forward_api",
        "devices.api",
        "devices.management_api",
        "firmware.firmware_api",
        "system.agent_package_registry",
        "system.api",
        "system.assets",
        "system.audit",
        "system.desktop",
        "system.integrations",
        "system.notifications_api",
        "system.terminal_api",
        "system.tools_data_api",
        "users.config_api",
    }
)

_FEATURE_REF_RE = re.compile(r"^(features\.[a-z_]+(?:\.[a-z_0-9]+)+)(?::([A-Za-z_][A-Za-z_0-9]*))?$")
_BANNED_CHANNEL_PREFIXES = ("routers.", "core.", "modules.")


def _assistant_sources():
    for path in SCAN_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or "/tests/" in path.as_posix():
            continue
        yield path


def _iter_strings(tree: ast.AST):
    """Yield string constants and the leading literal of f-strings."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.JoinedStr) and node.values:
            first = node.values[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                yield first.value


def _module_definitions(module_path: pathlib.Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                t.id for t in node.targets if isinstance(t, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            # ``from .x import y as y`` re-exports count as definitions.
            names.update(alias.name for alias in node.names)
    return names


def _facade_declared_modules(feature: str) -> set[str]:
    init = ROOT / "features" / feature / "__init__.py"
    if not init.exists():
        return set()
    tree = ast.parse(init.read_text(encoding="utf-8"))
    declared: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level >= 1:
            if node.module:
                declared.add(node.module.lstrip("."))
            declared.update(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(".")
        ):
            declared.add(node.value.lstrip("."))
    return declared


def _collect_dynamic_refs():
    """Return ``(feature_refs, banned_refs)`` from assistant sources.

    ``feature_refs`` maps ``(module, symbol_or_empty)`` to example sources;
    both ``module:symbol`` strings and ``_fetch_router_json(module, func)``
    literal pairs are collected.
    """
    refs: dict[tuple[str, str], list[str]] = {}
    banned: list[tuple[str, str]] = []
    for path in _assistant_sources():
        relative = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_fetch_router_json"
            ):
                if (
                    len(node.args) >= 2
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.startswith("features.")
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                ):
                    key = (node.args[0].value, node.args[1].value)
                    refs.setdefault(key, []).append(relative)
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                texts = (node.value,)
            elif isinstance(node, ast.JoinedStr) and node.values:
                # executor_ref 由 f-string 拼出时取前导字面量前缀。
                first = node.values[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    texts = (first.value,)
                else:
                    continue
            else:
                continue
            for text in texts:
                if text.startswith(_BANNED_CHANNEL_PREFIXES):
                    banned.append((relative, text))
                    continue
                if not text.startswith("features."):
                    continue
                match = _FEATURE_REF_RE.match(text.strip())
                if match:
                    key = (match.group(1), match.group(2) or "")
                    refs.setdefault(key, []).append(relative)
    return refs, banned


class AssistantDynamicImportGateTests(unittest.TestCase):
    def test_dynamic_targets_resolve_and_stay_public(self):
        refs, _ = _collect_dynamic_refs()
        offenders = []
        for (module, symbol), _sources in sorted(refs.items()):
            feature = module.split(".")[1]
            if feature == "assistant":
                # Same-domain calls: governed by ordinary import rules.
                continue
            if feature not in ALLOWED_DYNAMIC_TARGET_FEATURES:
                offenders.append(f"{module}: feature not allowlisted")
                continue
            module_path = ROOT / pathlib.Path(*module.split(".")).with_suffix(".py")
            if not module_path.exists():
                offenders.append(f"{module}:{symbol} -> module missing")
                continue
            if not symbol:
                continue
            if symbol.startswith("_"):
                offenders.append(f"{module}:{symbol} -> private symbol")
                continue
            if symbol not in _module_definitions(module_path):
                offenders.append(f"{module}:{symbol} -> symbol missing")
        self.assertEqual(
            offenders,
            [],
            "assistant dynamic refs (executor_ref / _fetch_router_json) must "
            "point at existing, public features.* symbols: "
            + "; ".join(offenders),
        )

    def test_no_legacy_execution_channels(self):
        _, banned = _collect_dynamic_refs()
        self.assertEqual(
            banned,
            [],
            "assistant must not build executor refs through legacy "
            "routers./core./modules. channels; target features.* only.",
        )

    def test_facade_ratchet_shrinks(self):
        offenders = []
        for pending in sorted(FACADE_PENDING_MODULES):
            feature, submodule = pending.split(".", 1)
            declared = _facade_declared_modules(feature)
            if submodule in declared:
                offenders.append(
                    f"{pending}: facade already declares it — delete the "
                    "FACADE_PENDING_MODULES entry (ratchet only shrinks)"
                )
        self.assertEqual(
            offenders,
            [],
            "; ".join(offenders),
        )

    def test_pending_manifest_has_no_stale_entries(self):
        refs, _ = _collect_dynamic_refs()
        referenced_submodules = {
            module.removeprefix("features.")
            for (module, _) in refs
            if module.split(".")[1] != "assistant"
        }
        stale = sorted(
            pending
            for pending in FACADE_PENDING_MODULES
            if pending not in referenced_submodules
        )
        self.assertEqual(
            stale,
            [],
            f"pending entries no longer referenced dynamically: {stale}; "
            "delete them to keep the manifest accurate.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
