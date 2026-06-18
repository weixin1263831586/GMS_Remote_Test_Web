from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from foundation.tasks import SingleFlightTask

from .agent import RedmineAgent
from .repository import RedmineAgentDB


class RedmineService:
    """Application service for Redmine runs, sync, and issue analysis."""

    def __init__(
        self,
        *,
        repository: RedmineAgentDB,
        agent: RedmineAgent | None = None,
    ):
        self.repository = repository
        self.agent = agent or RedmineAgent(repository)
        self.task = SingleFlightTask()
        self.active_run_id: str | None = None
        self._stale_runs_marked = False

    def _mark_stale_runs_once(self) -> None:
        if self._stale_runs_marked:
            return
        self.repository.mark_stale_running_runs()
        self._stale_runs_marked = True

    async def _start(self, *, run_id: str, message: str, operation) -> dict:
        self._mark_stale_runs_once()
        if self.task.running:
            return {
                "success": False,
                "error": "RedmineAgent already running",
                "run_id": self.active_run_id,
            }
        self.active_run_id = run_id
        await self.task.start(operation)
        return {"success": True, "message": message, "run_id": run_id}

    async def start_run(
        self,
        *,
        hours: int,
        max_issues: int,
        mode: str,
    ) -> dict:
        run_id = (
            datetime.now().strftime("%Y%m%d%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        return await self._start(
            run_id=run_id,
            message="RedmineAgent started",
            operation=lambda: self.agent.run(
                hours=hours,
                max_issues=max_issues,
                run_id=run_id,
                mode=mode,
            ),
        )

    async def start_sync(self, *, max_analyze: int) -> dict:
        run_id = "sync-" + datetime.now().strftime("%Y%m%d%H%M%S")
        return await self._start(
            run_id=run_id,
            message="RedmineAgent sync started",
            operation=lambda: self.agent.sync_all_assigned_issues(
                analyze_new=True,
                max_analyze=max_analyze,
                run_id=run_id,
            ),
        )

    async def fetch_and_analyze_issue(self, issue_id: int) -> dict:
        existing = self.repository.get_issue(issue_id)
        if existing and existing.get("analysis_status") == "done":
            return {
                "success": True,
                "data": {"action": "exists", "issue": existing},
            }
        run_id = f"fetch-{datetime.now().strftime('%Y%m%d%H%M%S')}"

        async def analyze() -> dict[str, Any]:
            client = self.agent._make_client()
            try:
                return await self.agent.analyze_issue(
                    client,
                    issue_id,
                    run_id,
                )
            finally:
                await client.close()

        return await self._start(
            run_id=run_id,
            message=f"Fetching #{issue_id} from Redmine",
            operation=analyze,
        )

    def status(self) -> dict:
        last_result = self.task.last_result
        if self.task.last_error is not None:
            last_result = {
                "status": "failed",
                "error": str(self.task.last_error),
            }
        return {
            "running": self.task.running,
            "active_run_id": self.active_run_id,
            "last_result": last_result,
        }
