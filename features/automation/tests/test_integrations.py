import unittest


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class FakeSession:
    def __init__(self):
        self.calls = []
        self.auth = None
        self.verify = True
        self.headers = {}
        self.responses = []

    def queue(self, response):
        self.responses.append(response)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class JenkinsClientTests(unittest.TestCase):
    def test_trigger_build_uses_jenkins_queue_location(self):
        from features.automation.jenkins_client import JenkinsClient

        session = FakeSession()
        session.queue(FakeResponse(json_data={"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"}))
        session.queue(FakeResponse(status_code=201, headers={"Location": "http://jenkins/queue/item/9/"}))
        client = JenkinsClient(
            {"base_url": "http://jenkins/", "username": "user", "api_token": "token", "verify_ssl": False},
            session=session,
        )

        result = client.trigger_build("GMS_JOB", {"GERRIT_CHANGE": "123"})

        self.assertEqual(result["success"], True)
        self.assertEqual(result["queue_url"], "http://jenkins/queue/item/9/")
        self.assertEqual(session.auth, ("user", "token"))
        self.assertEqual(session.verify, False)
        self.assertEqual(session.calls[1][0], "POST")
        self.assertIn("/job/GMS_JOB/buildWithParameters", session.calls[1][1])

    def test_poll_build_and_select_artifact(self):
        from features.automation.jenkins_client import JenkinsClient

        session = FakeSession()
        session.queue(FakeResponse(json_data={
            "building": False,
            "result": "SUCCESS",
            "url": "http://jenkins/job/GMS_JOB/12/",
            "artifacts": [
                {"relativePath": "logs/console.txt"},
                {"relativePath": "out/update.img"},
            ],
        }))
        client = JenkinsClient({"base_url": "http://jenkins"}, session=session)

        build = client.get_build("GMS_JOB", "12")
        artifact = client.select_artifact(build, r".*update.*\.img$")

        self.assertEqual(build["success"], True)
        self.assertEqual(build["result"], "SUCCESS")
        self.assertEqual(artifact["url"], "http://jenkins/job/GMS_JOB/12/artifact/out/update.img")


class GerritTriggerTests(unittest.TestCase):
    def test_normalize_patchset_created_event_and_match_profile(self):
        from features.automation.gerrit_trigger import match_profiles, normalize_gerrit_event

        event = normalize_gerrit_event({
            "type": "patchset-created",
            "change": {
                "project": "rk3576_android16",
                "branch": "master",
                "number": "123",
                "subject": "Fix GMS",
                "owner": {"email": "dev@rock-chips.com"},
            },
            "patchSet": {"number": "7", "revision": "abcdef"},
        })
        profiles = [
            {"id": "p1", "enabled": True, "gerrit": {"project_regex": "rk3576.*", "branch_regex": "master"}},
            {"id": "p2", "enabled": True, "gerrit": {"project_regex": "rk3588.*"}},
        ]

        matches = match_profiles(event, profiles)

        self.assertEqual(event["change_id"], "123")
        self.assertEqual(event["patchset"], "7")
        self.assertEqual(event["source_key"], "gerrit:rk3576_android16:123:7")
        self.assertEqual([profile["id"] for profile in matches], ["p1"])


