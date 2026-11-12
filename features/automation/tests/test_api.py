import asyncio
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from bootstrap.application import create_app
from features.auth import AUTOMATION_PLAN_CAPABILITIES, CurrentUser, MachineAuthorityError
from features.automation import api as automation_api
from features.automation.executors import HttpAutomationExecutor
from features.automation.profiles import save_profiles
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService
from tests.contract.snapshot_tools import read_page_bundle, read_shell_bundle


@contextmanager
def cluster_port_snapshot():
    """create_app() 会向 foundation.cluster_port 注册进程级 provider。

    不恢复快照会把真实 cluster 服务泄漏给同进程后续测试（如 devices
    心跳分支读到真库），造成跨套件顺序依赖；这里在装配后还原注册态。
    """
    import foundation.cluster_port as port

    names = (
        '_cluster_service_provider', '_cancel_job', '_worker_tokens',
        '_run_worker_command', '_require_worker_session',
        '_require_cluster_enabled', '_authenticate_worker',
        '_device_action_request',
    )
    saved = {name: getattr(port, name) for name in names}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(port, name, value)


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


def online_worker_cluster(repository, **extra) -> SimpleNamespace:
    """预检用在线 worker 集群桩（三处 preflight 测试共享）。"""
    return SimpleNamespace(
        effective_enabled=True,
        repository=repository,
        has_command_agent=lambda _worker_id: True,
        list_workers=lambda: [{
            'id': 'worker-1', 'status': 'online',
            'running_jobs': 0, 'max_jobs': 1,
            'disk_free_gb': 100, 'memory_available_gb': 32,
        }],
        **extra,
    )


