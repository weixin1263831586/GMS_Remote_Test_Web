import re
import unittest
from pathlib import Path


CALL_ATTR_RE = re.compile(r'on(?:click|change|input|submit|keydown|mouseover|mouseout)=["\']([^"\']+)["\']')
FUNCTION_RE = re.compile(r'\bfunction\s+([A-Za-z_$][\w$]*)\s*\(')
WINDOW_ASSIGN_RE = re.compile(r'\bwindow\.([A-Za-z_$][\w$]*)\s*=')
CONST_FUNCTION_RE = re.compile(
    r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*'
    r'(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)'
)
BUILTINS = {
    "Array",
    "Boolean",
    "Date",
    "JSON",
    "Math",
    "Number",
    "Object",
    "Promise",
    "String",
    "alert",
    "clearTimeout",
    "confirm",
    "console",
    "decodeURIComponent",
    "document",
    "encodeURIComponent",
    "event",
    "fetch",
    "if",
    "navigator",
    "setTimeout",
    "this",
    "window",
}


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def declared_functions(text: str) -> set[str]:
    return (
        set(FUNCTION_RE.findall(text))
        | set(WINDOW_ASSIGN_RE.findall(text))
        | set(CONST_FUNCTION_RE.findall(text))
        | BUILTINS
    )


def inline_handler_calls(text: str) -> list[tuple[str, str]]:
    calls = []
    for body in CALL_ATTR_RE.findall(text):
        for name in re.findall(r'(?<![\.\w$])([A-Za-z_$][\w$]*)\s*\(', body):
            if name not in BUILTINS:
                calls.append((name, body))
    return calls


class FrontendIntegrityTests(unittest.TestCase):
    def test_main_app_inline_handlers_resolve_to_global_functions(self):
        main_text = read_text("templates/index_fastapi.html")
        script_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in Path("static/js").glob("*.js"))
        combined = main_text + "\n" + script_text
        funcs = declared_functions(combined)

        missing = sorted({f"{name}: {body}" for name, body in inline_handler_calls(main_text) if name not in funcs})

        self.assertEqual(missing, [])

    def test_embedded_dashboard_inline_handlers_resolve_locally(self):
        for label, paths in [
            ("redmine", ["routers/redmine_agent.py"]),
            ("gerrit", ["routers/gerrit_dashboard.py"]),
            ("update-monitor", ["routers/gms_update_monitor.py"]),
            ("mainline", ["routers/mainline_known_issues.py"]),
            (
                "automation",
                [
                    "features/automation/ui/page.html",
                    "features/automation/ui/page.js",
                ],
            ),
        ]:
            with self.subTest(page=label):
                text = "\n".join(read_text(path) for path in paths)
                funcs = declared_functions(text)
                missing = sorted({f"{name}: {body}" for name, body in inline_handler_calls(text) if name not in funcs})

                self.assertEqual(missing, [])

    def test_modal_pages_support_escape_close(self):
        for path in [
            "templates/index_fastapi.html",
            "routers/redmine_agent.py",
            "routers/gerrit_dashboard.py",
        ]:
            with self.subTest(path=path):
                text = read_text(path)
                self.assertIn('class="modal', text)
                self.assertTrue("Escape" in text or "ModalManager" in text)

    def test_user_facing_result_prompts_avoid_blocking_alerts(self):
        main_text = read_text("templates/index_fastapi.html")
        self.assertNotIn("alert(", main_text)
        self.assertIn("gms-dashboard-notification", main_text)
        self.assertIn("redmine-agent-notification", main_text)
        self.assertIn("gms-update-monitor-notification", main_text)

        for path in ["routers/redmine_agent.py", "routers/gerrit_dashboard.py", "routers/gms_update_monitor.py"]:
            with self.subTest(path=path):
                text = read_text(path)
                self.assertIn("function notifyUser", text)
                self.assertIn("postMessage", text)
                self.assertNotIn("alert(", text)

        self.assertNotIn("_sendParentNotification", read_text("routers/redmine_agent.py"))

    def test_modal_ids_and_function_declarations_are_not_duplicated(self):
        checked_paths = [
            "templates/index_fastapi.html",
            "routers/redmine_agent.py",
            "routers/gerrit_dashboard.py",
            "routers/gms_update_monitor.py",
            "routers/mainline_known_issues.py",
            "features/automation/ui/page.html",
            "features/automation/ui/page.js",
            *[str(path) for path in Path("static/js").glob("*.js")],
        ]
        for path in checked_paths:
            with self.subTest(path=path):
                text = read_text(path)
                modal_ids = re.findall(r'<[^>]+id=["\']([^"\']+)["\'][^>]+class=["\'][^"\']*\bmodal\b', text)
                duplicate_modal_ids = sorted({item for item in modal_ids if modal_ids.count(item) > 1})
                self.assertEqual(duplicate_modal_ids, [])

                function_names = FUNCTION_RE.findall(text)
                duplicate_functions = sorted({item for item in function_names if function_names.count(item) > 1})
                self.assertEqual(duplicate_functions, [])
