import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_LINE_LIMITS = {
    # Existing debt may shrink, but must not grow while modules are split.
    'foundation/config.py': 764,
    'features/assistant/api.py': 1466,
    'features/assistant/executor.py': 1398,
    'features/assistant/tools.py': 976,
    'features/assistant/universal_ai.py': 957,
    'features/automation/executors.py': 998,
    'features/automation/service.py': 710,
    'features/cluster/repository.py': 693,
    # 这些模块使用显式上限，后续拆分时继续收紧。
    'features/cluster/api.py': 631,
    'features/cluster/tests/test_cluster.py': 608,
    'features/auth/service.py': 631,
    'features/devices/config_override.py': 700,
    'features/devices/tests/test_usbip_reconnect.py': 1107,
    'features/devices/usbip.py': 679,
    'features/firmware/firmware_api.py': 881,
    'features/gerrit/api.py': 643,
    'features/knowledge/storage.py': 790,
    'features/redmine/agent.py': 605,
    'features/redmine/analysis_resolution.py': 613,
    'features/redmine/api.py': 1027,
    'features/redmine/client.py': 724,
    'features/redmine/knowledge_service.py': 636,
    'features/redmine/tests/test_dashboard_stats.py': 1073,
    'features/reports/analysis_api.py': 765,
    'features/reports/api_helpers.py': 775,
    'features/reports/weekly_report_api.py': 1123,
    'features/system/api.py': 1123,
    'features/system/api_docs_list.py': 963,
    'features/system/icon_fetcher.py': 870,
}


class FileSizeRuleTests(unittest.TestCase):
    def test_new_python_modules_stay_reviewable(self):
        offenders = []
        for base in ('bootstrap', 'foundation', 'features', 'workflows'):
            for path in (ROOT / base).rglob('*.py'):
                relative = str(path.relative_to(ROOT))
                count = len(path.read_text(encoding='utf-8').splitlines())
                limit = MIGRATION_LINE_LIMITS.get(relative, 600)
                if count > limit:
                    offenders.append((relative, count))
        self.assertEqual(offenders, [])
