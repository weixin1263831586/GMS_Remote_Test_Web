import unittest
from pathlib import Path


EXPECTED_SIDEBAR_PAGES = {
    "test": "测试界面",
    "desktop": "主机桌面",
    "terminal": "主机终端",
    "users": "用户管理",
    "devices": "设备管理",
    "reports": "报告管理",
    "report-analysis": "报告分析",
    "apk-analysis": "APK分析",
    "test-suites": "测试套件",
    "api-docs": "系统接口",
    "architecture": "系统架构",
    "websites": "常用网址",
    "tools": "常用工具",
    "security-audit": "安全审计",
    "gms-assistant": "GMS助手",
    "automation": "GMS ATS",
    "redmine-agent": "Redmine看板",
    "gerrit-dashboard": "Gerrit看板",
    "agent": "对话Agent",
}


class AgentIntentTests(unittest.TestCase):
    def test_agent_can_navigate_to_automation_page(self):
        from features.assistant.intent import resolve

        intent = resolve("打开 GMS ATS", {})

        self.assertEqual(intent.tool_name, "navigate")
        self.assertEqual(intent.params["page"], "automation")
        self.assertGreaterEqual(intent.confidence, 0.9)

    def test_agent_navigation_aliases_cover_all_sidebar_pages(self):
        from features.assistant.intent import _NAV_ALIASES, resolve

        template = Path("templates/index_fastapi.html").read_text(encoding="utf-8", errors="ignore")
        for page, label in EXPECTED_SIDEBAR_PAGES.items():
            with self.subTest(page=page):
                self.assertIn(f'data-page="{page}"', template)
                self.assertIn(page, set(_NAV_ALIASES.values()))

                intent = resolve(f"打开 {label}", {})
                self.assertEqual(intent.tool_name, "navigate")
                self.assertEqual(intent.params["page"], page)

    def test_page_overview_and_display_names_cover_all_sidebar_pages(self):
        from features.assistant.response import _page_display_name, generate_page_overview, page_quick_actions

        overview = generate_page_overview()
        quick_actions = page_quick_actions()
        self.assertEqual(
            {item["page"]: item["label"] for item in quick_actions},
            EXPECTED_SIDEBAR_PAGES,
        )
        for page, label in EXPECTED_SIDEBAR_PAGES.items():
            with self.subTest(page=page):
                self.assertEqual(_page_display_name(page), label)
                self.assertIn(label, overview)
