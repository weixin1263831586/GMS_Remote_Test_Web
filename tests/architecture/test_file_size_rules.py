import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXEMPTIONS: set[str] = set()


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
