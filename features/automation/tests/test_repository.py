import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class AutomationModelTests(unittest.TestCase):
    def test_manual_run_request_defaults_to_queued_manual_source(self):
        from features.automation.models import RUN_STATUS_QUEUED, AutomationRunCreateRequest

        req = AutomationRunCreateRequest(
            profile_id="manual-smoke",
            project="rk3576_android16",
            branch="master",
            artifact_path="/tmp/update.img",
            devices=["ABC123"],
            test_plan={"test_type": "CTS", "test_module": "CtsAppSecurityHostTestCases"},
        )

        run = req.to_run_dict(run_id="ats_test_001")

        self.assertEqual(run["id"], "ats_test_001")
        self.assertEqual(run["source_type"], "manual")
        self.assertEqual(run["status"], RUN_STATUS_QUEUED)
        self.assertEqual(run["current_stage"], RUN_STATUS_QUEUED)
        self.assertEqual(run["devices_json"], '[{"serial":"ABC123"}]')
        self.assertIn("CtsAppSecurityHostTestCases", run["test_plan_json"])


class AutomationStoreTests(unittest.TestCase):
    def test_store_creates_run_and_appends_events(self):
        from features.automation.models import AutomationRunCreateRequest
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            run = AutomationRunCreateRequest(
                profile_id="manual-smoke",
                devices=["ABC123"],
                test_plan={"test_type": "CTS"},
            ).to_run_dict("ats_test_001")

            created = store.create_run(run)
            event = store.append_event("ats_test_001", "queued", "info", "Run queued", {"profile_id": "manual-smoke"})

            self.assertEqual(created["id"], "ats_test_001")
            self.assertEqual(store.get_run("ats_test_001")["status"], "queued")
            self.assertEqual(event["run_id"], "ats_test_001")
            self.assertEqual(len(store.list_events("ats_test_001")), 1)
            self.assertEqual(store.list_events("ats_test_001")[0]["message"], "Run queued")

    def test_store_updates_status_and_lists_runs_newest_first(self):
        from features.automation.models import AutomationRunCreateRequest
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            store.create_run(AutomationRunCreateRequest(profile_id="p1").to_run_dict("run_1"))
            store.create_run(AutomationRunCreateRequest(profile_id="p2").to_run_dict("run_2"))

            updated = store.update_run("run_1", status="testing", current_stage="testing", error="")

            self.assertEqual(updated["status"], "testing")
            self.assertEqual([run["id"] for run in store.list_runs(limit=10)], ["run_2", "run_1"])

    def test_store_can_find_run_by_source_key(self):
        from features.automation.models import AutomationRunCreateRequest
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            run_data = AutomationRunCreateRequest(
                profile_id="p1",
                source_key="gerrit:project:123:7:p1",
            ).to_run_dict("run_1")
            store.create_run(run_data)

            found = store.get_run_by_source_key("gerrit:project:123:7:p1")

            self.assertEqual(found["id"], "run_1")


class AutomationProfileTests(unittest.TestCase):
    def test_profile_loader_reads_enabled_profiles(self):
        from features.automation.profiles import load_profiles

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            path.write_text(json.dumps({
                "profiles": [
                    {"id": "p1", "name": "Profile 1", "enabled": True},
                    {"id": "p2", "name": "Profile 2", "enabled": False},
                ]
            }), encoding="utf-8")

            profiles = load_profiles(path, enabled_only=True)

            self.assertEqual([profile["id"] for profile in profiles], ["p1"])

    def test_profile_loader_can_save_and_update_profiles(self):
        from features.automation.profiles import load_profiles, save_profiles, upsert_profile

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            profile = {"id": "p1", "name": "Profile 1", "enabled": True, "test_plan": {"test_type": "CTS"}}

            save_profiles(path, [profile])
            upsert_profile(path, {"id": "p1", "name": "Profile 1 Updated", "enabled": False})

            profiles = load_profiles(path)

            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0]["name"], "Profile 1 Updated")
            self.assertEqual(profiles[0]["enabled"], False)
