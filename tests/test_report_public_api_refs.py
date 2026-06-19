import importlib
import unittest


class ReportPublicApiReferenceTests(unittest.TestCase):
    def test_agent_report_executor_references_resolve_to_feature_api(self):
        from features.assistant.tools import registry

        report_tools = registry.get_by_category("report")
        self.assertTrue(report_tools)

        references = [
            tool.executor_ref
            for tool in report_tools
            if tool.executor_ref
        ]
        self.assertTrue(references)

        for reference in references:
            module_name, function_name = reference.split(":", 1)
            self.assertEqual(module_name, "features.reports.api")
            module = importlib.import_module(module_name)
            self.assertTrue(callable(getattr(module, function_name)))


if __name__ == "__main__":
    unittest.main()
