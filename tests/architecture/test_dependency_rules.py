import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FEATURE_IMPORT_EXEMPTIONS = set()
MIGRATION_FOUNDATION_IMPORT_EXEMPTIONS = set()


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def imported_symbols(path: Path) -> list[tuple[str, str]]:
    """Return ``(module, symbol_name)`` pairs for every ``from X import Y``."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    result: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                result.append((node.module, alias.name))
    return result


class DependencyRuleTests(unittest.TestCase):
    def test_foundation_never_imports_features(self):
        offenders = []
        for path in (ROOT / 'foundation').rglob('*.py'):
            relative = str(path.relative_to(ROOT))
            if relative in MIGRATION_FOUNDATION_IMPORT_EXEMPTIONS:
                continue
            bad = sorted(
                name
                for name in imports(path)
                if name == 'features' or name.startswith('features.')
            )
            if bad:
                offenders.append((str(path.relative_to(ROOT)), bad))
        self.assertEqual(offenders, [])

    def test_features_never_import_other_feature_internals(self):
        offenders = []
        for path in (ROOT / 'features').rglob('*.py'):
            relative = str(path.relative_to(ROOT))
            feature = path.relative_to(ROOT / 'features').parts[0]
            for name in imports(path):
                if not name.startswith('features.'):
                    continue
                parts = name.split('.')
                if (
                    len(parts) >= 3
                    and parts[1] != feature
                    and (relative, name) not in MIGRATION_FEATURE_IMPORT_EXEMPTIONS
                ):
                    offenders.append((str(path.relative_to(ROOT)), name))
        self.assertEqual(offenders, [])

    def test_new_code_does_not_import_old_packages(self):
        offenders = []
        for base in ('bootstrap', 'foundation', 'features', 'workflows'):
            for path in (ROOT / base).rglob('*.py'):
                relative = str(path.relative_to(ROOT))
                bad = sorted(
                    name
                    for name in imports(path)
                    if name in {'core', 'routers', 'modules'}
                    or name.startswith(('core.', 'routers.', 'modules.'))
                )
                if bad:
                    offenders.append((relative, bad))
        self.assertEqual(offenders, [])

    def test_never_import_private_symbols_across_features(self):
        """``_``-prefixed names must not leak across feature boundaries.

        Symbols like ``_helper`` are package-private.  Importing them from
        another feature (or from ``bootstrap``) couples consumers to internal
        implementation details.  If a name is needed externally, drop the
        underscore and re-export it from the feature package.
        """
        offenders = []
        for base in ('features', 'bootstrap', 'workflows'):
            for path in (ROOT / base).rglob('*.py'):
                relative = str(path.relative_to(ROOT))
                if '/tests/' in relative or '__pycache__' in relative:
                    continue
                current_feature = None
                parts_path = path.relative_to(ROOT).parts
                if parts_path[0] == 'features' and len(parts_path) > 1:
                    current_feature = parts_path[1]
                for module, symbol in imported_symbols(path):
                    if not module.startswith('features.'):
                        continue
                    mod_parts = module.split('.')
                    target_feature = mod_parts[1] if len(mod_parts) >= 2 else None
                    if target_feature == current_feature:
                        continue
                    if symbol.startswith('_'):
                        offenders.append((relative, f'from {module} import {symbol}'))
        self.assertEqual(offenders, [])
