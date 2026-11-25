"""AST import discovery for repository and architecture gates."""

from __future__ import annotations

import ast
from importlib.util import resolve_name


def literal_dynamic_targets(tree: ast.AST, package: str = "") -> list[tuple[ast.Call, str]]:
    """Resolve statically identifiable dynamic imports."""
    importlib_names = {"importlib"}
    functions = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            importlib_names.update(alias.asname or alias.name for alias in node.names
                                   if alias.name == "importlib")
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib" and not node.level:
            functions.update(alias.asname or alias.name for alias in node.names
                             if alias.name == "import_module")
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_import_module = (
            isinstance(func, ast.Name) and func.id in functions
        ) or (
            isinstance(func, ast.Attribute) and func.attr == "import_module"
            and isinstance(func.value, ast.Name) and func.value.id in importlib_names
        )
        is_builtin = isinstance(func, ast.Name) and func.id == "__import__"
        if not (is_import_module or is_builtin):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        arg = node.args[0] if node.args else keywords.get("name")
        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
            continue
        target = arg.value
        if is_import_module and target.startswith("."):
            relative_package = node.args[1] if len(node.args) > 1 else keywords.get("package")
            if isinstance(relative_package, ast.Constant) and isinstance(relative_package.value, str):
                base = relative_package.value
            elif isinstance(relative_package, ast.Name) and relative_package.id == "__package__":
                base = package
            else:
                continue
            if not base:
                continue
            try:
                target = resolve_name(target, base)
            except ImportError:
                continue
        result.append((node, target))
    return result


def module_names(tree: ast.AST, package: str, *, include_dynamic: bool = True) -> set[str]:
    """Collect static and literal dynamic import targets without executing code."""
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level:
                try:
                    target = resolve_name("." * node.level + target, package)
                except ImportError:
                    continue
            if target:
                result.add(target)
    if include_dynamic:
        result.update(target for _, target in literal_dynamic_targets(tree, package))
    return result
