import json
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor
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
        self.assertEqual(run["trace_id"], "ats_test_001")
        self.assertEqual(run["state_version"], "1")
        self.assertEqual(run["source_type"], "manual")
        self.assertEqual(run["status"], RUN_STATUS_QUEUED)
        self.assertEqual(run["current_stage"], RUN_STATUS_QUEUED)
        self.assertEqual(run["devices_json"], '[{"serial":"ABC123"}]')
        self.assertIn("CtsAppSecurityHostTestCases", run["test_plan_json"])


class AutomationStoreTests(unittest.TestCase):
    def test_store_recovers_after_runtime_data_directory_deletion(self):
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "automation"
            store = AutomationStore(data_dir / "automation.sqlite3")

            shutil.rmtree(data_dir)

            self.assertEqual(store.list_runs(limit=10), [])

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
            payload = json.loads(store.list_events("ats_test_001")[0]["payload_json"])
            self.assertEqual(payload["trace_id"], "ats_test_001")

    def test_compare_and_swap_validates_transition_and_increments_version(self):
        from features.automation.models import AutomationRunCreateRequest
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            store.create_run(AutomationRunCreateRequest().to_run_dict("run_1"))

            advanced, applied = store.update_run_if_status(
                "run_1", "queued", status="waiting_device", current_stage="waiting_device"
            )
            self.assertTrue(applied)
            self.assertEqual(advanced["state_version"], 2)
            with self.assertRaisesRegex(ValueError, "invalid automation run transition"):
                store.update_run_if_status(
                    "run_1", "waiting_device", status="reporting", current_stage="reporting"
                )

    def test_expired_claim_recovery_is_persisted_and_observable(self):
        from features.automation.models import AutomationRunCreateRequest
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            store.create_run(AutomationRunCreateRequest().to_run_dict("run_1"))
            store.update_run(
                "run_1", lease_owner="dead-controller",
                lease_expires_at="2000-01-01T00:00:00Z",
            )

            self.assertTrue(store.claim_run("run_1", "new-controller"))
            recovered = store.get_run("run_1")
            self.assertEqual(recovered["recovery_count"], 1)
            self.assertTrue(recovered["last_recovered_at"])
            event = store.list_events("run_1")[-1]
            self.assertEqual(event["event_type"], "run.recovered")
            self.assertEqual(event["operation_id"], "new-controller")

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

    def test_only_one_worker_can_claim_the_same_run(self):
        from features.automation.models import AutomationRunCreateRequest
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            store.create_run(AutomationRunCreateRequest().to_run_dict("run_1"))
            with ThreadPoolExecutor(max_workers=2) as pool:
                claimed = list(pool.map(
                    lambda owner: store.claim_run("run_1", owner),
                    ("worker-a", "worker-b"),
                ))

            self.assertEqual(sum(claimed), 1)

    def test_run_lists_filter_by_creator(self):
        from features.automation.models import AutomationRunCreateRequest
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            for run_id, owner in (("run_1", "alice"), ("run_2", "bob")):
                data = AutomationRunCreateRequest().to_run_dict(run_id)
                data["created_by"] = owner
                store.create_run(data)

            self.assertEqual(
                [item["id"] for item in store.list_run_summaries(created_by="alice")],
                ["run_1"],
            )


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
