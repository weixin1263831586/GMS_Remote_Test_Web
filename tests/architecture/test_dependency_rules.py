import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FEATURE_IMPORT_EXEMPTIONS = {
    # 2026-06-19 Task 12/13 migration: these are being moved behind workflows
    # before final cutover.
    'features/assistant/api.py',
    'features/assistant/executor.py',
    'features/system/integrations.py',
}
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
            if relative in MIGRATION_FEATURE_IMPORT_EXEMPTIONS:
                continue
            feature = path.relative_to(ROOT / 'features').parts[0]
            for name in imports(path):
                if not name.startswith('features.'):
                    continue
                parts = name.split('.')
                if len(parts) >= 3 and parts[1] != feature:
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
