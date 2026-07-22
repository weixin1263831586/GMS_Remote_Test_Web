import sqlite3
import tempfile
import unittest
from pathlib import Path

from bootstrap.dependencies import build_services
from bootstrap.lifecycle import initialize_runtime_data
from foundation.config import RuntimeSettings


class EmptyDataInitializationTests(unittest.TestCase):
    def test_application_initializes_all_runtime_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(__file__).resolve().parents[2]
            data_root = Path(tmp) / 'data'
            services = build_services(
                runtime_settings=RuntimeSettings.from_environment(
                    project_root=root,
                    environ={'GMS_DATA_ROOT': str(data_root)},
                )
            )

            initialize_runtime_data(services)
            initialize_runtime_data(services)

            self.assertTrue((data_root / 'redmine/redmine.sqlite3').is_file())
            self.assertTrue((data_root / 'redmine/docs').is_dir())
            self.assertTrue((data_root / 'redmine/attachments').is_dir())
            self.assertTrue((data_root / 'automation/automation.sqlite3').is_file())
            self.assertTrue((data_root / 'reports/reports.sqlite3').is_file())
            self.assertTrue((data_root / 'gms_update_monitor.sqlite3').is_file())
            self.assertTrue((data_root / 'mainline_known_issues.sqlite3').is_file())

            with sqlite3.connect(data_root / 'automation/automation.sqlite3') as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn('automation_runs', tables)

            with sqlite3.connect(data_root / 'redmine/redmine.sqlite3') as conn:
                redmine_tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn('redmine_agent_issues', redmine_tables)
