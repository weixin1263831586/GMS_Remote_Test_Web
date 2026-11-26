"""Tests for foundation port registration semantics and composition-root wiring.

Unified rule under test: for every ``configure_*`` seam, omitted arguments
keep the current registration while an explicit ``None`` clears it; accessors
raise ``RuntimeError`` once cleared so callers fall back to single-host
behavior. Port registration must come from the composition root
(``bootstrap.dependencies._wire_cross_feature_services``), never from
importing a feature package.
"""

import asyncio
import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock

from bootstrap import dependencies
from features.redmine import api as redmine_api
from foundation import automation_port, cluster_port, ops_port


class ClusterPortConfigureSemanticsTests(unittest.TestCase):
    def setUp(self):
        saved = (
            cluster_port._cluster_service_provider,
            cluster_port._cancel_job,
            cluster_port._worker_tokens,
            cluster_port._device_action_request,
        )
        self.addCleanup(
            cluster_port.configure_cluster_access,
            service_provider=saved[0],
            cancel_job=saved[1],
            worker_tokens=saved[2],
            device_action_request=saved[3],
        )

    def test_omitted_arguments_keep_current_registration(self):
        provider = MagicMock(return_value='service')
        tokens = MagicMock(return_value={'w1': 't1'})
        cluster_port.configure_cluster_access(
            service_provider=provider,
            worker_tokens=tokens,
        )

        # A later partial update must not wipe the earlier registrations.
        cluster_port.configure_cluster_access(cancel_job=MagicMock())

        self.assertIs(cluster_port.get_cluster_service(), 'service')
        self.assertEqual(cluster_port.worker_tokens(), {'w1': 't1'})

    def test_explicit_none_clears_registration(self):
        cluster_port.configure_cluster_access(service_provider=MagicMock())

        cluster_port.configure_cluster_access(service_provider=None)

        with self.assertRaises(RuntimeError):
            cluster_port.get_cluster_service()

    def test_worker_command_signature_and_defaults(self):
        self.addCleanup(
            cluster_port.configure_cluster_access,
            run_worker_command=cluster_port._run_worker_command,
        )
        command = MagicMock(return_value='pending')
        cluster_port.configure_cluster_access(run_worker_command=command)

        self.assertEqual(
            cluster_port.run_worker_command(worker_id='w1', command_type='probe', payload={}),
            'pending',
        )
        command.assert_called_once_with('w1', 'probe', {}, timeout=10)
        with self.assertRaises(TypeError):
            cluster_port.run_worker_command('w1', 'probe', {}, invalid=True)

    def test_worker_session_signature_and_defaults(self):
        self.addCleanup(
            cluster_port.configure_cluster_access,
            require_worker_session=cluster_port._require_worker_session,
        )
        session = MagicMock()
        cluster_port.configure_cluster_access(require_worker_session=session)

        cluster_port.require_worker_session(worker_id='w1')
        session.assert_called_once_with('w1', '', 0)

    def test_device_action_request_is_late_bound(self):
        result = {"success": True, "accepted": True, "command_id": "c1"}
        submit = AsyncMock(return_value=result)
        cluster_port.configure_cluster_access(device_action_request=submit)

        self.assertEqual(
            asyncio.run(cluster_port.submit_device_action("request", {"action": "aoa_probe"})),
            result,
        )
        submit.assert_awaited_once_with("request", {"action": "aoa_probe"})


class OpsPortContractTests(unittest.TestCase):
    def test_unregistered_incident_and_sweep_match_contracts(self):
        self.addCleanup(ops_port.configure_incident_recorder, ops_port._incident_recorder)
        self.addCleanup(ops_port.configure_alert_sweep, ops_port._alert_sweep)
        ops_port.configure_incident_recorder(None)
        ops_port.configure_alert_sweep(None)
        incident = ops_port.record_incident(severity='info', kind='test', message='test')
        self.assertEqual(set(incident), ops_port.IncidentResult.__required_keys__)
        self.assertEqual(incident, {'event': None, 'suppressed': True})
        sweep = ops_port.run_alert_sweep()
        self.assertEqual(set(sweep), ops_port.AlertSweepResult.__required_keys__)
        self.assertEqual(sweep, {'evaluated': 0, 'recorded': 0, 'notified': 0})


    def test_unregistered_cleanup_preserves_requested_mode(self):
        self.addCleanup(ops_port.configure_disk_hygiene, ops_port._disk_hygiene)
        ops_port.configure_disk_hygiene(None)
        self.assertEqual(ops_port.run_disk_hygiene(), {'dry_run': True, 'freed_bytes': 0, 'removed_files': 0})
        self.assertFalse(ops_port.run_disk_hygiene(dry_run=False)['dry_run'])

    def test_cleanup_default_is_dry_run(self):
        self.addCleanup(ops_port.configure_disk_hygiene, ops_port._disk_hygiene)
        cleanup = MagicMock(return_value={'dry_run': True, 'freed_bytes': 0, 'removed_files': 0})
        ops_port.configure_disk_hygiene(cleanup)

        self.assertEqual(ops_port.run_disk_hygiene(), {'dry_run': True, 'freed_bytes': 0, 'removed_files': 0})
        cleanup.assert_called_once_with(
            stage_max_age_days=ops_port.DEFAULT_STAGE_MAX_AGE_DAYS,
            upload_temp_max_age_days=ops_port.DEFAULT_UPLOAD_TEMP_MAX_AGE_DAYS,
            include_stage=False,
            dry_run=True,
        )

    def test_incident_forwards_named_contract(self):
        self.addCleanup(ops_port.configure_incident_recorder, ops_port._incident_recorder)
        recorder = MagicMock(return_value={'event': None, 'suppressed': False})
        ops_port.configure_incident_recorder(recorder)

        self.assertEqual(
            ops_port.record_incident(severity='warning', kind='worker', message='offline'),
            {'event': None, 'suppressed': False},
        )
        recorder.assert_called_once_with(
            severity='warning', kind='worker', message='offline', subject='',
            dedup_key='', next_actions=None, suppress_minutes=60,
        )


