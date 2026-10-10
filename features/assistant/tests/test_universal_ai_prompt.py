import unittest

from features.assistant.universal_ai import UniversalAIAnalyzer


class BuildPromptTests(unittest.TestCase):
    def test_json_example_interpolates_emoji_constants(self):
        prompt = UniversalAIAnalyzer({})._build_prompt(
            'MyTestClass', 'testMethod', 'AssertionError boom', None, None
        )

        self.assertIn('MyTestClass', prompt)
        self.assertIn('"analysis": "📊 详细分析', prompt)
        self.assertIn('"✅ 建议一：具体的修改步骤"', prompt)
        self.assertNotIn('EMOJI_CHART', prompt)
        self.assertNotIn('EMOJI_CHECK', prompt)

    def test_json_example_keeps_literal_braces(self):
        prompt = UniversalAIAnalyzer({})._build_prompt(
            'MyTestClass', None, 'AssertionError boom', None, None
        )

        self.assertIn('直接以 { 开始，以 } 结束', prompt)
        self.assertIn('"solution": {', prompt)
        self.assertIn('"root_cause"', prompt)


if __name__ == "__main__":
    unittest.main()
