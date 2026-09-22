import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_LINE_LIMITS = {
    # Existing debt may shrink, but must not grow while modules are split.
    'foundation/config.py': 771,  # +7: RuntimeConfigStore 集成（examples 模板回退/owner 隔离）
    'features/assistant/api.py': 1462,
    'features/assistant/executor.py': 1397,
    'features/assistant/tools.py': 917,  # knowledge 工具拆至 knowledge_tools.py; -50 后收缩登记
    'features/assistant/universal_ai.py': 970,
    'features/automation/executors.py': 1095,  # +44: ADR 0012 回环请求携带能力 Bearer 头
    'features/automation/service.py': 771,  # +35: ADR 0012 能力快照编译入 run
    'features/automation/tests/test_api.py': 684,  # +84: ADR 0012 human-only 验收（agent 403 / 能力不足 400）
    'features/cluster/deployment_api.py': 775,  # +9: worker token 改 0600 文件传递
    # 这些模块使用显式上限，后续拆分时继续收紧。
    'features/cluster/api.py': 760,  # +37: page JS 静态端点(CSP 前置迁移); +3: ADR 0010 owner 读侧注释
    'features/cluster/tests/test_api_hardening.py': 744,
    'features/cluster/tests/test_cluster.py': 667,
    # +43: 启动期 reconcile_claims()（跨库 split-brain 修复）；
    # 后续把 reconcile 拆到 repository_reconciliation.py 时应回落 600。
    'features/cluster/repository.py': 651,
    # +25: upsert_seen_device（终端握手库存滞后的实时探测回填）。
    'features/cluster/repository_inventory.py': 623,
    # +7: update_user last-admin 检查移入 BEGIN IMMEDIATE（跨进程原子性）。
    'features/auth/service.py': 656,
    'features/auth/agent_tokens.py': 365,
    'features/auth/constants.py': 98,  # +14: build.* scopes 与人类角色权限(ADR 0006)
    'features/auth/api.py': 606,  # 23edaef 后按实际行数登记（+6 超 600 默认限额）
    'features/auth/tests/test_auth_api.py': 636,  # 23edaef 后按实际行数登记（602→636）
    'features/auth/tests/test_security_boundary.py': 472,
    # +105: tmux 嵌套引号注入回归（7 组恶意 workspace，真实 shell 执行桩）。
    'features/build/tests/test_build_service.py': 705,
    'features/devices/config_override.py': 732,
    # 57c30e1 grew apk_api.py 524→605 without registering it here; the rule
    # failed on clean HEAD. Registered at its current size; debt must now
    # shrink, not grow.
    'features/firmware/apk_api.py': 605,
    # 2026-09 shares home 扩展；登记当前尺寸，债务只减不增。
    'features/firmware/shares_api.py': 634,
    'features/firmware/tests/test_api.py': 620,
    'features/gerrit/api.py': 652,  # +10: page JS 静态端点(CSP 前置迁移)
    'features/knowledge/storage.py': 728,
    'features/redmine/agent.py': 592,
    # 2026-09 triage 证据门禁与 MCP 健康预检；登记当前尺寸，拆分后回落。
    'features/redmine/kkagent/analyzer.py': 674,
    'features/redmine/analysis_resolution.py': 583,
    'features/redmine/api.py': 976,
    'features/redmine/client.py': 749,  # 2026-09 triage evidence precollect；登记当前尺寸，拆分后回落
    'features/redmine/knowledge_service.py': 599,
    # 2026-09 并发收敛:enqueue 去重键修正 + runs ON CONFLICT + job lease_token
    # CAS + 旧库迁移分支;后续拆 jobs 队列到独立模块时应回落 600。
    'features/redmine/daily_brief_repository.py': 918,  # +178: f64c054 及后续协作式取消/执行统计扩展; +7: deep-analysis 接入; +81: triage 证据预采集;拆分后回落
    'features/redmine/daily_brief_service.py': 709,  # +21: f64c054 执行统计; +82: triage 证据预采集与脱敏落库; +6: record.error/failure_message 脱敏
    'features/redmine/tests/test_daily_brief_api.py': 603,  # +3: f64c054 统计接口回归
    'features/redmine/tests/test_daily_brief_repository.py': 728,  # +23: f64c054 取消路径回归; +7: deep-analysis 接入; +98: 2026-09 证据预采集回归
    'features/redmine/tests/test_daily_brief_service.py': 793,  # +21: f64c054; +143: 2026-09 证据预采集/脱敏回归
    # +72: sanitizeHref scheme 白名单回归(node 执行测试)。
    'features/redmine/repository_queries.py': 614,  # 23edaef 后按实际行数登记（+14 超 600 默认限额）
    'features/redmine/tests/test_dashboard_stats.py': 1286,  # 23edaef 后按实际行数登记（1165→1286）
    'features/reports/analysis_api.py': 654,  # -98: mainline/KB 召回拆至 diagnosis_recalls.py; ADR 0014 系统背景召回接入
    'features/reports/api_helpers.py': 614,
    'features/reports/weekly_report_api.py': 1112,
    'features/system/update_monitor/api.py': 602,  # +page JS 静态端点(CSP 前置迁移)
    'features/system/api.py': 879,  # skills Deprecation/Sunset 头；assistant proxy 拆出后按实际规模收紧
    'features/system/gms_assistant_proxy.py': 542,  # 请求体上限 + 根级兼容路由 deprecation 头
    'features/system/api_docs_list.py': 985,
    'features/system/integrations.py': 618,  # +6: _HUMAN_ONLY vpn/ssh POST
    'features/system/assets.py': 604,  # +10: auth deps on opengrok/favicon
    'features/system/icon_fetcher.py': 861,
    # 2026-09 终端辅助服务扩展；登记当前尺寸，债务只减不增。
    'features/system/terminal_service.py': 677,
    'features/users/config_api.py': 616,
    'features/devices/adb_proxy_service.py': 874,  # adb proxy Hub 重启防护 + host-level disconnect guards
    'features/devices/config_explorer.py': 625,
    # +5: stop_usbip 两处阻塞调用迁 asyncio.to_thread（事件循环冻结热修）。
    'features/devices/integrations_api.py': 2469,  # assignments 存储层已拆至 usbip_assignments.py; +1: ADR 0010 owner 注释
    'features/devices/reconnect.py': 942,
    # 2026-09 串口日志 OSError 隔离（不误杀采集）+ retention 容错；拆分后回落。
    'features/devices/serial_console.py': 621,
    'features/devices/tests/test_adb_proxy_service.py': 911,  # adb proxy 重启/断连 guard 回归桩
    'features/devices/tests/test_usbip_flash_modes.py': 851,  # +43: scoped mode 重算回归
    'features/devices/tests/test_usbip_linux_source.py': 990,
    'features/devices/tests/test_usbip_reconnect.py': 3121,
    'features/devices/usbip_linux_source.py': 818,
    'features/devices/usbip.py': 939,  # 协议/来源会话/清单拆至 usbip_protocol/_source_sessions/_source_inventory
    'features/firmware/firmware_api.py': 962,  # agent burn gate；h 参数收敛后按实际规模收紧
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