class AutomationApiTests(unittest.TestCase):
    @staticmethod
    def request_for(username: str, role: str = 'user', extra_permissions: frozenset[str] = frozenset()) -> Request:
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'client': ('127.0.0.1', 1234)})
        if role == 'device_operator' and not extra_permissions:
            # 缺省 flash 即烧写（ADR 0012）：device_operator 需要完整 plan 能力并集；显式传入 extra_permissions 时不覆盖。
            extra_permissions = frozenset(AUTOMATION_PLAN_CAPABILITIES)
        request.state.current_user = CurrentUser(
            id=f'id-{username}', username=username, role=role, extra_permissions=extra_permissions,
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

    def test_retry_reauthorizes_from_current_principal_not_old_snapshot(self):
        # 审核回归：Retry 是一次新的执行授权。提权会话创建的 Run 在提权
        # 过期（或权限收回）后，Retry 必须按当前主体重新计算能力，不得
        # 沿用旧 Run 的 granted_capabilities 快照。
        plan = {
            'artifact_path': '/tmp/update.img',
            'devices': ['ABC123'],
            'test_plan': {'test_type': 'CTS'},
        }
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            elevated_admin = self.request_for('alice', role='client')
            elevated_admin.state.is_elevated = True
            principal = elevated_admin.state.current_user
            original = service.create_run(plan, created_by='id-alice', principal=principal, elevated=True)
            # 该计划无 build 阶段，能力并集不含 build.*；提权授予其余全部
            expected_caps = [c for c in AUTOMATION_PLAN_CAPABILITIES if not c.startswith('build.')]
            self.assertEqual(original['granted_capabilities'], ','.join(expected_caps))
            service.store.update_run(original['id'], status='completed', current_stage='completed')

            with patch.object(automation_api, 'automation_service', service):
                # 提权过期后（同一普通用户、无提权）：403 且不产生新 Run
                expired = self.request_for('alice')
                response = asyncio.run(
                    automation_api.retry_automation_run(original['id'], expired)
                )
                self.assertEqual(response.status_code, 403)
                self.assertIn('does not hold', response.body.decode())
                self.assertEqual(
                    [run['id'] for run in service.list_run_summaries(limit=10) if run['id'] != original['id']],
                    [],
                )

                # 重新提权后可 Retry，新 Run 能力由当前提权重新授予
                re_elevated = self.request_for('alice')
                re_elevated.state.is_elevated = True
                retried = asyncio.run(
                    automation_api.retry_automation_run(original['id'], re_elevated)
                )
                self.assertEqual(retried['data']['retry_of_run_id'], original['id'])
                self.assertEqual(
                    retried['data']['granted_capabilities'],
                    ','.join(expected_caps),
                )

    def test_retry_rejects_machine_principal_at_service_layer(self):
        # 审核回归：人类身份约束由 Service 公共方法统一执行（与 create_run
        # 同一卡点），不依赖每个调用方记得先过 Web 路由检查。
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            original = service.create_run({
                'artifact_path': '/tmp/update.img',
                'devices': ['ABC123'],
                'test_plan': {'test_type': 'CTS'},
            }, created_by='id-alice')
            service.store.update_run(
                original['id'], status='completed', current_stage='completed'
            )
            agent = CurrentUser(id='id-agent', username='agent-bot', role='agent')
            with self.assertRaises(MachineAuthorityError):
                service.retry_run(original['id'], agent)
            # 被拒绝的 Retry 不得留下新 Run
            self.assertEqual(
                [r['id'] for r in service.list_run_summaries(limit=10)
                 if r['id'] != original['id']],
                [],
            )

    def test_retry_records_requested_by_while_keeping_original_owner(self):
        # 审核回归：管理员代其他用户 Retry 时，资源归属（created_by）保持
        # 原用户，但新 Run 必须单独记录实际操作者（requested_by）。
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            original = service.create_run({
                'artifact_path': '/tmp/update.img',
                'devices': ['ABC123'],
                'test_plan': {'test_type': 'CTS'},
            }, created_by='id-alice')
            service.store.update_run(
                original['id'], status='completed', current_stage='completed'
            )
            admin = CurrentUser(id='id-bob', username='bob', role='admin')
            retried = service.retry_run(original['id'], admin)
            self.assertEqual(retried['created_by'], 'id-alice')
            self.assertEqual(retried['requested_by'], 'bob')

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
                        # ADR 0012: 创建即编译 plan 能力并集（见 request_for）。
                        self.request_for('alice', role='device_operator'),
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
                        self.request_for('alice', role='device_operator'),
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
        from tests.contract.snapshot_tools import flatten_app_routes
        with cluster_port_snapshot():
            paths = {route.path for route in flatten_app_routes(create_app())}
        self.assertIn('/api/automation/runs', paths)
        self.assertIn('/api/automation/runs/preflight', paths)
        self.assertIn('/automation', paths)

    def test_non_admin_can_read_another_users_run_as_readonly(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service):
                created = asyncio.run(automation_api.create_automation_run(
                    {
                        'artifact_path': '/tmp/update.img',
                        'devices': ['ABC'],
                        'test_plan': {'test_type': 'CTS'},
                    },
                    self.request_for('alice', role='device_operator'),
                ))
                shared = asyncio.run(automation_api.get_automation_run(
                    created['data']['id'], self.request_for('bob')
                ))
                visible = asyncio.run(automation_api.list_automation_runs(
                    self.request_for('alice', role='device_operator'),
                    status='', limit=50,
                ))

            self.assertTrue(shared['success'])
            self.assertEqual(shared['data']['permissions'], {'can_manage': False})
            self.assertEqual([item['id'] for item in visible['data']['items']], [created['data']['id']])
            self.assertEqual(visible['data']['items'][0]['permissions'], {'can_manage': True})

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

        cluster = online_worker_cluster(Repository())
        with TemporaryDirectory() as tmp:
            service = AutomationService(
                store=AutomationStore(Path(tmp) / 'automation.sqlite3'),
                profiles_path=Path(tmp) / 'profiles.json',
                cluster_provider=lambda: cluster,
            )
            result = service.preflight({
                'devices': ['ABC'],
                'test_plan': {'worker_id': 'worker-1', 'test_type': 'CTS', 'flash': {'mode': 'skip'}},
            })

        self.assertTrue(result['ready'])
        self.assertTrue(result['runtime_checked'])
        self.assertEqual(result['worker_id'], 'worker-1')
        self.assertEqual(result['test_suite'], '/suite/tools')
        self.assertEqual(result['devices'], ['worker-1:ABC'])

    def test_live_preflight_uses_same_gsi_suite_mapping_as_test_page(self):
        class Repository:
            @staticmethod
            def list_suites():
                return [{
                    'worker_id': 'worker-1', 'available': True,
                    'suite_type': 'CTS', 'suite_version': '17_r1',
                    'suite_key': 'CTS:17_r1', 'tools_path': '/cts/tools',
                    'last_scanned_at': '2026-07-13T00:00:00Z',
                }]

            @staticmethod
            def list_devices(worker_id):
                return [{
                    'id': f'{worker_id}:ABC', 'serial': 'ABC',
                    'state': 'available',
                }]

        cluster = online_worker_cluster(Repository())
        with TemporaryDirectory() as tmp:
            service = AutomationService(
                store=AutomationStore(Path(tmp) / 'automation.sqlite3'),
                profiles_path=Path(tmp) / 'profiles.json',
                cluster_provider=lambda: cluster,
            )
            result = service.preflight({
                'devices': ['ABC'],
                'test_plan': {
                    'worker_id': 'worker-1', 'test_type': 'GSI',
                    'flash': {'mode': 'skip'},
                    'test_module': 'CtsAppSecurityHostTestCases',
                },
            })

        self.assertEqual(result['test_type'], 'GSI')
        self.assertEqual(result['test_suite'], '/cts/tools')

    def test_live_preflight_rejects_adb_proxy_for_firmware_flash(self):
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
            def list_devices(worker_id):
                return [{
                    'id': f'{worker_id}:ABC', 'serial': 'ABC',
                    'state': 'available', 'transport': 'adb_proxy',
                }]

        cluster = online_worker_cluster(
            Repository(), config=SimpleNamespace(local_worker_id='ats-worker-controller'),
        )
        with TemporaryDirectory() as tmp:
            service = AutomationService(
                store=AutomationStore(Path(tmp) / 'automation.sqlite3'),
                profiles_path=Path(tmp) / 'profiles.json',
                cluster_provider=lambda: cluster,
            )
            with self.assertRaisesRegex(
                ValueError, 'ADB Proxy devices cannot be used'
            ):
                service.preflight({
                    'artifact_url': 'https://jenkins.example/artifact/update.img',
                    'devices': ['ABC'],
                    'test_plan': {
                        'worker_id': 'worker-1',
                        'test_type': 'CTS',
                        'jenkins': {'base_url': 'https://jenkins.example'},
                    },
                })

    def test_preflight_compiles_capabilities_like_create(self):
        # ADR 0012：预检与创建同一 compiler——权限不足预检即 ValueError（路由 409）。
        with TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            build_service(tmp).preflight({'artifact_path': '/tmp/update.img', 'test_plan': {'test_type': 'CTS'}},
                                         principal=CurrentUser(id='u', username='u', role='user'))

    def test_index_template_has_gms_ats_nav_entry(self):
        # shell 脚本已外置：标记可能落在模板或外置脚本，统一走 bundle helper。
        template = read_shell_bundle()
        self.assertIn('data-page="automation"', template)
        self.assertIn('id="page-automation"', template)
        self.assertIn('src="/automation"', template)
        self.assertIn("'automation': 'GMS ATS", template)

    def test_automation_page_exposes_workflow_controls(self):
        response = asyncio.run(automation_api.automation_page())
        html = response.body.decode('utf-8')
        self.assertIn('role="tablist" aria-label="GMS ATS workflow"', html)
        self.assertIn('data-workflow="create"', html)
        self.assertIn('id="automation-preflight-run"', html)
        self.assertIn('id="automation-flash-mode"', html)
        self.assertIn('id="automation-create-run"', html)
        self.assertIn('id="automation-runs"', html)
        js = read_page_bundle('features/automation/ui/page.html')
        self.assertIn('/automation/page.js', html)
        self.assertIn('/api/automation/runs', js)
        self.assertIn('/api/automation/gerrit/poll', js)
        self.assertIn('/api/automation/worker/tick', js)

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

    def test_profile_responses_mask_jenkins_secrets_and_roundtrip(self):
        """读回表面遮蔽 Jenkins 凭据；"***" 哨兵保存不得覆盖真实值。"""
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service):
                created = asyncio.run(
                    automation_api.save_automation_profile(
                        {
                            'id': 'p1',
                            'name': 'Profile 1',
                            'jenkins': {
                                'job': 'J1',
                                'username': 'bot',
                                'api_token': 'real-token',
                            },
                        }
                    )
                )
                self.assertEqual(
                    created['data']['profile']['jenkins']['api_token'], '***'
                )
                listed = asyncio.run(
                    automation_api.list_automation_profiles(enabled_only=False)
                )
                self.assertEqual(
                    listed['data']['items'][0]['jenkins']['api_token'], '***'
                )
                updated = asyncio.run(
                    automation_api.save_automation_profile(
                        {
                            'id': 'p1',
                            'name': 'Profile 1',
                            'jenkins': {
                                'job': 'J1',
                                'username': 'bot',
                                'api_token': '***',
                            },
                        }
                    )
                )
                self.assertEqual(
                    updated['data']['profile']['jenkins']['job'], 'J1'
                )
                stored = next(
                    item
                    for item in service.list_profiles()
                    if item['id'] == 'p1'
                )
                self.assertEqual(stored['jenkins']['api_token'], 'real-token')


    def test_preflight_firmware_artifact_roots_are_fail_closed(self):
        """未配置 firmware_shares.allowed_prefixes 时拒绝固件路径（fail-closed）。"""
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

        cluster = online_worker_cluster(Repository())
        with TemporaryDirectory() as tmp:
            image = Path(tmp) / 'update.img'
            image.write_bytes(b'firmware')
            service = AutomationService(
                store=AutomationStore(Path(tmp) / 'automation.sqlite3'),
                profiles_path=Path(tmp) / 'profiles.json',
                cluster_provider=lambda: cluster,
            )
            request = {
                'artifact_path': str(image),
                'devices': ['ABC'],
                'test_plan': {
                    'worker_id': 'worker-1', 'test_type': 'CTS',
                    'test_module': 'CtsAppSecurityHostTestCases',
                    'flash': {'mode': 'skip'},
                },
            }
            with patch('foundation.config.config_manager.load_config', return_value={}), self.assertRaisesRegex(
                ValueError, 'outside the configured firmware roots'
            ):
                service.preflight(request)
            with patch(
                'foundation.config.config_manager.load_config',
                return_value={'firmware_shares': {'allowed_prefixes': [tmp]}},
            ):
                result = service.preflight(request)

            self.assertTrue(result['ready'])

    def test_profile_ui_roundtrip_saves_masked_listing_verbatim(self):
        """管理端 UI 把 GET 遮蔽列表原文整体保存，真实凭据不被 "***" 覆盖。"""
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service):
                asyncio.run(automation_api.save_automation_profile({
                    'id': 'p1',
                    'name': 'Profile 1',
                    'jenkins': {
                        'job': 'J1', 'username': 'bot', 'api_token': 'real-token',
                    },
                    'build': {
                        'provider': 'ssh', 'server_password': 'real-password',
                    },
                }))
                listed = asyncio.run(
                    automation_api.list_automation_profiles(enabled_only=False)
                )
                # UI 拿到的就是这份遮蔽数据，不重敲字段、原样整体回存。
                masked_profile = listed['data']['items'][0]
                self.assertEqual(masked_profile['jenkins']['api_token'], '***')
                self.assertEqual(masked_profile['build']['server_password'], '***')
                saved = asyncio.run(
                    automation_api.save_automation_profile(masked_profile)
                )

            self.assertTrue(saved['success'])
            stored = next(
                item for item in service.list_profiles() if item['id'] == 'p1'
            )
            self.assertEqual(stored['jenkins']['api_token'], 'real-token')
            self.assertEqual(stored['build']['server_password'], 'real-password')


