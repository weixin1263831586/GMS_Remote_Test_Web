import json
import unittest
from unittest.mock import patch

from features.automation.notifier import notify_run_completion


class AutomationNotifierTests(unittest.TestCase):
    def test_invalid_test_plan_is_treated_as_unconfigured(self):
        result = notify_run_completion({'test_plan_json': '{invalid'})

        self.assertEqual(result, {'sent': [], 'reason': 'no reporting config'})

    def test_email_transport_uses_public_feature_api(self):
        run = {
            'profile_id': 'gms-main',
            'status': 'completed',
            'test_plan_json': json.dumps(
                {'reporting': {'email_to': 'owner@example.com', 'transports': ['email']}}
            ),
        }
        with patch('features.email.send_email', return_value={'sent': True, 'mode': 'smtp'}) as sender:
            result = notify_run_completion(run)

        self.assertTrue(result['sent'][0]['sent'])
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0], 'owner@example.com')
        self.assertIsNotNone(sender.call_args.kwargs['manager'])

    def test_unknown_transport_is_reported_without_raising(self):
        run = {
            'test_plan_json': json.dumps(
                {'reporting': {'transports': ['pager'], 'email_to': 'owner@example.com'}}
            )
        }

        result = notify_run_completion(run)

        self.assertEqual(
            result['sent'],
            [{'transport': 'pager', 'sent': False, 'reason': 'unknown transport'}],
        )


if __name__ == '__main__':
    unittest.main()
