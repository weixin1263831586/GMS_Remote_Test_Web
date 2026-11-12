"""对话 Agent 会话目录（多会话管理）运行时回归。

覆盖 GMS Agent 参考稿引入的三栏工作台：
- 会话列表侧栏：目录渲染、当前会话高亮、搜索过滤、切换、删除；
- 运行中会话禁止删除；导出空会话给出可见提示；
- 任务对话头部承载引擎/模型状态，与执行步骤头部保持 48px 等高；
- Controller 真实重启 + 页面强刷后对话记录保留（落盘恢复）。
"""

import json
import os
import subprocess
import time
from urllib.request import urlopen

from tests.runtime_ui.harness import REPO_ROOT, RuntimeUiHarness, expect


DIRECTORY_FIXTURE = [
    {
        "session_id": "sid-run", "title": "跑 CtsWifiTestCases 并 retry", "status": "running",
        "updated_at": "2026-11-12T03:05:00+00:00",
        "created_at": "2026-11-12T03:00:00+00:00", "active": True,
    },
    {
        "session_id": "sid-done", "title": "烧写固件并执行 AOA", "status": "done",
        "updated_at": "2026-11-12T03:00:00+00:00",
        "created_at": "2026-11-12T02:55:00+00:00", "active": False,
    },
    {
        "session_id": "sid-idle", "title": "分析最近报告", "status": "idle",
        "updated_at": "2026-11-12T01:00:00+00:00",
        "created_at": "2026-11-12T01:00:00+00:00", "active": False,
    },
]


