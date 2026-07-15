import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_gerrit_transport_posts_comment_and_verified_vote(self):
        run = {
            'created_by': 'alice',
            'status': 'completed',
            'gerrit_change_id': '123',
            'gerrit_patchset': '7',
            'test_plan_json': json.dumps({'reporting': {
                'transports': ['gerrit'],
                'gerrit_comment': True,
                'gerrit_verified_label': True,
            }}),
        }
        manager = MagicMock()
        manager.for_owner.return_value.get_gerrit_dashboard_config.return_value = {
            'base_url': 'https://gerrit.example.com'
        }
        sender = AsyncMock(return_value={'sent': True, 'source': 'rest'})
        with patch('features.gerrit.gerrit_config_manager', manager), patch(
            'features.gerrit.post_gerrit_review', sender
        ):
            result = notify_run_completion(run)

        self.assertTrue(result['sent'][0]['sent'])
        self.assertEqual(sender.await_args.kwargs['change_id'], '123')
        self.assertEqual(sender.await_args.kwargs['patchset'], '7')
        self.assertEqual(sender.await_args.kwargs['verified'], 1)

    def test_redmine_transport_writes_note_with_owner_credentials(self):
        run = {
            'id': 'ats-1',
            'created_by': 'alice',
            'status': 'completed',
            'test_plan_json': json.dumps({
                'redmine_issue_id': '456',
                'reporting': {'transports': ['redmine']},
            }),
        }
        owner_manager = MagicMock()
        owner_manager.get_redmine_config.return_value = {
            'base_url': 'https://redmine.example.com'
        }
        owner_manager.load_redmine_credentials.return_value = {
            'username': 'alice', 'password': 'secret'
        }
        manager = MagicMock()
        manager.for_owner.return_value = owner_manager
        client = MagicMock()
        client.update_issue = AsyncMock()
        client.close = AsyncMock()
        with patch('features.redmine.config_manager', manager), patch(
            'features.redmine.RedmineClient', return_value=client
        ):
            result = notify_run_completion(run)

        self.assertTrue(result['sent'][0]['sent'])
        client.update_issue.assert_awaited_once()
        self.assertEqual(client.update_issue.await_args.args[0], '456')
        self.assertIn('GMS ATS', client.update_issue.await_args.kwargs['notes'])

    def test_required_transport_failure_is_explicit(self):
        run = {
            'test_plan_json': json.dumps({'reporting': {
                'transports': ['pager'], 'required': True,
            }}),
        }

        result = notify_run_completion(run)

        self.assertFalse(result['ok'])
        self.assertEqual(result['failed_required'], ['pager'])


if __name__ == '__main__':
    unittest.main()
