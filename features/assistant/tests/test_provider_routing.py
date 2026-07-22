import unittest
from unittest.mock import patch

from features.assistant.universal_ai import UniversalAIAnalyzer


class ProviderRoutingTests(unittest.TestCase):
    def test_failure_analysis_prefers_local_then_fails_over(self):
        analyzer = UniversalAIAnalyzer({
            'enabled': True,
            'primary_provider': 'remote',
            'providers': {
                'glm_local': {
                    'enabled': True,
                    'base_url': 'http://172.16.0.2:3000',
                    'model': 'local-model',
                },
                'remote': {
                    'enabled': True,
                    'base_url': 'https://example.invalid',
                    'model': 'remote-model',
                },
            },
        })

        def call(provider, *_args):
            if provider == 'glm_local':
                return {'success': False, 'error': 'quota exceeded'}
            return {
                'success': True,
                'root_cause': 'remote diagnosis',
                'analysis': 'details',
                'suggestions': ['fix'],
            }

        with patch.object(analyzer, '_call_aimodel', side_effect=call) as request:
            result = analyzer.analyze_test_failure(
                'ExampleTest', 'testFailure', 'assertion failed',
                auto_fetch_source=False,
                preferred_provider='glm_local',
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['provider'], 'remote')
        self.assertTrue(result['fallback_used'])
        self.assertIn('quota exceeded', result['provider_errors'][0])
        self.assertEqual(
            [item.args[0] for item in request.call_args_list],
            ['glm_local', 'remote'],
        )


if __name__ == '__main__':
    unittest.main()
