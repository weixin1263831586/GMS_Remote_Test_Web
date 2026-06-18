import asyncio
import unittest


class _FakeRepository:
    def __init__(self):
        self.issues = {}
        self.stale_mark_count = 0

    def get_issue(self, issue_id):
        return self.issues.get(issue_id)

    def mark_stale_running_runs(self):
        self.stale_mark_count += 1


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeAgent:
    def __init__(self):
        self.run_gate = asyncio.Event()
        self.client = _FakeClient()
        self.raise_on_analyze = False

    async def run(self, **kwargs):
        await self.run_gate.wait()
        return {"status": "done", **kwargs}

    async def sync_all_assigned_issues(self, **kwargs):
        return {"status": "done", **kwargs}

    def _make_client(self):
        return self.client

    async def analyze_issue(self, client, issue_id, run_id):
        if self.raise_on_analyze:
            raise RuntimeError("analysis failed")
        return {"issue_id": issue_id, "run_id": run_id}


class RedmineServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_run_is_single_flight_and_status_tracks_run(self):
        from features.redmine.service import RedmineService

        repository = _FakeRepository()
        agent = _FakeAgent()
        service = RedmineService(repository=repository, agent=agent)

        started = await service.start_run(hours=48, max_issues=20, mode="manual")
        duplicate = await service.start_run(hours=24, max_issues=10, mode="manual")

        self.assertTrue(started["success"])
        self.assertFalse(duplicate["success"])
        self.assertEqual(duplicate["run_id"], started["run_id"])
        self.assertTrue(service.status()["running"])
        self.assertEqual(service.status()["active_run_id"], started["run_id"])
        self.assertEqual(repository.stale_mark_count, 1)

        agent.run_gate.set()
        await service.task.task
        self.assertFalse(service.status()["running"])
        self.assertEqual(service.status()["last_result"]["status"], "done")

    async def test_fetch_and_analyze_issue_closes_client_on_success(self):
        from features.redmine.service import RedmineService

        repository = _FakeRepository()
        agent = _FakeAgent()
        service = RedmineService(repository=repository, agent=agent)

        started = await service.fetch_and_analyze_issue(123)
        self.assertTrue(started["success"])
        await service.task.task

        self.assertTrue(agent.client.closed)
        self.assertEqual(service.status()["last_result"]["issue_id"], 123)

    async def test_fetch_and_analyze_issue_closes_client_on_error(self):
        from features.redmine.service import RedmineService

        repository = _FakeRepository()
        agent = _FakeAgent()
        agent.raise_on_analyze = True
        service = RedmineService(repository=repository, agent=agent)

        await service.fetch_and_analyze_issue(456)
        with self.assertRaisesRegex(RuntimeError, "analysis failed"):
            await service.task.task

        self.assertTrue(agent.client.closed)
        self.assertEqual(service.status()["last_result"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
