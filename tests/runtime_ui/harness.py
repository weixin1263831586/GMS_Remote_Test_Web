import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import urlopen


try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except Exception:  # pragma: no cover - exercised only when Playwright is unavailable
    PlaywrightError = Exception
    expect = None
    sync_playwright = None


REPO_ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RuntimeUiHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is not installed")
        cls.port = free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        runtime_parent = "/dev/shm" if Path("/dev/shm").is_dir() else None
        cls.runtime_dir = tempfile.TemporaryDirectory(dir=runtime_parent)
        env = os.environ.copy()
        env["GMS_PORT"] = str(cls.port)
        env["GMS_DATA_ROOT"] = cls.runtime_dir.name
        env["GMS_ENV"] = "development"
        env["GMS_SKIP_RUNTIME_ENV"] = "1"
        env["ATS_WORKER_ENABLED"] = "0"
        env["GMS_AUTH_REQUIRED"] = "true"
        env["GMS_SECURE_COOKIES"] = "false"
        # 终端 E2E 需要确定性的本地 PTY 通道：不设置时配置回退到
        # config.example.json 的 192.0.2.10（TEST-NET），CI 上会走真实
        # SSH 分支并连接失败，terminal 用例集体超时。127.0.0.1 属于
        # _LOCAL_HOSTS，终端服务进入 local PTY 模式，与主机环境无关。
        env["UBUNTU_HOST"] = "127.0.0.1"
        env["UBUNTU_USER"] = "ui-smoke"
        cls.server = subprocess.Popen(
            ["python", "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        cls.server_output = []

        def drain_server_output():
            if cls.server.stdout:
                for line in cls.server.stdout:
                    cls.server_output.append(line)

        cls.server_output_thread = threading.Thread(
            target=drain_server_output,
            name="runtime-ui-server-output",
            daemon=True,
        )
        cls.server_output_thread.start()
        deadline = time.time() + 30
        last_error = None
        while time.time() < deadline:
            if cls.server.poll() is not None:
                output = "".join(cls.server_output)
                raise RuntimeError(f"test server exited early:\n{output}")
            try:
                with urlopen(f"{cls.base_url}/api/system/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception as exc:
                last_error = exc
                time.sleep(0.25)
        else:
            raise RuntimeError(f"test server did not start: {last_error}")

        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            cls.tearDownClass()
            raise unittest.SkipTest(f"Playwright browser is unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        browser = getattr(cls, "browser", None)
        if browser:
            browser.close()
        playwright = getattr(cls, "playwright", None)
        if playwright:
            playwright.stop()
        server = getattr(cls, "server", None)
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        output_thread = getattr(cls, "server_output_thread", None)
        if output_thread:
            output_thread.join(timeout=2)
        if server and server.stdout:
            server.stdout.close()
        runtime_dir = getattr(cls, "runtime_dir", None)
        if runtime_dir:
            runtime_dir.cleanup()

    def new_page(self):
        # Runtime smoke tests intentionally execute helper expressions in the
        # page context.  Keep the production CSP strict while allowing that
        # Playwright-only instrumentation in this isolated browser context.
        page = self.browser.new_page(
            viewport={"width": 1440, "height": 960},
            bypass_csp=True,
        )
        page.set_default_timeout(8000)
        page.set_default_navigation_timeout(15000)
        status = page.request.get(f"{self.base_url}/api/auth/status")
        self.assertTrue(status.ok, status.text())
        endpoint = "setup" if status.json().get("setup_required") else "login"
        authenticated = page.request.post(
            f"{self.base_url}/api/auth/{endpoint}",
            data={
                "username": "ui-admin",
                "password": "UiSmokeAdmin-2026!",
                "display_name": "UI Smoke Admin",
            },
        )
        self.assertTrue(authenticated.ok, authenticated.text())
        page.route(
            "**/api/websites/load",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"tools":{},"last_updated":null}',
            ),
        )
        page.route(
            "**/api/websites/save",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true}',
            ),
        )
        page.route(
            "**/api/websites/sync",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"tools":{},"last_updated":null}',
            ),
        )
        page.add_init_script(
            """
            try {
                localStorage.setItem('gms_sidebar_visible_pages', JSON.stringify([
                    'test', 'desktop', 'terminal', 'users', 'devices', 'devices-console', 'reports',
                    'report-analysis', 'test-suites', 'apk-analysis', 'security-audit',
                    'api-docs', 'architecture', 'websites', 'tools', 'gms-assistant',
                    'automation', 'cluster', 'redmine-agent', 'gerrit-dashboard', 'agent', 'notes'
                ]));
            } catch (_error) {
                // Init scripts also run in transient/opaque child frames where
                // storage access is intentionally unavailable.
            }
            """
        )
        return page

    def close_initial_modals(self, page):
        # 初始窗口内晚到的自动弹框（如客户端身份识别 500ms 延迟弹出）
        # 最多等 2s；分层弹框（登录层之上再叠 modal）时 Escape 每次只关
        # 一层，最多按 5 次。全部关不掉视为泄漏，断言失败。
        for _ in range(10):
            if page.locator(".modal.show").count():
                for _ in range(5):
                    if not page.locator(".modal.show").count():
                        break
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(150)
                expect(page.locator(".modal.show")).to_have_count(0)
                return
            page.wait_for_timeout(200)

    def show_all_sidebar_pages(self, page):
        page.evaluate(
            """
            if (typeof applySidebarVisibility === 'function') {
                applySidebarVisibility([
                    'test', 'desktop', 'terminal', 'users', 'devices', 'devices-console', 'reports',
                    'report-analysis', 'test-suites', 'apk-analysis', 'security-audit',
                    'api-docs', 'architecture', 'websites', 'tools', 'gms-assistant',
                    'automation', 'cluster', 'redmine-agent', 'gerrit-dashboard', 'agent', 'notes'
                ]);
            }
            """
        )

    def visible_sidebar_pages(self, page):
        return page.locator(".sidebar-item[data-page]").evaluate_all(
            "(items) => items.map(item => item.dataset.page)"
        )

    def goto_shell(self, page):
        page.goto(self.base_url, wait_until="domcontentloaded")
        page.wait_for_selector(".sidebar-item[data-page]")
        self.close_initial_modals(page)
        self.show_all_sidebar_pages(page)

    def frame_for(self, page, selector):
        handle = page.locator(selector).element_handle()
        self.assertIsNotNone(handle, f"missing iframe: {selector}")
        frame = handle.content_frame()
        self.assertIsNotNone(frame, f"iframe not loaded: {selector}")
        return frame

    def assert_no_page_errors(self, page_errors):
        self.assertEqual(page_errors, [])

    def press_escape_in_frame(self, frame):
        frame.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}))")

    def assert_frame_modal_closes_with_escape(self, frame, open_script, modal_selector):
        function_name = open_script.split("(", 1)[0]
        frame.wait_for_function(f"typeof {function_name} === 'function'")
        frame.evaluate(open_script)
        expect(frame.locator(modal_selector)).to_have_class(re.compile(r"show"))
        self.press_escape_in_frame(frame)
        expect(frame.locator(modal_selector)).not_to_have_class(re.compile(r"show"))
