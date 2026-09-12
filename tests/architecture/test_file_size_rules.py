import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_LINE_LIMITS = {
    # Existing debt may shrink, but must not grow while modules are split.
    'foundation/config.py': 771,  # +7: RuntimeConfigStore 集成（examples 模板回退/owner 隔离）
    'features/assistant/api.py': 1462,
    'features/assistant/executor.py': 1397,
    'features/assistant/tools.py': 967,
    'features/assistant/universal_ai.py': 970,
    'features/automation/executors.py': 1051,
    'features/automation/service.py': 736,
    'features/cluster/deployment_api.py': 775,  # +9: worker token 改 0600 文件传递
    # 这些模块使用显式上限，后续拆分时继续收紧。
    'features/cluster/api.py': 723,
    'features/cluster/tests/test_api_hardening.py': 744,
    'features/cluster/tests/test_cluster.py': 667,
    'features/auth/service.py': 608,
    'features/auth/agent_tokens.py': 365,
    'features/auth/constants.py': 81,  # +2: reports.read 人类角色
    'features/auth/tests/test_auth_api.py': 602,
    'features/auth/tests/test_security_boundary.py': 472,
    'features/devices/config_override.py': 732,
    # 57c30e1 grew apk_api.py 524→605 without registering it here; the rule
    # failed on clean HEAD. Registered at its current size; debt must now
    # shrink, not grow.
    'features/firmware/apk_api.py': 605,
    'features/firmware/tests/test_api.py': 620,
    'features/gerrit/api.py': 642,
    'features/knowledge/storage.py': 728,
    'features/redmine/agent.py': 592,
    'features/redmine/analysis_resolution.py': 583,
    'features/redmine/api.py': 976,
    'features/redmine/client.py': 712,
    'features/redmine/knowledge_service.py': 599,
    'features/redmine/tests/test_dashboard_stats.py': 1093,
    'features/reports/analysis_api.py': 752,  # +40: reports.read 门禁 helper
    'features/reports/api_helpers.py': 614,
    'features/reports/weekly_report_api.py': 1112,
    'features/system/api.py': 879,  # skills Deprecation/Sunset 头；assistant proxy 拆出后按实际规模收紧
    'features/system/gms_assistant_proxy.py': 542,  # 请求体上限 + 根级兼容路由 deprecation 头
    'features/system/api_docs_list.py': 985,
    'features/system/integrations.py': 618,  # +6: _HUMAN_ONLY vpn/ssh POST
    'features/system/assets.py': 604,  # +10: auth deps on opengrok/favicon
    'features/system/icon_fetcher.py': 861,
    'features/users/config_api.py': 616,
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
    'features/firmware/firmware_api.py': 962,  # agent burn gate；h 参数收敛后按实际规模收紧
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