class HttpAutomationExecutorTests(unittest.TestCase):
    def test_post_flash_verification_accepts_product_device_or_board_identity(self):
        from features.automation.executors import HttpAutomationExecutor

        class DeviceManager:
            @staticmethod
            def get_device_info(serial):
                return {"board": "rk3576", "model": "Android", "fingerprint": "vendor/release-keys"}

        executor = HttpAutomationExecutor(device_manager=DeviceManager())

        result = executor._verify_post_flash(
            ["ABC123"],
            {"product": "rk3576", "fingerprint_contains": "release-keys", "retries": 1},
        )

        self.assertEqual(result, {"success": True, "verified": True})

    def test_jenkins_flash_stages_artifact_url_instead_of_using_relative_path(self):
        from features.automation.executors import HttpAutomationExecutor

        class StagingExecutor(HttpAutomationExecutor):
            def _stage_http_artifact(self, run, artifact_url):
                self.staged_url = artifact_url
                return {"success": True, "firmware_path": "/test-host/ats_update.img"}

        session = FakeSession()
        session.queue(FakeResponse(json_data={"success": True, "message": "burn ok"}))
        executor = StagingExecutor(base_url="http://127.0.0.1:5001", session=session)
        run = {
            "id": "run_jenkins",
            "artifact_path": "out/update.img",
            "artifact_url": "http://jenkins/job/gms/1/artifact/out/update.img",
            "devices_json": '[{"serial":"ABC123"}]',
            "test_plan_json": '{"jenkins":{"base_url":"http://jenkins"}}',
        }

        result = executor.flash(run)

        self.assertEqual(result["success"], True)
        self.assertEqual(executor.staged_url, run["artifact_url"])
        self.assertEqual(session.calls[0][2]["data"]["firmware_path"], "/test-host/ats_update.img")

    def test_http_executor_uses_embedded_jenkins_config_and_selects_artifact(self):
        from features.automation.executors import HttpAutomationExecutor

        executor = HttpAutomationExecutor(base_url="http://127.0.0.1:5001")
        run = {
            "id": "run_jenkins",
            "jenkins_job": "GMS_BUILD",
            "jenkins_queue_url": "http://jenkins/queue/item/9/",
            "jenkins_build_number": "12",
            "test_plan_json": """
            {
              "jenkins": {
                "base_url": "http://jenkins/",
                "username": "user",
                "api_token": "token",
                "parameters": {"GERRIT_CHANGE": "123"},
                "artifact_pattern": ".*update.*\\\\.img$"
              }
            }
            """,
        }

        class FakeJenkinsClient:
            def __init__(self, config):
                self.config = config

            def trigger_build(self, job, params):
                return {"success": True, "queue_url": "http://jenkins/queue/item/9/", "job": job, "params": params}

            def get_build(self, job, number):
                return {
                    "success": True,
                    "job": job,
                    "build_number": str(number),
                    "building": False,
                    "result": "SUCCESS",
                    "url": "http://jenkins/job/GMS_BUILD/12/",
                    "artifacts": [{"relativePath": "out/update.img"}],
                }

            @staticmethod
            def select_artifact(build, artifact_pattern):
                return {"success": True, "url": "http://jenkins/job/GMS_BUILD/12/artifact/out/update.img"}

        import features.automation.executors as executors
        old_client = executors.JenkinsClient
        executors.JenkinsClient = FakeJenkinsClient
        try:
            triggered = executor.trigger_build(run)
            polled = executor.poll_build(run)
        finally:
            executors.JenkinsClient = old_client

        self.assertEqual(triggered["success"], True)
        self.assertEqual(triggered["jenkins_queue_url"], "http://jenkins/queue/item/9/")
        self.assertEqual(polled["success"], True)
        self.assertEqual(polled["artifact_url"], "http://jenkins/job/GMS_BUILD/12/artifact/out/update.img")

    def test_http_executor_calls_existing_burn_and_test_apis(self):
        from features.automation.executors import HttpAutomationExecutor

        session = FakeSession()
        session.queue(FakeResponse(json_data={"success": True, "message": "burn ok"}))
        session.queue(FakeResponse(json_data={"success": True, "message": "test started"}))
        session.queue(FakeResponse(json_data={"running": True, "logs": [], "log_count": 0}))
        executor = HttpAutomationExecutor(base_url="http://127.0.0.1:5001", session=session)
        run = {
            "id": "run_1",
            "artifact_path": "/tmp/update.img",
            "devices_json": '[{"serial":"ABC123"}]',
            "test_plan_json": '{"test_type":"CTS","test_module":"CtsAppSecurityHostTestCases","test_suite":"/tmp/android-cts/tools"}',
        }

        flash = executor.flash(run)
        start = executor.start_test(run)
        poll = executor.poll_test(run)

        self.assertEqual(flash["success"], True)
        self.assertEqual(start["success"], True)
        self.assertEqual(start["running"], True)
        self.assertEqual(poll["running"], True)
        self.assertEqual(session.calls[0][0], "POST")
        self.assertIn("/api/burn/firmware", session.calls[0][1])
        self.assertEqual(session.calls[0][2]["data"]["firmware_path"], "/tmp/update.img")
        self.assertIn("/api/test/start", session.calls[1][1])
        self.assertEqual(session.calls[1][2]["json"]["devices"], ["ABC123"])
        self.assertIn("/api/test/status", session.calls[2][1])

    def test_poll_test_returns_the_exact_report_created_by_the_run(self):
        from features.automation.executors import HttpAutomationExecutor

        session = FakeSession()
        session.queue(FakeResponse(json_data={
            "running": False,
            "test_outcome": "completed",
            "report_timestamp": "2026.07.10_20.30.00",
            "logs": [{"type": "success", "msg": "done"}],
        }))
        executor = HttpAutomationExecutor(base_url="http://127.0.0.1:5001", session=session)

        result = executor.poll_test({"id": "run_1"})

        self.assertEqual(result["success"], True)
        self.assertEqual(result["report_timestamp"], "2026.07.10_20.30.00")

    def test_cluster_test_routes_start_poll_and_cancel_by_cluster_job_id(self):
        from features.automation.executors import HttpAutomationExecutor

        session = FakeSession()
        session.queue(FakeResponse(json_data={"success": True, "data": {
            "cluster_job_id": "job-1", "worker_id": "worker-246"}}))
        session.queue(FakeResponse(json_data={"success": True, "job": {
            "id": "job-1", "status": "running", "assigned_worker_id": "worker-246"}}))
        session.queue(FakeResponse(json_data={"success": True, "job": {
            "id": "job-1", "status": "stopping"}}))
        executor = HttpAutomationExecutor(base_url="http://controller", session=session)
        run = {"id": "run-cluster", "status": "testing",
               "devices_json": '[{"serial":"worker-246:ABC"}]',
               "test_plan_json": '{"worker_id":"worker-246","test_type":"CTS","test_suite":"/suite/tools"}',
               "result_json": '{"cluster_job_id":"job-1"}'}

        started = executor.start_test(run)
        polled = executor.poll_test(run)
        cancelled = executor.cancel(run)

        self.assertEqual(started["cluster_job_id"], "job-1")
        self.assertTrue(polled["running"])
        self.assertTrue(cancelled["success"])
        self.assertEqual(session.calls[0][2]["json"]["worker_id"], "worker-246")
        self.assertIn("/api/cluster/jobs/job-1", session.calls[1][1])
        self.assertIn("/api/cluster/jobs/job-1/cancel", session.calls[2][1])
