import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from bootstrap.application import create_app
from features.auth import CurrentUser
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
    @staticmethod
    def request_for(username: str, role: str = 'user') -> Request:
        request = Request({
            'type': 'http', 'method': 'GET', 'path': '/',
            'headers': [], 'client': ('127.0.0.1', 1234),
        })
        request.state.current_user = CurrentUser(
            id=f'id-{username}', username=username, role=role
        )
        return request

    def test_create_does_not_mutate_nested_request_when_removing_secret(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            request = {
                'artifact_path': '/tmp/update.img',
                'test_plan': {
                    'test_type': 'CTS',
                    'build': {'server_password': 'session-secret'},
                },
            }

            run = service.create_run(request)

            self.assertEqual(
                request['test_plan']['build']['server_password'],
                'session-secret',
            )
            self.assertNotIn('session-secret', run['test_plan_json'])

    def test_create_rejects_run_without_build_or_firmware(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service):
                response = asyncio.run(automation_api.create_automation_run({
                    'devices': ['ABC123'],
                    'test_plan': {'test_type': 'CTS'},
                }, self.request_for('alice')))

            self.assertEqual(response.status_code, 400)
            self.assertIn('Firmware artifact', response.body.decode())

    def test_build_password_survives_restart_and_retry_without_plaintext_storage(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            original = service.create_run({
                'profile_id': 'manual-smoke',
                'artifact_path': '/tmp/update.img',
                'devices': ['ABC123'],
                'test_plan': {'test_type': 'CTS'},
                'build_server_password': 'session-secret',
            })
            service.store.update_run(
                original['id'], status='completed', current_stage='completed'
            )

            restarted_service = build_service(tmp)
            self.assertEqual(
                restarted_service.get_build_password(original['id']),
                'session-secret',
            )

            retried = restarted_service.retry_run(original['id'])

            self.assertEqual(
                restarted_service.get_build_password(retried['id']),
                'session-secret',
            )
            self.assertNotIn('session-secret', retried['test_plan_json'])
            self.assertNotIn(
                b'session-secret',
                (Path(tmp) / 'automation.sqlite3').read_bytes(),
            )

    def test_retry_rejects_active_run(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            original = service.create_run({
                'artifact_path': '/tmp/update.img',
                'devices': ['ABC123'],
                'test_plan': {'test_type': 'CTS'},
            }, created_by='id-alice')
            with patch.object(automation_api, 'automation_service', service):
                response = asyncio.run(
                    automation_api.retry_automation_run(
                        original['id'], self.request_for('alice')
                    )
                )

            self.assertEqual(response.status_code, 409)
            self.assertIn('terminal automation runs', response.body.decode())

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
                        },
                        self.request_for('alice'),
                    )
                )
                run_id = created['data']['id']
                listed = asyncio.run(
                    automation_api.list_automation_runs(
                        self.request_for('alice'), status='', limit=50
                    )
                )
                fetched = asyncio.run(
                    automation_api.get_automation_run(
                        run_id, self.request_for('alice')
                    )
                )
                events = asyncio.run(
                    automation_api.get_automation_run_events(
                        run_id, self.request_for('alice')
                    )
                )

            self.assertTrue(created['success'])
            self.assertEqual(created['data']['status'], 'queued')
            self.assertEqual(len(listed['data']['items']), 1)
            self.assertEqual(fetched['data']['id'], run_id)
            self.assertEqual(events['data']['items'][0]['stage'], 'queued')

    def test_timeline_merges_automation_and_cluster_events_by_trace(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            run = service.create_run({
                'artifact_path': '/tmp/update.img',
                'devices': ['ABC123'],
                'test_plan': {'test_type': 'CTS'},
            }, created_by='id-alice')
            service.store.update_run(run['id'], cluster_job_id='job-1')
            repository = SimpleNamespace(list_timeline=lambda **_kwargs: [{
                'id': 10,
                'created_at': '2099-01-01T00:00:01Z',
                'event_type': 'job.transition',
                'message': 'Worker recovered',
                'trace_id': run['id'],
            }])
            with patch.object(automation_api, 'automation_service', service), patch.object(
                automation_api, 'get_cluster_service',
                return_value=SimpleNamespace(repository=repository),
            ):
                timeline = asyncio.run(
                    automation_api.get_automation_run_timeline(
                        run['id'], self.request_for('alice')
                    )
                )

            self.assertEqual(timeline['data']['trace_id'], run['id'])
            self.assertEqual(
                [item['domain'] for item in timeline['data']['items']],
                ['automation', 'cluster'],
            )

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
                        },
                        self.request_for('alice'),
                    )
                )
                ticked = asyncio.run(automation_api.automation_worker_tick('stub'))

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
        self.assertIn('/api/automation/runs/preflight', paths)
        self.assertIn('/automation', paths)

    def test_non_admin_cannot_read_another_users_run(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service):
                created = asyncio.run(automation_api.create_automation_run(
                    {
                        'artifact_path': '/tmp/update.img',
                        'devices': ['ABC'],
                        'test_plan': {'test_type': 'CTS'},
                    },
                    self.request_for('alice'),
                ))
                denied = asyncio.run(automation_api.get_automation_run(
                    created['data']['id'], self.request_for('bob')
                ))
                visible = asyncio.run(automation_api.list_automation_runs(
                    self.request_for('alice'), status='', limit=50
                ))

            self.assertEqual(denied.status_code, 404)
            self.assertEqual([item['id'] for item in visible['data']['items']], [created['data']['id']])

    def test_admin_created_run_is_owned_by_admin_not_global(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service):
                created = asyncio.run(automation_api.create_automation_run(
                    {
                        'artifact_path': '/tmp/update.img',
                        'devices': ['ABC'],
                        'test_plan': {'test_type': 'CTS'},
                    },
                    self.request_for('admin', role='admin'),
                ))

            self.assertEqual(created['data']['created_by'], 'id-admin')

    def test_live_preflight_resolves_worker_suite_and_devices(self):
        class Repository:
            @staticmethod
            def list_suites():
                return [{
                    'worker_id': 'worker-1', 'available': True,
                    'suite_type': 'CTS', 'suite_version': '17_r1',
                    'suite_key': 'CTS:17_r1', 'tools_path': '/suite/tools',
                    'last_scanned_at': '2026-07-13T00:00:00Z',
                }]

            @staticmethod
            def get_worker(worker_id):
                return {'id': worker_id, 'status': 'online'}

            @staticmethod
            def list_devices(worker_id):
                return [{
                    'id': f'{worker_id}:ABC', 'serial': 'ABC', 'state': 'available'
                }]

        cluster = SimpleNamespace(
            effective_enabled=True,
            repository=Repository(),
            has_command_agent=lambda _worker_id: True,
            list_workers=lambda: [{
                'id': 'worker-1', 'status': 'online',
                'running_jobs': 0, 'max_jobs': 1,
                'disk_free_gb': 100, 'memory_available_gb': 32,
            }],
        )
        with TemporaryDirectory() as tmp:
            service = AutomationService(
                store=AutomationStore(Path(tmp) / 'automation.sqlite3'),
                profiles_path=Path(tmp) / 'profiles.json',
                cluster_provider=lambda: cluster,
            )
            result = service.preflight({
                'devices': ['ABC'],
                'test_plan': {
                    'worker_id': 'worker-1', 'test_type': 'CTS',
                    'flash': {'mode': 'skip'},
                },
            })

        self.assertTrue(result['ready'])
        self.assertTrue(result['runtime_checked'])
        self.assertEqual(result['worker_id'], 'worker-1')
        self.assertEqual(result['test_suite'], '/suite/tools')
        self.assertEqual(result['devices'], ['worker-1:ABC'])

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
            'Gerrit → 构建 → 刷机 → GMS 测试 → 报告',
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
            with patch.object(automation_api, 'automation_service', service), patch.dict(
                'os.environ', {
                    'GMS_AUTOMATION_WEBHOOK_TOKEN': 'test-webhook-token',
                    'GMS_AUTOMATION_OWNER_ID': 'service-automation',
                }
            ):
                first = asyncio.run(
                    automation_api.handle_gerrit_webhook(
                        payload, automation_token='test-webhook-token'
                    )
                )
                second = asyncio.run(
                    automation_api.handle_gerrit_webhook(
                        payload, automation_token='test-webhook-token'
                    )
                )
            self.assertEqual(len(first['data']['created']), 1)
            self.assertEqual(len(second['data']['created']), 0)
            self.assertEqual(len(second['data']['existing']), 1)
            self.assertEqual(
                first['data']['created'][0]['created_by'],
                'service-automation',
            )

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

        async def query(owner_id, _query, _limit):
            self.assertEqual(owner_id, 'id-admin')
            return changes

        with TemporaryDirectory() as tmp:
            service = build_service(
                tmp,
                profiles=profiles,
                gerrit_query=query,
            )
            with patch.object(automation_api, 'automation_service', service):
                result = asyncio.run(
                    automation_api.poll_gerrit_changes(
                        admin=self.request_for(
                            'admin', role='admin'
                        ).state.current_user
                    )
                )
            self.assertEqual(result['data']['created_count'], 1)
            self.assertEqual(result['data']['rejected_count'], 0)
            self.assertEqual(
                service.store.list_runs(limit=10)[0]['gerrit_patchset'],
                '7',
            )

    def test_gerrit_poll_reports_preflight_rejections(self):
        profiles = [{
            'id': 'invalid-no-build',
            'enabled': True,
            'gerrit': {'query': 'status:open'},
            'test_plan': {'test_type': 'CTS'},
        }]

        async def query(_owner_id, _query, _limit):
            return [{
                'project': 'android',
                'branch': 'main',
                'number': '123',
                'current_revision': 'abcdef',
                'revisions': {'abcdef': {'_number': 1}},
            }]

        with TemporaryDirectory() as tmp:
            service = build_service(tmp, profiles=profiles, gerrit_query=query)
            result = asyncio.run(
                service.poll_gerrit_changes(created_by='service-automation')
            )

        self.assertEqual(result['created_count'], 0)
        self.assertEqual(result['rejected_count'], 1)
        self.assertIn('Firmware artifact', result['rejected'][0]['error'])

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
