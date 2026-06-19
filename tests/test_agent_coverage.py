import importlib
import unittest

from features.assistant.executor import ActionExecutor
from features.assistant.tools import registry
from features.system import API_DOCS_LIST


class AgentCoverageTests(unittest.TestCase):
    def test_agent_registry_covers_documented_api_paths(self):
        documented_paths = {
            item["path"]
            for item in API_DOCS_LIST
            if str(item.get("path", "")).startswith("/api/") or item.get("path") == "/"
        }
        registered_paths = {tool.api_path for tool in registry.get_all_tools()}

        self.assertEqual(sorted(documented_paths - registered_paths), [])

    def test_agent_executor_refs_resolve_after_refactor(self):
        missing = []
        for tool in registry.get_all_tools():
            if not tool.executor_ref:
                continue
            module_path, func_name = tool.executor_ref.rsplit(":", 1)
            try:
                module = importlib.import_module(module_path)
                getattr(module, func_name)
            except (ImportError, AttributeError) as exc:
                missing.append(f"{tool.name}: {tool.executor_ref} ({exc})")

        self.assertEqual(missing, [])

    def test_tools_without_executor_ref_are_handled_or_explicitly_page_only(self):
        executor = ActionExecutor()
        page_only_tools = {
            "home",
            "system_websocket_{client_id}",
            "test_logs_stream",
            "architecture",
        }
        unhandled = []
        for tool in registry.get_all_tools():
            if tool.executor_ref or tool.name in executor._handlers or tool.name in page_only_tools:
                continue
            unhandled.append(f"{tool.name}: {tool.api_path}")

        self.assertEqual(unhandled, [])


if __name__ == "__main__":
    unittest.main()