class AutomationPortConfigureSemanticsTests(unittest.TestCase):
    def test_advancement_gate_preserves_fail_open_contract(self):
        self.addCleanup(
            automation_port.configure_advancement_gate,
            automation_port._advancement_gate,
        )
        automation_port.configure_advancement_gate(None)
        self.assertFalse(automation_port.advancement_paused())

        automation_port.configure_advancement_gate(lambda: True)
        self.assertTrue(automation_port.advancement_paused())

        automation_port.configure_advancement_gate(MagicMock(side_effect=RuntimeError("unavailable")))
        with self.assertLogs("foundation.automation_port", level="ERROR"):
            self.assertFalse(automation_port.advancement_paused())

    def test_registered_worker_status_matches_contract(self):
        from features.automation import register_worker_status_port

        self.addCleanup(
            automation_port.configure_worker_status_provider,
            automation_port._worker_status_provider,
        )
        register_worker_status_port()
        status = automation_port.get_worker_status()
        self.assertEqual(set(status), set(automation_port.AutomationWorkerStatus.__annotations__))
        self.assertIsInstance(status['enabled'], bool)
        self.assertIsInstance(status['running'], bool)
        self.assertIsInstance(status['interval_seconds'], int)
        self.assertIsInstance(status['advancement_running'], bool)

    def test_explicit_none_clears_registration(self):
        self.addCleanup(
            automation_port.configure_worker_status_provider,
            automation_port._worker_status_provider,
        )

        status: automation_port.AutomationWorkerStatus = {
            'enabled': False,
            'running': False,
            'interval_seconds': 15,
            'executor': 'http',
            'last_tick_result': {},
            'last_tick_seconds_ago': None,
            'advancement_running': False,
        }
        automation_port.configure_worker_status_provider(lambda: status)
        self.assertIs(automation_port.get_worker_status(), status)

        automation_port.configure_worker_status_provider(None)

        with self.assertRaises(RuntimeError):
            automation_port.get_worker_status()


class RedmineAgentFactoryConfigureSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(
            setattr,
            redmine_api,
            '_REPORT_ANALYZER_FACTORY',
            redmine_api._REPORT_ANALYZER_FACTORY,
        )
        self.addCleanup(
            setattr,
            redmine_api,
            '_AI_ANALYZER_FACTORY',
            redmine_api._AI_ANALYZER_FACTORY,
        )

    def test_partial_update_keeps_other_factory(self):
        ai_factory = object()
        redmine_api.configure_agent_factories(
            report_analyzer_factory=object(),
            ai_analyzer_factory=ai_factory,
        )

        redmine_api.configure_agent_factories(report_analyzer_factory=object())

        self.assertIs(redmine_api._AI_ANALYZER_FACTORY, ai_factory)

    def test_explicit_none_clears_factory(self):
        redmine_api.configure_agent_factories(report_analyzer_factory=object())

        redmine_api.configure_agent_factories(report_analyzer_factory=None)

        self.assertIsNone(redmine_api._REPORT_ANALYZER_FACTORY)


class ClusterContractTests(unittest.TestCase):
    def test_repository_contract_members_exist(self):
        from features.cluster import ClusterRepository

        contract = cluster_port.ClusterRepositoryContract
        methods = {
            name for name, value in vars(contract).items()
            if not name.startswith('_') and inspect.isfunction(value)
        }
        for name in methods:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(ClusterRepository, name, None)))

    def test_service_contract_members_exist(self):
        from features.cluster import ClusterService

        self.assertTrue(callable(ClusterService.list_workers))
        self.assertTrue(callable(ClusterService.has_command_agent))
        self.assertIsInstance(ClusterService.effective_enabled, property)
        self.assertIsInstance(cluster_port.ClusterServiceContract.offline_seconds, property)
        service = ClusterService(MagicMock(), offline_seconds=73)
        self.assertEqual(service.offline_seconds, 73)


class CrossFeatureWiringTests(unittest.TestCase):
    def test_wire_cross_feature_services_registers_access_ports(self):
        self.addCleanup(
            cluster_port.configure_cluster_access,
            service_provider=cluster_port._cluster_service_provider,
        )
        self.addCleanup(
            automation_port.configure_worker_status_provider,
            automation_port._worker_status_provider,
        )
        cluster_port.configure_cluster_access(service_provider=None)
        automation_port.configure_worker_status_provider(None)

        dependencies._wire_cross_feature_services()

        self.assertIsNotNone(cluster_port._cluster_service_provider)
        self.assertIsNotNone(cluster_port._worker_tokens)
        self.assertIsNotNone(automation_port._worker_status_provider)


if __name__ == '__main__':
    unittest.main()