class HumanOnlyRunSurfaceTests(unittest.TestCase):
    """ADR 0012 验收：ATS run 表面对 Agent/机器 principal 关闭。

    零 scope 的 Agent Token 能通过任意“仅认证”端点，因此 create/
    preflight/cancel/retry 必须显式要求人类 principal；能力不足的
    人类创建者按缺失能力拒绝（执行链不能比发起者更多权）。
    """

    def agent_request_for(self, username: str = 'bot') -> Request:
        request = Request({
            'type': 'http', 'method': 'POST', 'path': '/',
            'headers': [], 'client': ('127.0.0.1', 1234),
        })
        request.state.current_user = CurrentUser(
            id=f'agent:{username}', username=username, role='agent_service',
        )
        request.state.auth_method = 'agent_token'
        return request

    @staticmethod
    def _run_payload() -> dict:
        return {
            'profile_id': 'manual-smoke',
            'artifact_path': '/tmp/update.img',
            'devices': ['ABC123'],
            'test_plan': {'test_type': 'CTS'},
        }

    def test_agent_token_cannot_create_run(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service), \
                    self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    automation_api.create_automation_run(
                        self._run_payload(), self.agent_request_for()
                    )
                )
            self.assertEqual(raised.exception.status_code, 403)
            self.assertTrue(raised.exception.detail.get('agent_forbidden'))

    def test_agent_token_cannot_cancel_or_retry_run(self):
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            run = service.create_run(
                self._run_payload(), created_by='id-human'
            )
            with patch.object(automation_api, 'automation_service', service):
                with self.assertRaises(HTTPException) as cancel_raised:
                    asyncio.run(
                        automation_api.cancel_automation_run(
                            run['id'], self.agent_request_for()
                        )
                    )
                with self.assertRaises(HTTPException) as retry_raised:
                    asyncio.run(
                        automation_api.retry_automation_run(
                            run['id'], self.agent_request_for()
                        )
                    )
            self.assertEqual(cancel_raised.exception.status_code, 403)
            self.assertEqual(retry_raised.exception.status_code, 403)

    def test_creator_without_capabilities_is_rejected(self):
        request = Request({
            'type': 'http', 'method': 'POST', 'path': '/',
            'headers': [], 'client': ('127.0.0.1', 1234),
        })
        # 'user' 角色没有 devices.lease/devices.read —— 设备预留阶段
        # 的能力必须由创建者本身持有（ADR 0012 阶段→能力映射）。
        request.state.current_user = CurrentUser(
            id='id-alice', username='alice', role='user'
        )
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            with patch.object(automation_api, 'automation_service', service):
                response = asyncio.run(
                    automation_api.create_automation_run(
                        self._run_payload(), request
                    )
                )
            self.assertEqual(response.status_code, 400)
            self.assertIn('devices.lease', response.body.decode())

    def test_service_layer_rejects_non_human_creators(self):
        """服务层 human-only 边界（对话 Agent 直连 service 的路径）。

        路由层 require_human_principal 只覆盖 REST 入口；工作流执行直接
        调 AutomationService，必须在服务内部同样拒绝四类非人类主体：
        agent token、machine capability principal、worker_service，
        以及 ADR 0006 的 'agent' 兼容角色。None（dev 模式/Gerrit 触发）
        维持既有信任级别放行。
        """
        payload = self._run_payload()
        non_human = [
            CurrentUser(id='agent:1', username='bot', role='agent_service'),
            CurrentUser(
                id='automation:ats_run.1', username='automation:ats_run.1',
                role='agent_service', extra_permissions=frozenset({
                    'tests.execute', 'devices.lease', 'firmware.stage',
                }), resource_owner_id='id-human',
            ),
            CurrentUser(id='worker-1', username='worker-1', role='worker_service'),
            CurrentUser(id='agent-legacy', username='legacy', role='agent'),
        ]
        with TemporaryDirectory() as tmp:
            service = build_service(tmp)
            for principal in non_human:
                with self.subTest(role=principal.role, id=principal.id):
                    with self.assertRaises(ValueError) as raised:
                        service.create_run(
                            payload, created_by='id-human', principal=principal
                        )
                    self.assertIn('human principal', str(raised.exception))
                    with self.assertRaises(ValueError):
                        service.preflight(payload, principal=principal)
            # None：dev 模式与系统触发（Gerrit/计划重放）的既有信任级别。
            run = service.create_run(payload, created_by='id-human', principal=None)
            self.assertTrue(run['id'])
