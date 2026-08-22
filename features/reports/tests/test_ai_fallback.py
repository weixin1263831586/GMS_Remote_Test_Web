import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from features.reports import analysis_api, api_helpers
from features.reports.api_helpers import AnalysisMode
from features.reports.diagnosis_quality import (
    calibrate_ai_result,
    public_provider_error,
)


class ReportAiFallbackTests(unittest.TestCase):
    def test_ai_analysis_endpoint_redacts_unexpected_provider_exception(self):
        secret_error = RuntimeError(
            'glm_local failed Bearer provider-token api_key=provider-key'
        )
        with patch.object(
            analysis_api, "require_authenticated_user", return_value=SimpleNamespace()
        ), patch.object(analysis_api, "analyze_with_ai", side_effect=secret_error):
            response = asyncio.run(analysis_api.analyze_reports(
                request=SimpleNamespace(),
                mode=AnalysisMode.AI,
                test_name="ExampleTest#testFailure",
                error_message=None,
                stack_trace=None,
                module=None,
                class_names=None,
            ))

        payload = json.loads(response.body)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["data"]["ai_attempted"])
        for secret in ("provider-token", "provider-key"):
            self.assertNotIn(secret, response.body.decode("utf-8"))

    def test_rule_fallback_exposes_model_failure(self):
        analyzer = Mock()
        analyzer.get_local_provider.return_value = 'glm_local'
        analyzer.analyze_test_failure.return_value = {
            'success': False,
            'error': 'glm_local quota exceeded',
            'attempted_providers': ['glm_local', 'remote'],
        }
        previous = api_helpers.dependencies.universal_analyzer_factory
        api_helpers.dependencies.universal_analyzer_factory = lambda: analyzer
        try:
            result = api_helpers.analyze_with_ai(
                'ExampleTest#testFailure', 'java.lang.AssertionError'
            )
        finally:
            api_helpers.dependencies.universal_analyzer_factory = previous

        self.assertFalse(result['ai_enabled'])
        self.assertTrue(result['ai_attempted'])
        self.assertEqual(result['ai_error'], 'glm_local：本地模型额度已用尽。')
        self.assertEqual(result['root_cause_status'], 'hypothesis')
        self.assertFalse(result['root_cause_verified'])
        self.assertEqual(
            result['ai_providers_attempted'], ['glm_local', 'remote']
        )
        self.assertEqual(
            analyzer.analyze_test_failure.call_args.kwargs['preferred_provider'],
            'glm_local',
        )

    def test_successful_backup_provider_keeps_local_failure_context(self):
        analyzer = Mock()
        analyzer.get_local_provider.return_value = 'glm_local'
        analyzer.analyze_test_failure.return_value = {
            'success': True,
            'provider': 'zhipu',
            'preferred_provider': 'glm_local',
            'attempted_providers': ['glm_local', 'zhipu'],
            'fallback_used': True,
            'provider_errors': ['glm_local: quota exceeded'],
            'root_cause': 'AI diagnosis',
            'analysis': 'details',
            'suggestions': [],
        }
        previous = api_helpers.dependencies.universal_analyzer_factory
        api_helpers.dependencies.universal_analyzer_factory = lambda: analyzer
        try:
            with patch.object(
                api_helpers.config_manager,
                'get_ai_provider_config',
                return_value={'name': 'Backup AI'},
            ):
                result = api_helpers.analyze_with_ai(
                    'ExampleTest#testFailure', 'java.lang.AssertionError'
                )
        finally:
            api_helpers.dependencies.universal_analyzer_factory = previous

        self.assertTrue(result['ai_enabled'])
        self.assertTrue(result['ai_fallback_used'])
        self.assertEqual(result['ai_provider'], 'zhipu')
        self.assertEqual(result['ai_preferred_provider'], 'glm_local')
        self.assertEqual(
            result['ai_provider_errors'][0],
            'glm_local：本地模型额度已用尽。',
        )
        self.assertEqual(result['root_cause_status'], 'hypothesis')

    def test_litellm_quota_error_is_shortened_for_the_operator(self):
        raw = (
            'glm_local: glm_local API错误: ***.RateLimitError: '
            '已达到 5 小时的使用上限。您的限额将在 '
            '2026-07-16 19:08:49 重置。No fallback model group found. '
            'LiteLLM Retried: 2 times sk-secretvalue'
        )
        public = public_provider_error(raw)

        self.assertEqual(
            public,
            'glm_local：本地模型额度已用尽，预计 2026-07-16 19:08:49 恢复。',
        )
        self.assertNotIn('LiteLLM', public)
        self.assertNotIn('sk-secretvalue', public)

    def test_provider_error_redacts_embedded_credentials(self):
        raw = (
            'glm_local: unauthorized Bearer provider-token '
            'api_key=provider-key https://user:url-pass@example.test'
        )

        public = public_provider_error(raw)

        for secret in ('provider-token', 'provider-key', 'user:url-pass'):
            self.assertNotIn(secret, public)

    def test_ai_statement_is_calibrated_as_hypothesis_not_root_cause(self):
        result = calibrate_ai_result(
            {
                'root_cause': '🎯 待验证假设：自动填充应用被杀后未触发onSaveRequest()回调',
                'analysis': 'details',
                'ai_enabled': True,
            },
            'RetryableException: onSaveRequest() not called (timeout=20000ms)',
            '',
        )

        self.assertEqual(result['root_cause_status'], 'hypothesis')
        self.assertEqual(result['root_cause_confidence'], 'low')
        self.assertFalse(result['root_cause_verified'])
        self.assertTrue(result['root_cause'].startswith('待验证：'))
        self.assertNotIn('待验证：待验证', result['root_cause'])
        self.assertIn('onSaveRequest() not called', result['observed_failure'])
        self.assertIn('当前直接证据只证明', result['root_cause_note'])

        draft = api_helpers._build_patch_draft({'ai_result': result})
        self.assertIn('暂不生成代码补丁', draft)
        self.assertNotIn('--- a/', draft)


if __name__ == '__main__':
    unittest.main()