class AgentSessionDirectoryTests(RuntimeUiHarness):
    def test_directory_render_search_switch_delete(self):
        page = self.new_page()
        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        deletes = []

        def directory(route):
            route.fulfill(content_type="application/json", body=json.dumps({
                "success": True, "data": {"sessions": DIRECTORY_FIXTURE}}))

        def session_detail(route):
            if route.request.method != "GET":
                route.fallback()
                return
            sid = route.request.url.rsplit("/", 1)[-1]
            route.fulfill(content_type="application/json", body=json.dumps({
                "success": True,
                "data": {"session": {"session_id": sid, "status": "idle",
                                     "messages": [], "steps": []}}}))

        def delete(route):
            if route.request.method != "DELETE":
                route.fallback()
                return
            deletes.append(route.request.url.rsplit("/", 1)[-1])
            route.fulfill(content_type="application/json", body=json.dumps({
                "success": True, "data": {"session_id": deletes[-1]}}))

        try:
            page.route("**/api/agent/sessions", directory)
            page.route("**/api/agent/sessions/sid-*", delete)
            page.route("**/api/agent/sessions/sid-*", session_detail)
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            expect(page.locator(".agent-session-item")).to_have_count(3)
            # 目录按 updated_at 倒序：running 会话最新在前。
            first_title = page.locator(".agent-session-item").first.locator(".agent-session-title")
            expect(first_title).to_have_text("跑 CtsWifiTestCases 并 retry")
            expect(page.locator(".agent-session-item.live")).to_have_count(1)
            expect(page.locator(".agent-session-item.live .agent-session-status")).to_have_text("运行中")

            # 搜索过滤
            page.locator("#agent-session-search").fill("AOA")
            expect(page.locator(".agent-session-item")).to_have_count(1)
            page.locator("#agent-session-search").fill("不存在")
            expect(page.locator(".agent-session-empty")).to_have_text("没有匹配的会话")
            page.locator("#agent-session-search").fill("")
            expect(page.locator(".agent-session-item")).to_have_count(3)

            # 切换会话：拉取详情并渲染（空会话），目录保持 3 条
            page.evaluate("void switchAgentSession('sid-done')")
            page.wait_for_timeout(500)
            expect(page.locator("#agent-chat-messages .agent-chat-empty")).to_be_visible()

            # 运行中的会话不允许删除：点击后只有警告提示，无 DELETE 请求
            page.evaluate("void deleteAgentSessionItem('sid-run')")
            expect(page.locator(".toast").last).to_contain_text("先取消")
            self.assertEqual(deletes, [])

            # 空会话导出：可见提示
            page.evaluate("exportAgentSessionTranscript()")
            expect(page.locator(".toast").last).to_contain_text("没有可导出")

            # 删除空闲会话：DELETE 命中、目录刷新、当前会话不受影响
            page.evaluate("void deleteAgentSessionItem('sid-idle')")
            expect(page.locator(".toast").last).to_contain_text("会话已删除")
            page.wait_for_timeout(400)
            self.assertEqual(deletes, ["sid-idle"])

            self.assertEqual(
                [e for e in console_errors if "act-bridge" in e], [],
                "会话目录控件不得出现 handler 解析失败")
        finally:
            page.close()

    def test_three_pane_headers_share_height(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.wait_for_timeout(400)
            heights = page.evaluate(
                "() => ['.agent-sessions-heading', '.agent-conversation-heading', "
                "'.agent-run-header'].map(s => "
                "document.querySelector(s).getBoundingClientRect().height)")
            self.assertEqual([round(h) for h in heights], [48, 48, 48])
        finally:
            page.close()

    def test_page_reload_keeps_recovered_conversation(self):
        """强刷回归：重启后 GET 返回恢复会话时，对话不得被重置为空。"""
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.wait_for_timeout(400)
            # 模拟 localStorage 里保存了一个已恢复的会话 ID，且服务端按
            # 恢复链路返回落盘对话记录（Controller 重启后的真实响应形态）。
            page.evaluate("""() => {
                localStorage.setItem('gms_agent_session_id', 'sid-recovered');
                agentSessionId = 'sid-recovered';
            }""")
            page.route("**/api/agent/sessions/sid-recovered", lambda route: route.fulfill(
                content_type="application/json", body=json.dumps({
                    "success": True,
                    "data": {"session": {
                        "session_id": "sid-recovered", "title": "重启前会话",
                        "status": "idle", "recovered": True,
                        "workspace_context": {},
                        "messages": [
                            {"id": "m1", "role": "user", "kind": "text",
                             "content": "跑 CtsWifiTestCases，失败 retry 2 次",
                             "created_at": "2026-11-12T03:00:10+00:00", "data": {}},
                            {"id": "m2", "role": "assistant", "kind": "text",
                             "content": "工作流已完成，报告已生成。",
                             "created_at": "2026-11-12T03:05:00+00:00", "data": {}},
                        ],
                        "steps": [],
                    }}})))
            page.evaluate("fetchAgentSession()")
            page.wait_for_timeout(400)
            # 关键断言：恢复的消息渲染出来，而不是被重置成空会话提示。
            expect(page.locator("#agent-chat-messages")).to_contain_text("跑 CtsWifiTestCases")
            expect(page.locator("#agent-chat-messages")).to_contain_text("工作流已完成")
            self.assertEqual(
                page.evaluate("localStorage.getItem('gms_agent_session_id')"), "sid-recovered")
        finally:
            page.close()


class AgentSessionRestartTests(RuntimeUiHarness):
    """真实服务重启回归：Controller 重启 + 页面强刷后对话保留。

    会话消息落盘（agent_transcripts/<sid>.json）后，重启 + 强刷不再把
    会话重置为空；目录里也能看到该会话。
    """

    def _restart_server(self):
        import subprocess as _subprocess

        self.server.terminate()
        try:
            self.server.wait(timeout=10)
        except _subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)
        env = os.environ.copy()
        env.update({
            "GMS_PORT": str(self.port),
            "GMS_DATA_ROOT": self.runtime_dir.name,
            "GMS_ENV": "development",
            "GMS_SKIP_RUNTIME_ENV": "1",
            "ATS_WORKER_ENABLED": "0",
            "GMS_ASSISTANT_KKAGENT_ENABLED": "0",
            "GMS_AUTH_REQUIRED": "true",
            "GMS_SECURE_COOKIES": "false",
            "UBUNTU_HOST": "127.0.0.1",
            "UBUNTU_USER": "ui-smoke",
        })
        self.server = _subprocess.Popen(
            ["python", "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
             "--port", str(self.port)],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.time() + 30
        last_error = None
        while time.time() < deadline:
            if self.server.poll() is not None:
                raise RuntimeError("restarted test server exited early")
            try:
                with urlopen(f"{self.base_url}/api/system/health", timeout=1) as response:
                    if response.status == 200:
                        return
            except Exception as exc:
                last_error = exc
                time.sleep(0.25)
        raise RuntimeError(f"restarted test server did not start: {last_error}")

    def test_conversation_survives_server_restart_and_reload(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.wait_for_timeout(400)
            page.locator("#agent-input").fill("重启恢复回归：跑 CtsWifiTestCases")
            page.locator(".agent-send-actions .btn-primary").click()
            expect(page.locator("#agent-chat-messages")).to_contain_text(
                "重启恢复回归：跑 CtsWifiTestCases", timeout=8000)
            expect(page.locator("#agent-chat-messages .agent-message-row.assistant")
                   ).to_have_count(1, timeout=15000)
            session_id = page.evaluate("agentSessionId")
            self.assertTrue(session_id)

            # 真实重启 Controller（同一数据目录），模拟生产部署重启。
            self._restart_server()

            # 强刷同一页面（保持 localStorage 与 cookie，等价浏览器 F5）。
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector(".sidebar-item[data-page]")
            self.close_initial_modals(page)
            page.evaluate("switchPage('agent', null)")
            page.wait_for_timeout(600)
            self.assertEqual(
                page.evaluate("localStorage.getItem('gms_agent_session_id')"),
                session_id, "强刷不得清掉本地会话 ID")
            expect(page.locator("#agent-chat-messages")).to_contain_text(
                "重启恢复回归：跑 CtsWifiTestCases", timeout=8000)
            expect(page.locator("#agent-chat-messages .agent-message-row.assistant")
                   ).to_have_count(1, timeout=8000)
            # 目录里能看到重启前的会话标题。
            expect(page.locator("#agent-session-list")).to_contain_text(
                "重启恢复回归", timeout=8000)
        finally:
            page.close()
