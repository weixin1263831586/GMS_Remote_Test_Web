"""Tests for foundation port registration semantics and composition-root wiring.

Unified rule under test: for every ``configure_*`` seam, omitted arguments
keep the current registration while an explicit ``None`` clears it; accessors
raise ``RuntimeError`` once cleared so callers fall back to single-host
behavior. Port registration must come from the composition root
(``bootstrap.dependencies._wire_cross_feature_services``), never from
importing a feature package.
"""

import unittest
from unittest.mock import MagicMock

from bootstrap import dependencies
from foundation import automation_port, cluster_port
from features.redmine import api as redmine_api


class ClusterPortConfigureSemanticsTests(unittest.TestCase):
    def setUp(self):
        saved = (
            cluster_port._cluster_service_provider,
            cluster_port._cancel_job,
            cluster_port._worker_tokens,
        )
        self.addCleanup(
            cluster_port.configure_cluster_access,
            service_provider=saved[0],
            cancel_job=saved[1],
            worker_tokens=saved[2],
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


class AutomationPortConfigureSemanticsTests(unittest.TestCase):
    def test_explicit_none_clears_registration(self):
        self.addCleanup(
            automation_port.configure_worker_status_provider,
            automation_port._worker_status_provider,
        )

        automation_port.configure_worker_status_provider(
            lambda: {'enabled': False}
        )
        self.assertEqual(
            automation_port.get_worker_status(), {'enabled': False}
        )

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
