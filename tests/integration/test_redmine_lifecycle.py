import inspect
import tempfile
import unittest
from pathlib import Path

from bootstrap.application import create_app
from bootstrap.dependencies import build_services
from foundation.config import RuntimeSettings


class RedmineLifecycleTests(unittest.TestCase):
    def test_application_and_scheduler_share_injected_service(self):
        from features.redmine import api
        from features.redmine import scheduler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_settings = RuntimeSettings.from_environment(
                project_root=root,
                environ={"GMS_DATA_ROOT": str(root / "data")},
            )
            services = build_services(runtime_settings=runtime_settings)

            create_app(services)

            self.assertIs(api.redmine_service, services.redmine)
            self.assertIn("service", inspect.signature(
                scheduler.start_redmine_agent_scheduler
            ).parameters)
            self.assertNotIn(
                "features.redmine.api",
                inspect.getsource(scheduler),
            )
            self.assertEqual(
                services.redmine.repository.db_path,
                runtime_settings.data_root / "redmine/redmine.sqlite3",
            )


if __name__ == "__main__":
    unittest.main()
