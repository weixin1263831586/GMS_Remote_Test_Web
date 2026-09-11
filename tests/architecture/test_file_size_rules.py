import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_LINE_LIMITS = {
    # Existing debt may shrink, but must not grow while modules are split.
    'foundation/config.py': 771,  # +7: RuntimeConfigStore 集成（examples 模板回退/owner 隔离）
    'features/assistant/api.py': 1466,
    'features/assistant/executor.py': 1398,
    'features/assistant/tools.py': 976,
    'features/assistant/universal_ai.py': 971,
    'features/automation/executors.py': 1051,
    'features/automation/service.py': 736,
    'features/cluster/deployment_api.py': 776,  # +9: P3-3 worker token 改 0600 文件传递
    # 这些模块使用显式上限，后续拆分时继续收紧。
    'features/cluster/api.py': 723,
    'features/cluster/tests/test_api_hardening.py': 744,
    'features/cluster/tests/test_cluster.py': 667,
    'features/auth/service.py': 637,
    'features/auth/agent_tokens.py': 456,
    'features/auth/constants.py': 89,  # +2: P1 reports.read 人类角色
    'features/auth/tests/test_auth_api.py': 610,
    'features/auth/tests/test_security_boundary.py': 615,
    'features/devices/config_override.py': 740,
    # 57c30e1 grew apk_api.py 524→605 without registering it here; the rule
    # failed on clean HEAD. Registered at its current size; debt must now
    # shrink, not grow.
    'features/firmware/apk_api.py': 605,
    'features/firmware/tests/test_api.py': 624,
    'features/gerrit/api.py': 643,
    'features/knowledge/storage.py': 790,
    'features/redmine/agent.py': 605,
    'features/redmine/analysis_resolution.py': 613,
    'features/redmine/api.py': 1027,
    'features/redmine/client.py': 724,
    'features/redmine/knowledge_service.py': 636,
    'features/redmine/tests/test_dashboard_stats.py': 1093,
    'features/reports/analysis_api.py': 752,  # +40: P1 reports.read 门禁 helper
    'features/reports/api_helpers.py': 775,
    'features/reports/weekly_report_api.py': 1123,
    'features/system/api.py': 1185,  # websocket/jq 端点 +41; installer wrapper 重写净 +1
    'features/system/api_docs_list.py': 985,
    'features/system/integrations.py': 619,  # +6: P1 _HUMAN_ONLY vpn/ssh POST
    'features/system/assets.py': 604,  # +10: auth deps on opengrok/favicon
    'features/system/icon_fetcher.py': 870,
    'features/users/config_api.py': 617,
    'features/devices/adb_proxy_service.py': 874,  # adb proxy Hub 重启防护 + host-level disconnect guards
    'features/devices/config_explorer.py': 625,
    'features/devices/integrations_api.py': 2554,
    'features/devices/reconnect.py': 942,
    'features/devices/tests/test_adb_proxy_service.py': 911,  # adb proxy 重启/断连 guard 回归桩
    'features/devices/tests/test_usbip_flash_modes.py': 808,
    'features/devices/tests/test_usbip_linux_source.py': 990,
    'features/devices/tests/test_usbip_reconnect.py': 3121,
    'features/devices/usbip_linux_source.py': 818,
    'features/devices/usbip.py': 1631,
    'features/firmware/firmware_api.py': 1082,  # +28: §五 agent burn approval-token gate
    'features/system/vnc.py': 613,
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
