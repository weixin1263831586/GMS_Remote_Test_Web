"""UI structure tests must inspect the assembled Shell template."""

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def raw_shell_reads(source):
    tree = ast.parse(source)
    aliases = set()

    def references_entry(node):
        return any(
            (isinstance(item, ast.Constant) and isinstance(item.value, str)
             and item.value.replace("\\", "/").endswith("shell.html"))
            or (isinstance(item, ast.Name) and item.id in aliases)
            for item in ast.walk(node)
        )

    # Follow path variables as well as inline Path(...).read_text() calls.
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    for _ in range(len(assignments) + 1):
        previous = set(aliases)
        for node in assignments:
            if references_entry(node.value):
                aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
        if aliases == previous:
            break
    return [
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
        and references_entry(node)
        and ((isinstance(node.func, ast.Attribute) and node.func.attr in {"read_text", "read_bytes", "open"})
             or (isinstance(node.func, ast.Name) and node.func.id == "open"))
    ]


@pytest.mark.parametrize("source", [
    'shell = Path("web/shell/shell.html").read_text()',
    'entry = ROOT / "web" / "shell" / "shell.html"\nhtml = entry.read_text()',
    'entry = "web/shell/shell.html"\nwith open(entry) as handle: html = handle.read()',
])
def test_gate_detects_raw_entry_reads(source):
    assert raw_shell_reads(source)


@pytest.mark.parametrize("source", [
    'shell = read_shell_template()',
    'shell = read_shell_bundle()',
    'script = Path("web/static/js/navigation.js").read_text()',
    'entry = ROOT / "web/shell/shell.html"\nsize = entry.stat().st_size',
])
def test_gate_allows_assembly_and_file_metadata(source):
    assert not raw_shell_reads(source)


def test_ui_structure_tests_use_shell_assembly():
    paths = list((ROOT / "tests").rglob("test_*.py"))
    paths += [path for path in (ROOT / "features").glob("*/tests/test_*.py")]
    violations = []
    for path in paths:
        lines = raw_shell_reads(path.read_text(encoding="utf-8"))
        violations.extend(f"{path.relative_to(ROOT)}:{line}" for line in lines)
    assert not violations, (
        "Use tests.contract.snapshot_tools.read_shell_template/read_shell_bundle: "
        + ", ".join(violations)
    )
