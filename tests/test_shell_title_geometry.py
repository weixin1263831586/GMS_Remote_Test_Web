"""页面标题几何回归：主机桌面/主机终端左上角标题必须与其他页面对齐。

Desktop/Terminal 的页级标题走 `.page-header-row > .section-title` 公共
几何（显式 line-height:17.2px、标题盒顶对齐）；本回归保证今后调整主机
布局按钮（右侧 24px 控件）时不会把左侧标题推偏。
"""

from tests.test_runtime_ui_smoke import RuntimeUiHarness


class ShellTitleGeometryTests(RuntimeUiHarness):
    def test_host_workspace_titles_align_with_standard_pages(self):
        page = self.new_page()
        page.route("**/api/redmine-agent/**", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
            page.wait_for_function("typeof switchPage === 'function'")
            page.wait_for_timeout(400)
            page.evaluate("document.querySelectorAll('.modal').forEach(m => m.remove())")

            def title_offset(page_name: str) -> dict:
                return page.evaluate(
                    """(pageName) => {
                        const content = document.getElementById('page-' + pageName);
                        const title = content && content.querySelector('.section-title');
                        if (!content || !title) return null;
                        const contentTop = content.getBoundingClientRect().top;
                        const titleTop = title.getBoundingClientRect().top;
                        return {
                            offset: titleTop - contentTop,
                            lineHeight: getComputedStyle(title).lineHeight,
                        };
                    }""",
                    page_name,
                )

            offsets = {}
            for page_name in ("desktop", "terminal", "users", "reports"):
                page.evaluate("document.querySelectorAll('.modal').forEach(m => m.remove());"
                              f"switchPage('{page_name}')")
                page.wait_for_timeout(250)
                offsets[page_name] = title_offset(page_name)
            for page_name in ("desktop", "terminal", "users", "reports"):
                self.assertIsNotNone(offsets[page_name], f"{page_name} 缺少 .section-title")
            for page_name in ("desktop", "terminal"):
                # 允许 ≤1px 误差；修复前 host 页 offset 比标准页大约 2px。
                self.assertAlmostEqual(
                    offsets[page_name]["offset"], offsets["users"]["offset"], delta=1,
                    msg=f"{page_name} 标题相对页面顶部的偏移应与标准页一致",
                )
                self.assertAlmostEqual(
                    offsets[page_name]["lineHeight"], offsets["users"]["lineHeight"],
                    msg=f"{page_name} 标题行高应回归公共几何（17.2px）",
                )
        finally:
            page.close()
