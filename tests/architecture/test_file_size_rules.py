import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXEMPTIONS = {
    # 2026-06-19 Task 12/13 migration exemptions. Final cutover must split or
    # delete these entries.
    'foundation/config.py',
    'features/assistant/api.py',
    'features/assistant/executor.py',
    'features/assistant/tools.py',
    'features/assistant/universal_ai.py',
    'features/system/api.py',
    'features/system/api_docs_list.py',
    'features/system/icon_fetcher.py',
    'features/system/integrations.py',
    'features/system/vnc.py',
}


class FileSizeRuleTests(unittest.TestCase):
    def test_new_python_modules_stay_reviewable(self):
        offenders = []
        for base in ('bootstrap', 'foundation', 'features', 'workflows'):
            for path in (ROOT / base).rglob('*.py'):
                relative = str(path.relative_to(ROOT))
                count = len(path.read_text(encoding='utf-8').splitlines())
                if count > 600 and relative not in EXEMPTIONS:
                    offenders.append((relative, count))
        self.assertEqual(offenders, [])
