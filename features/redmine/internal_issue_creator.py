"""经二次确认后从客户问题或成熟案例创建内部 Redmine 工单。"""

from __future__ import annotations

from typing import Any

from .case_extractor import decode_json_list, decode_json_obj
from .knowledge_repository import RedmineKnowledgeDB


class InternalIssueCreator:
    """Build the Redmine payload and (optionally) create the issue."""

    def __init__(self, knowledge_db: RedmineKnowledgeDB, *, client: Any | None = None, allow_create: bool = True):
        self.db = knowledge_db
        self.client = client
        self.allow_create = allow_create

    async def from_issue(self, source_issue_id: int, *, payload: dict[str, Any], confirmed: bool = False) -> dict[str, Any]:
        """Create an internal issue seeded from a customer/source issue."""
        fact = self.db.get_case_fact(source_issue_id) or {}
        subject = str(payload.get("subject") or fact.get("subject") or f"内部工单(源自 #{source_issue_id})")
        description = self._render_description(
            summary=fact.get("problem_summary") or payload.get("summary") or "",
            root_cause=fact.get("root_cause") or payload.get("root_cause") or "",
            solution=fact.get("solution") or payload.get("solution") or "",
            related=[source_issue_id],
        )
        return await self._create(
            subject=subject,
            description=description,
            payload=payload,
            source_issue_id=source_issue_id,
            case_id=None,
            confirmed=confirmed,
        )

    async def from_mature_case(self, case_id: int, *, payload: dict[str, Any], confirmed: bool = False) -> dict[str, Any]:
        case = self.db.get_mature_case(case_id) or {}
        subject = str(payload.get("subject") or case.get("title") or f"内部工单(源自案例 #{case_id})")
        solution = decode_json_obj(case.get("solution_json"))
        source_ids = decode_json_list(case.get("source_issue_ids_json"))
        description = self._render_description(
            summary=case.get("problem_summary") or "",
            root_cause=case.get("root_cause") or "",
            solution=solution.get("overview") or "",
            related=source_ids,
        )
        return await self._create(
            subject=subject,
            description=description,
            payload=payload,
            source_issue_id=None,
            case_id=case_id,
            confirmed=confirmed,
        )

    # Internals

    async def _create(self, *, subject: str, description: str, payload: dict[str, Any], source_issue_id: int | None, case_id: int | None, confirmed: bool) -> dict[str, Any]:
        if not self.allow_create:
            return {"success": False, "error": "internal issue creation is disabled (allow_internal_issue_create=false)"}
        if not confirmed:
            return {"success": False, "error": "confirmation required", "subject": subject, "description": description, "payload": payload}

        redmine_payload = {
            "project_id": str(payload.get("project_id") or "fae"),
            "tracker_id": payload.get("tracker_id", 1),
            "priority_id": payload.get("priority_id", 2),
            "assigned_to_id": payload.get("assigned_to_id"),
            "subject": subject,
            "description": description,
        }
        # Drop None values the Redmine API would reject.
        redmine_payload = {k: v for k, v in redmine_payload.items() if v is not None}

        if self.client is None:
            return {"success": False, "error": "redmine client not configured", "payload": redmine_payload}

        # create_issue 是异步接口。
        try:
            issue = await self.client.create_issue(
                redmine_payload["project_id"],
                redmine_payload["subject"],
                tracker_id=redmine_payload.get("tracker_id"),
                priority_id=redmine_payload.get("priority_id"),
                assigned_to_id=redmine_payload.get("assigned_to_id"),
                description=redmine_payload.get("description"),
            )
        except Exception as exc:
            return {"success": False, "error": f"create_issue failed: {exc}", "payload": redmine_payload}

        internal_issue_id = int(getattr(issue, "id", 0) or 0)
        link_id = self.db.insert_internal_issue_link({
            "source_issue_id": source_issue_id,
            "case_id": case_id,
            "internal_issue_id": internal_issue_id,
            "created_by": payload.get("created_by") or "",
            "payload": redmine_payload,
        })
        return {"success": True, "internal_issue_id": internal_issue_id, "link_id": link_id, "payload": redmine_payload}

    @staticmethod
    def _render_description(*, summary: str, root_cause: str, solution: str, related: list[int]) -> str:
        sections = [
            ("问题摘要", summary.strip() or "（待补充）"),
            ("根因分析", root_cause.strip() or "（待补充）"),
            ("解决方案", solution.strip() or "（待补充）"),
        ]
        if related:
            sections.append(("关联历史工单", " ".join(f"#{int(i)}" for i in related)))
        return "\n\n".join(f"h1. {title}\n\n{body}" for title, body in sections)
