import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bootstrap.application import create_app
from features.automation import api as automation_api
from features.automation.executors import HttpAutomationExecutor
from features.automation.profiles import save_profiles
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService


def build_service(
    root: str,
    *,
    profiles=None,
    gerrit_query=None,
) -> AutomationService:
    base = Path(root)
    profiles_path = base / 'profiles.json'
    if profiles is not None:
        save_profiles(profiles_path, profiles)
    return AutomationService(
        store=AutomationStore(base / 'automation.sqlite3'),
        profiles_path=profiles_path,
        gerrit_query=gerrit_query,
    )


class AutomationApiTests(unittest.TestCase):
    def test_create_list_get_and_events(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service):
                created = asyncio.run(
                    automation_api.create_automation_run(
                        {
                            'profile_id': 'manual-smoke',
                            'artifact_path': '/tmp/update.img',
                            'devices': ['ABC123'],
                            'test_plan': {'test_type': 'CTS'},
                        }
                    )
                )
                run_id = created['data']['id']
                listed = asyncio.run(
                    automation_api.list_automation_runs(status='', limit=50)
                )
                fetched = asyncio.run(
                    automation_api.get_automation_run(run_id)
                )
                events = asyncio.run(
                    automation_api.get_automation_run_events(run_id)
                )

            self.assertTrue(created['success'])
            self.assertEqual(created['data']['status'], 'queued')
            self.assertEqual(len(listed['data']['items']), 1)
            self.assertEqual(fetched['data']['id'], run_id)
            self.assertEqual(events['data']['items'][0]['stage'], 'queued')

    def test_worker_tick_advances_run(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service):
                created = asyncio.run(
                    automation_api.create_automation_run(
                        {
                            'profile_id': 'manual-smoke',
                            'artifact_path': '/tmp/update.img',
                            'devices': ['ABC123'],
                            'test_plan': {'test_type': 'CTS'},
                        }
                    )
                )
                ticked = asyncio.run(automation_api.automation_worker_tick())

            self.assertEqual(created['data']['status'], 'queued')
            self.assertEqual(ticked['data']['status'], 'waiting_device')

    def test_worker_tick_can_select_http_executor(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            orchestrator = service.orchestrator(executor_name='http')
        self.assertIsInstance(orchestrator.executor, HttpAutomationExecutor)

    def test_router_is_registered(self):
        paths = {route.path for route in create_app().routes}
        self.assertIn('/api/automation/runs', paths)
        self.assertIn('/automation', paths)

    def test_index_template_has_gms_ats_nav_entry(self):
        template = Path('web/shell/shell.html').read_text(
            encoding='utf-8'
        )
        self.assertIn('data-page="automation"', template)
        self.assertIn('id="page-automation"', template)
        self.assertIn('src="/automation"', template)
        self.assertIn("'automation': 'GMS ATS", template)

    def test_automation_page_exposes_workflow_controls(self):
        response = asyncio.run(automation_api.automation_page())
        html = response.body.decode('utf-8')
        self.assertIn(
            'Gerrit -> Jenkins -> 刷机 -> GMS 测试 -> 报告分析',
            html,
        )
        self.assertIn('id="automation-create-run"', html)
        self.assertIn('id="automation-runs"', html)
        self.assertIn('/api/automation/runs', html)
        self.assertIn('/api/automation/gerrit/poll', html)
        self.assertIn('/api/automation/worker/tick', html)

    def test_gerrit_webhook_creates_idempotent_runs(self):
        profiles = [
            {
                'id': 'rk3576-smoke',
                'name': 'RK3576 Smoke',
                'enabled': True,
                'gerrit': {
                    'project_regex': 'rk3576.*',
                    'branch_regex': 'master',
                },
                'jenkins': {'job': 'RK3576_ANDROID16'},
                'test_plan': {'test_type': 'CTS'},
            }
        ]
        payload = {
            'type': 'patchset-created',
            'change': {
                'project': 'rk3576_android16',
                'branch': 'master',
                'number': '123',
                'subject': 'Fix GMS',
                'owner': {'email': 'dev@rock-chips.com'},
            },
            'patchSet': {'number': '7', 'revision': 'abcdef'},
        }
        with TemporaryDirectory() as tmp:
            service = build_service(tmp, profiles=profiles)
            with patch.object(automation_api, 'automation_service', service):
                first = asyncio.run(
                    automation_api.handle_gerrit_webhook(payload)
                )
                second = asyncio.run(
                    automation_api.handle_gerrit_webhook(payload)
                )
            self.assertEqual(len(first['data']['created']), 1)
            self.assertEqual(len(second['data']['created']), 0)
            self.assertEqual(len(second['data']['existing']), 1)

    def test_gerrit_poll_creates_runs_from_query_results(self):
        profiles = [
            {
                'id': 'rk3576-smoke',
                'name': 'RK3576 Smoke',
                'enabled': True,
                'gerrit': {
                    'project_regex': 'rk3576.*',
                    'branch_regex': 'master',
                    'query': 'project:rk3576 status:open',
                },
                'jenkins': {'job': 'RK3576_ANDROID16'},
                'test_plan': {'test_type': 'CTS'},
            }
        ]
        changes = [
            {
                'project': 'rk3576_android16',
                'branch': 'master',
                'number': '123',
                'subject': 'Fix GMS',
                'owner': {'email': 'dev@rock-chips.com'},
                'current_revision': 'abcdef',
                'revisions': {'abcdef': {'_number': 7}},
            }
        ]

        async def query(_query, _limit):
            return changes

        with TemporaryDirectory() as tmp:
            service = build_service(
                tmp,
                profiles=profiles,
                gerrit_query=query,
            )
            with patch.object(automation_api, 'automation_service', service):
                result = asyncio.run(automation_api.poll_gerrit_changes())
            self.assertEqual(result['data']['created_count'], 1)
            self.assertEqual(
                service.store.list_runs(limit=10)[0]['gerrit_patchset'],
                '7',
            )

    def test_profile_crud_and_dry_run(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service):
                created = asyncio.run(
                    automation_api.save_automation_profile(
                        {
                            'id': 'p1',
                            'name': 'Profile 1',
                            'enabled': True,
                            'gerrit': {
                                'project_regex': 'rk3576.*',
                                'branch_regex': 'master',
                            },
                            'jenkins': {'job': 'J1'},
                            'test_plan': {'test_type': 'CTS'},
                        }
                    )
                )
                dry_run = asyncio.run(
                    automation_api.dry_run_automation_profile(
                        'p1',
                        {
                            'project': 'rk3576_android16',
                            'branch': 'master',
                            'change_id': '123',
                            'patchset': '7',
                        },
                    )
                )
            self.assertTrue(created['success'])
            self.assertTrue(dry_run['data']['matched'])
            self.assertEqual(dry_run['data']['run_request']['profile_id'], 'p1')
