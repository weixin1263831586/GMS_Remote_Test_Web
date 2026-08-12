import unittest
from unittest.mock import patch

from features.assistant.universal_ai import UniversalAIAnalyzer


class ProviderRoutingTests(unittest.TestCase):
    def test_provider_status_distinguishes_enabled_from_ready_and_available(self):
        analyzer = UniversalAIAnalyzer({
            'enabled': True,
            'primary_provider': 'remote',
            'providers': {
                'private_model': {
                    'name': 'Private model',
                    'enabled': True,
                    'base_url': 'http://172.31.8.4:3000',
                    'model': 'local-model',
                    'api_key': '',
                },
                'remote': {
                    'enabled': True,
                    'base_url': 'https://example.invalid',
                    'model': 'remote-model',
                    'api_key': 'configured-token',
                },
            },
        })

        with patch.dict('os.environ', {'GMS_LOCAL_AI_API_KEY': ''}, clear=False):
            statuses = {
                item['provider']: item for item in analyzer.get_provider_statuses()
            }

        self.assertTrue(statuses['private_model']['local'])
        self.assertEqual(statuses['private_model']['state'], 'credential_missing')
        self.assertFalse(statuses['private_model']['credential_configured'])
        self.assertIsNone(statuses['private_model']['available'])
        self.assertEqual(statuses['remote']['state'], 'ready_to_probe')

    def test_probe_checks_only_requested_provider_without_fallback(self):
        analyzer = UniversalAIAnalyzer({
            'enabled': True,
            'providers': {
                'glm_local': {
                    'enabled': True,
                    'base_url': 'http://127.0.0.1:3000',
                    'model': 'local-model',
                    'auth_required': False,
                },
                'remote': {
                    'enabled': True,
                    'base_url': 'https://example.invalid',
                    'model': 'remote-model',
                    'api_key': 'configured-token',
                },
            },
        })

        with patch.object(
            analyzer,
            '_generate_with_provider',
            return_value={'success': True, 'content': 'OK'},
        ) as request:
            result = analyzer.probe_provider('glm_local')

        self.assertTrue(result['available'])
        self.assertEqual(result['state'], 'available')
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], 'glm_local')

    def test_generate_prefers_local_then_fails_over(self):
        analyzer = UniversalAIAnalyzer({
            'enabled': True,
            'providers': {
                'glm_local': {'enabled': True},
                'remote': {'enabled': True},
            },
        })

        def call(provider, *_args):
            if provider == 'glm_local':
                return {'success': False, 'error': 'quota exceeded'}
            return {'success': True, 'content': 'fallback response'}

        with patch.object(
            analyzer,
            '_generate_with_provider',
            side_effect=call,
        ) as request:
            result = analyzer.generate(
                'hello',
                preferred_provider='glm_local',
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['provider'], 'remote')
        self.assertEqual(result['content'], 'fallback response')
        self.assertTrue(result['fallback_used'])
        self.assertEqual(result['attempted_providers'], ['glm_local', 'remote'])
        self.assertIn('quota exceeded', result['provider_errors'][0])
        self.assertEqual(
            [item.args[0] for item in request.call_args_list],
            ['glm_local', 'remote'],
        )

    def test_generate_does_not_report_untried_backup_provider(self):
        analyzer = UniversalAIAnalyzer({
            'enabled': True,
            'providers': {
                'glm_local': {'enabled': True},
                'remote': {'enabled': True},
            },
        })

        with patch.object(
            analyzer,
            '_generate_with_provider',
            return_value={'success': True, 'content': 'local response'},
        ) as request:
            result = analyzer.generate('hello', preferred_provider='glm_local')

        self.assertTrue(result['success'])
        self.assertEqual(result['attempted_providers'], ['glm_local'])
        request.assert_called_once()

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
