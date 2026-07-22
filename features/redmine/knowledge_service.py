"""Application service for the Redmine knowledge base.

Bridges the issue scan store (``RedmineAgentDB``) and the knowledge base
(``RedmineKnowledgeDB``). ``batch_import`` reads already-scanned issues from
the scan store and writes structured case facts into the knowledge base — it
does not call Redmine or the AI model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from .case_evaluator import CaseEvaluator
from .case_extractor import RedmineCaseExtractor, meaningful_text
from .case_search import RedmineCaseSearch
from .internal_issue_creator import InternalIssueCreator
from .knowledge_repository import RedmineKnowledgeDB
from .mature_cases import MatureCaseBuilder
from .reply_drafter import ReplyDrafter
from .utils import parse_iso


logger = logging.getLogger(__name__)


def _parse_iso(value: Any) -> datetime | None:
    # 统一使用共享的 Redmine 时间解析函数。
    return parse_iso(value)


class RedmineKnowledgeService:
    """Orchestrates knowledge base operations for one owner."""

    def __init__(
        self,
        *,
        knowledge_db: RedmineKnowledgeDB,
        issue_repository: Any,
        agent: Any | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.knowledge_db = knowledge_db
        self.issue_repository = issue_repository
        self.agent = agent
        self.config = config or {}
        self.search = RedmineCaseSearch(knowledge_db)
        self.builder = MatureCaseBuilder(knowledge_db)
        self.evaluator = CaseEvaluator(knowledge_db)
        # 同一工单的并发 AI 分析只执行一次。
        self._agent_reply_inflight: dict[int, asyncio.Task] = {}
        self._agent_reply_fresh_hours = float(self.config.get("agent_reply_fresh_hours", 24))

    # Config helpers

    def _show_internal_refs(self) -> bool:
        return bool(self.config.get("show_internal_refs_to_customer", False))

    def _allow_internal_create(self) -> bool:
        return bool(self.config.get("allow_internal_issue_create", True))

    # Batch import (no network, no AI)

    async def batch_import_cases(self, issue_ids: list[int], *, reanalyze: bool = True, fetch_missing: bool = True) -> dict[str, Any]:
        """Import issues into the knowledge base.

        Reads already-scanned issues from the local store. When an issue_id is
        not present locally and ``fetch_missing`` is set (and an agent is
        configured), it is fetched from Redmine and analyzed first — this is
        the core entry point for importing historical ticket numbers pasted by
        the operator.
        """
        issue_ids = [int(i) for i in (issue_ids or []) if i]
        items: list[dict[str, Any]] = []
        done = 0
        failed = 0
        for issue_id in issue_ids:
            try:
                issue = self.issue_repository.get_issue(issue_id)
                if not issue and fetch_missing and self.agent is not None:
                    issue = await self._fetch_and_analyze_issue(issue_id)
                if not issue:
                    items.append({"issue_id": issue_id, "status": "not_found"})
                    failed += 1
                    continue
                if not reanalyze:
                    existing = self.knowledge_db.get_case_fact(issue_id)
                    if existing:
                        items.append({"issue_id": issue_id, "status": "exists"})
                        continue
                fact = RedmineCaseExtractor.extract(issue)
                self.knowledge_db.upsert_case_fact(fact)
                items.append({"issue_id": issue_id, "status": "done", "module": fact.get("module")})
                done += 1
            except Exception as exc:
                logger.warning("[KnowledgeService] import %s failed: %s", issue_id, exc)
                items.append({"issue_id": issue_id, "status": "failed", "error": str(exc)})
                failed += 1
        return {"success": True, "done": done, "failed": failed, "items": items}

    async def _fetch_and_analyze_issue(self, issue_id: int) -> dict[str, Any] | None:
        """Fetch a missing issue from Redmine, analyze it, and return its stored row."""
        if self.agent is None:
            return None
        client = self.agent._make_client()
        try:
            await self.agent.analyze_issue(client, issue_id, run_id=f"kb-fetch-{issue_id}")
        except Exception as exc:
            logger.warning("[KnowledgeService] online fetch %s failed: %s", issue_id, exc)
            return None
        finally:
            await client.close()
        return self.issue_repository.get_issue(issue_id)

    async def import_recent_assigned(self, *, limit: int = 20, assigned_like: str = "", reanalyze: bool = True) -> dict[str, Any]:
        """Import the most recent N scanned issues (optionally filtered by assignee)."""
        issues = self.issue_repository.list_all_issues(limit=max(1, min(int(limit), 500)), offset=0, sort="updated_on", order="desc")
        if assigned_like:
            issues = [i for i in issues if assigned_like in str(i.get("assigned_to_name") or "")]
            issues = issues[: max(1, min(int(limit), 500))]
        return await self.batch_import_cases([int(i["issue_id"]) for i in issues if i.get("issue_id")], reanalyze=reanalyze)

    async def import_single_case(self, issue_id: int, *, reanalyze: bool = True) -> dict[str, Any]:
        result = await self.batch_import_cases([issue_id], reanalyze=reanalyze)
        item = (result.get("items") or [{}])[0]
        return {"success": item.get("status") in ("done", "exists"), **item}

    # Case facts

    def get_case_fact(self, issue_id: int) -> dict[str, Any] | None:
        return self.knowledge_db.get_case_fact(int(issue_id))

    def get_case_facts_for_issue_ids(self, issue_ids: list[int]) -> dict[int, dict[str, Any]]:
        return self.knowledge_db.get_case_facts_for_issue_ids(issue_ids)

    def list_case_facts(self, *, limit: int = 50, offset: int = 0, module: str = "", search: str = "") -> dict[str, Any]:
        items = self.knowledge_db.list_case_facts(limit=limit, offset=offset, module=module, search=search)
        total = self.knowledge_db.count_case_facts(module=module, search=search)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def search_similar(self, query_or_issue: dict[str, Any] | str, *, limit: int = 10, exclude_issue_id: int = 0) -> list[dict[str, Any]]:
        similar = self.search.search_similar(query_or_issue, limit=limit, exclude_issue_id=exclude_issue_id)
        if len(similar) < max(3, min(limit, 6)):
            self._seed_similar_case_facts(query_or_issue, exclude_issue_id=exclude_issue_id, limit=max(limit * 3, 20))
            similar = self.search.search_similar(query_or_issue, limit=limit, exclude_issue_id=exclude_issue_id)
        return similar

    def similar_for_issue(self, issue_id: int, *, limit: int = 10) -> dict[str, Any]:
        issue = self.issue_repository.get_issue(int(issue_id)) or {}
        if not issue:
            # Fall back to the stored case fact if the issue scan row is gone.
            fact = self.knowledge_db.get_case_fact(int(issue_id)) or {}
            issue = fact
        similar = self.search_similar(issue, limit=limit, exclude_issue_id=int(issue_id)) if issue else []
        return {"issue_id": int(issue_id), "similar": similar}

    def issue_workbench(self, issue_id: int, *, similar_limit: int = 6) -> dict[str, Any]:
        """Return a ticket-centered knowledge package for the issue list UI.

        This keeps the issue list as the main workbench: one call returns the
        structured fact, evidence from journals/attachments, similar historical
        tickets, best mature case, and latest evaluation/reference state.
        """
        issue_id = int(issue_id)
        issue = self.issue_repository.get_issue(issue_id) or {}
        fact = self.knowledge_db.get_case_fact(issue_id)
        if issue and fact is None:
            fact = RedmineCaseExtractor.extract(issue)
            self.knowledge_db.upsert_case_fact(fact)
        fact = fact or {}
        probe = issue or fact
        similar = self.search_similar(probe, limit=similar_limit, exclude_issue_id=issue_id) if probe else []
        mature_case = ReplyDrafter(self.knowledge_db).find_best_mature_case(fact, similar) if fact else None
        references = self.knowledge_db.get_reference_outputs(issue_id)
        evaluation = self.knowledge_db.get_latest_case_evaluation(issue_id)
        return {
            "issue_id": issue_id,
            "subject": issue.get("subject") or fact.get("subject") or "",
            "status_name": issue.get("status_name") or fact.get("status_name") or "",
            "fact": fact,
            "evidence": self._issue_evidence(issue, fact),
            "similar": similar,
            "mature_case": self._compact_mature_case(mature_case),
            "reference_count": len(references),
            "latest_evaluation": evaluation,
            "gms_like_sections": self._gms_like_sections(issue, fact, mature_case, similar),
        }

    # Mature cases

    def build_mature_case(self, issue_ids: list[int], *, title: str = "") -> dict[str, Any]:
        return self.builder.build_from_issues(issue_ids, title=title)

    def list_mature_cases(self, *, limit: int = 50, offset: int = 0, status: str = "", search: str = "") -> dict[str, Any]:
        items = self.knowledge_db.list_mature_cases(limit=limit, offset=offset, status=status, search=search)
        total = self.knowledge_db.count_mature_cases(status=status, search=search)
        for item in items:
            item["links"] = self.knowledge_db.list_links_for_case(int(item["case_id"]))
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_mature_case(self, case_id: int) -> dict[str, Any] | None:
        case = self.knowledge_db.get_mature_case(int(case_id))
        if case:
            case["links"] = self.knowledge_db.list_links_for_case(int(case_id))
        return case

    def approve_mature_case(self, case_id: int, approved_by: str) -> bool:
        return self.knowledge_db.approve_mature_case(int(case_id), approved_by)

    # Issue workbench evidence helpers

    def _issue_evidence(self, issue: dict[str, Any], fact: dict[str, Any]) -> dict[str, Any]:
        journals = issue.get("journals_json") or []
        attachments = issue.get("attachments_json") or []
        failures = issue.get("failures_json") or []
        return {
            "reply_summary": self._summarize_journals(journals),
            "attachment_summary": self._summarize_attachments(attachments),
            "failure_summary": self._summarize_failures(failures),
            "source_excerpt": {
                "description": str(issue.get("description") or fact.get("evidence_json", {}).get("redmine_description") or "")[:1200],
                "error_info": str(issue.get("error_info") or "")[:1200],
            },
        }

    @staticmethod
    def _summarize_journals(journals: list[Any], *, limit: int = 6) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for journal in journals or []:
            if not isinstance(journal, dict):
                continue
            notes = str(journal.get("notes") or "").strip()
            details = journal.get("details") or []
            if not notes and not details:
                continue
            item = {
                "id": journal.get("id") or "",
                "user": journal.get("user") or "",
                "created_on": journal.get("created_on") or "",
                "notes": notes[:900],
                "details": [
                    {
                        "name": d.get("name") or "",
                        "old_value": d.get("old_value") or "",
                        "new_value": d.get("new_value") or "",
                    }
                    for d in details[:5]
                    if isinstance(d, dict)
                ],
            }
            items.append(item)
        return items[-limit:]

    @staticmethod
    def _summarize_attachments(attachments: list[Any], *, limit: int = 10) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for attachment in attachments or []:
            if not isinstance(attachment, dict):
                continue
            analysis = attachment.get("analysis_json") or {}
            failures = analysis.get("failures") or []
            details = analysis.get("details") or {}
            detected = details.get("detected_errors") or []
            excerpt = analysis.get("text_excerpt") or ""
            items.append({
                "attachment_id": attachment.get("attachment_id") or "",
                "filename": attachment.get("filename") or "",
                "status": attachment.get("status") or "",
                "content_type": attachment.get("content_type") or "",
                "parsed": bool(analysis.get("parsed")),
                "type": details.get("type") or "",
                "detected_errors": detected[:6],
                "certification_type": details.get("certification_type") or "",
                "failures": [
                    {
                        "name": f.get("name") or "",
                        "module": f.get("module") or "",
                        "reason": str(f.get("reason") or "")[:500],
                    }
                    for f in failures[:4]
                    if isinstance(f, dict)
                ],
                "text_excerpt": str(excerpt or "")[:900],
            })
        return items[:limit]

    @staticmethod
    def _summarize_failures(failures: list[Any], *, limit: int = 6) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for failure in failures or []:
            if not isinstance(failure, dict):
                continue
            result.append({
                "module": str(failure.get("module") or ""),
                "name": str(failure.get("name") or failure.get("test_name") or ""),
                "reason": str(failure.get("reason") or failure.get("error_message") or "")[:700],
            })
        return result[:limit]

    @staticmethod
    def _compact_mature_case(case: dict[str, Any] | None) -> dict[str, Any] | None:
        if not case:
            return None
        return {
            "case_id": case.get("case_id"),
            "title": case.get("title") or "",
            "status": case.get("status") or "",
            "canonical_error_signature": case.get("canonical_error_signature") or "",
            "module": case.get("module") or "",
            "root_cause": case.get("root_cause") or "",
            "source_issue_ids": case.get("source_issue_ids_json") or [],
            "confidence": case.get("confidence") or 0,
        }

    def _gms_like_sections(
        self,
        issue: dict[str, Any],
        fact: dict[str, Any],
        mature_case: dict[str, Any] | None,
        similar: list[dict[str, Any]],
    ) -> dict[str, Any]:
        solution = ""
        if mature_case:
            solution_json = mature_case.get("solution_json") or {}
            if isinstance(solution_json, dict):
                solution = str(solution_json.get("overview") or "")
        solution = solution or str(fact.get("solution") or issue.get("solution") or "")
        return {
            "title": issue.get("subject") or fact.get("subject") or "",
            "scope": {
                "chip_platform": fact.get("chip_platform") or issue.get("soc_platform") or "",
                "android_version": fact.get("android_version") or issue.get("android_version") or "",
                "test_version": fact.get("certification_type") or "",
                "module": fact.get("module") or "",
                "product_form": fact.get("product_form") or "",
                "region": fact.get("region") or "",
            },
            "symptoms": fact.get("symptoms_json") or fact.get("symptoms") or [],
            "root_cause": (mature_case or {}).get("root_cause") or fact.get("root_cause") or issue.get("error_analysis") or "",
            "solution": solution,
            "verification": fact.get("verification") or "",
            "rules": (mature_case or {}).get("rules_json") or [],
            "source_issue_ids": (mature_case or {}).get("source_issue_ids_json") or ([int(fact["issue_id"])] if fact.get("issue_id") else []),
        }

    # Reply drafting

    def draft_reply(self, issue_id: int, *, mature_case_id: int | None = None) -> dict[str, Any]:
        issue = self.issue_repository.get_issue(int(issue_id)) or {}
        if not issue:
            fact = self.knowledge_db.get_case_fact(int(issue_id)) or {}
            issue = fact
        # Caller may pin a specific mature case (e.g. clicked from the case
        # detail view) instead of relying on automatic matching.
        mature_case = None
        if mature_case_id:
            mature_case = self.knowledge_db.get_mature_case(int(mature_case_id))
        drafter = ReplyDrafter(self.knowledge_db, show_internal_refs=self._show_internal_refs())
        return drafter.draft_reply(issue, exclude_issue_id=int(issue_id), mature_case=mature_case)

    # Agent 回复：在线获取、AI 分析和知识库匹配。

    async def draft_agent_reply(
        self,
        issue_id: int,
        *,
        force: bool = False,
        mature_case_id: int | None = None,
        similar_limit: int = 6,
    ) -> dict[str, Any]:
        """结合在线分析、成熟案例和相似工单生成回复与补丁方向。"""
        issue_id = int(issue_id)

        # Step 1 — fresh analysis (deduped per issue).
        issue = await self._ensure_analyzed(issue_id, force=force)
        if not issue:
            return {
                "issue_id": issue_id,
                "source": "not_found",
                "error": "无法从 Redmine 拉取或分析该工单(请检查 Redmine 凭据/网络)",
            }

        # Step 2 — AI-produced fields.
        ai_json = issue.get("ai_json") or {}
        root_cause = self._meaningful(issue.get("error_analysis")) or self._meaningful(ai_json.get("root_cause_guess")) or ""
        solution = self._meaningful(issue.get("solution")) or self._meaningful(ai_json.get("solution")) or ""
        patch_raw = self._meaningful(issue.get("patch_direction")) or self._meaningful(ai_json.get("patch_direction")) or ""
        patch_block = self._wrap_patch_block(patch_raw)

        # Step 3 — knowledge-base match.
        fact = RedmineCaseExtractor.extract(issue)
        similar = self.search_similar(fact or issue, limit=similar_limit, exclude_issue_id=issue_id)
        drafter = ReplyDrafter(self.knowledge_db, show_internal_refs=self._show_internal_refs())
        mature_case = None
        if mature_case_id:
            mature_case = self.knowledge_db.get_mature_case(int(mature_case_id))
        if mature_case is None:
            mature_case = drafter.find_best_mature_case(fact, similar)

        # Step 4 — assemble by signal priority.
        if mature_case:
            reply_body = drafter._render_from_mature_case(mature_case, issue_id, similar)
            source = "mature_case"
        elif self._meaningful(root_cause) or self._meaningful(solution) or self._meaningful(patch_raw):
            reply_body = self._render_agent_reply(issue, root_cause, solution, patch_block, similar)
            source = "ai_analysis"
        else:
            reply_body = drafter._render_from_similar(issue, fact, similar)
            source = "similar_issues"

        return {
            "issue_id": issue_id,
            "source": source,
            "subject": issue.get("subject") or "",
            "module": fact.get("module") or "",
            "error_signature": fact.get("error_signature") or "",
            "root_cause": root_cause,
            "solution": solution,
            "patch_direction": patch_block,
            "reply_draft": reply_body,
            "mature_case_id": (mature_case or {}).get("case_id"),
            "similar_issues": [
                {
                    "issue_id": s.get("issue_id"),
                    "subject": s.get("subject"),
                    "score": s.get("score"),
                    "module": s.get("module") or "",
                }
                for s in similar[:5]
            ],
            "analysis_status": issue.get("analysis_status"),
            "analyzed_at": issue.get("last_scanned_at") or "",
        }

    def _seed_similar_case_facts(self, query_or_issue: dict[str, Any] | str, *, exclude_issue_id: int = 0, limit: int = 20) -> None:
        """Backfill case facts from the local issue snapshot store.

        The personal knowledge DB can be much smaller than the synced Redmine
        issue DB. For reply drafting we should still reuse high-value local
        historical tickets such as VBMeta fixes even if the operator has not
        imported them as mature cases yet.
        """
        query = self._issue_store_query(query_or_issue)
        if not query:
            return
        try:
            candidates = self.issue_repository.search_similar(query, int(exclude_issue_id or 0), limit=max(1, min(int(limit or 20), 80)))
        except Exception as exc:
            logger.debug("[KnowledgeService] seed similar facts skipped: %s", exc)
            return
        for issue in candidates:
            try:
                issue_id = int(issue.get("issue_id") or 0)
                if not issue_id or (exclude_issue_id and issue_id == int(exclude_issue_id)):
                    continue
                if self.knowledge_db.get_case_fact(issue_id):
                    continue
                fact = RedmineCaseExtractor.extract(issue)
                if fact.get("error_signature") or fact.get("solution") or fact.get("reply_template"):
                    self.knowledge_db.upsert_case_fact(fact)
            except Exception as exc:
                logger.debug("[KnowledgeService] seed case fact failed: %s", exc)

    @staticmethod
    def _issue_store_query(query_or_issue: dict[str, Any] | str) -> str:
        if isinstance(query_or_issue, str):
            return query_or_issue.strip()
        return " ".join(filter(None, [
            str(query_or_issue.get("subject") or ""),
            str(query_or_issue.get("description") or ""),
            str(query_or_issue.get("summary") or ""),
            str(query_or_issue.get("problem_summary") or ""),
            str(query_or_issue.get("error_info") or ""),
            str(query_or_issue.get("error_analysis") or ""),
            str(query_or_issue.get("root_cause") or ""),
            str(query_or_issue.get("solution") or ""),
            str(query_or_issue.get("error_signature") or ""),
            str(query_or_issue.get("module") or ""),
        ])).strip()

    async def _ensure_analyzed(self, issue_id: int, *, force: bool) -> dict[str, Any] | None:
        """Step 1 helper — per-issue concurrent-analysis dedup."""
        inflight = self._agent_reply_inflight.get(issue_id)
        if inflight is not None and not inflight.done():
            try:
                return await asyncio.shield(inflight)
            except Exception:
                pass  # transient; fall through to our own attempt
        task = asyncio.ensure_future(self._resolve_issue_analysis(issue_id, force=force))
        self._agent_reply_inflight[issue_id] = task
        try:
            return await task
        finally:
            self._agent_reply_inflight.pop(issue_id, None)

    async def _resolve_issue_analysis(self, issue_id: int, *, force: bool) -> dict[str, Any] | None:
        issue = self.issue_repository.get_issue(issue_id)
        need = force or (not issue) or self._is_analysis_stale(issue or {})
        if need:
            fetched = await self._fetch_and_analyze_issue(issue_id)
            issue = fetched or issue
        return issue

    def _is_analysis_stale(self, issue: dict[str, Any]) -> bool:
        if (issue or {}).get("analysis_status") != "done":
            return True
        scanned = _parse_iso(issue.get("last_scanned_at"))
        if not scanned:
            return True
        if (datetime.utcnow() - scanned) > timedelta(hours=self._agent_reply_fresh_hours):
            return True
        updated = _parse_iso(issue.get("updated_on"))
        # Redmine side has new activity since our last scan → must re-fetch.
        return bool(updated and updated > scanned)

    @staticmethod
    def _meaningful(value: Any) -> str:
        return meaningful_text(value)

    def _render_agent_reply(self, issue: dict[str, Any], root_cause: str, solution: str, patch_block: str, similar: list[dict[str, Any]]) -> str:
        issue_id = issue.get("issue_id") or ""
        subject = issue.get("subject") or ""
        lines = [
            f"您好，关于 #{issue_id} {subject}，结合日志与历史经验，初步分析如下：",
            "",
        ]
        if root_cause:
            lines += ["**根因分析：**", root_cause, ""]
        if solution:
            lines += ["**建议处理步骤：**", solution, ""]
        if patch_block:
            lines += ["**补丁方向（参考，需按实际代码核对）：**", patch_block, ""]
        if similar:
            lines += [f"已匹配 {len(similar)} 条相似历史工单（默认不对客户展示单号），结论与上方一致。"]
        lines += ["", "如有进一步日志请提供，可继续协助确认根因，谢谢。"]
        return "\n".join(lines)

    @staticmethod
    def _wrap_patch_block(text: str) -> str:
        text = str(text or "").strip()
        if not text or text.startswith("```"):
            return text
        if re.search(r"^---\s+[ab]/", text, re.M) or re.search(r"^\+\+\+\s+[ab]/", text, re.M):
            return f"```diff\n{text}\n```"
        if re.search(r"^\$\s+", text, re.M):
            return f"```shell\n{text}\n```"
        if re.search(r"<\?xml|<[\w:-]+\s+[^>]*>", text):
            return f"```xml\n{text}\n```"
        return f"```\n{text}\n```"

    # Reference + evaluation (off the production path)

    def import_reference_output(self, issue_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        ref_id = self.evaluator.import_reference_output(int(issue_id), payload)
        return {"reference_id": ref_id, "issue_id": int(issue_id)}

    def list_reference_outputs(self, issue_id: int) -> list[dict[str, Any]]:
        return self.knowledge_db.get_reference_outputs(int(issue_id))

    def evaluate_case(self, issue_id: int, *, reference: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.evaluator.evaluate_case(int(issue_id), reference=reference)

    def latest_evaluation(self, issue_id: int) -> dict[str, Any] | None:
        return self.knowledge_db.get_latest_case_evaluation(int(issue_id))

    # Internal issue creation (confirmed, configurable)

    async def create_internal_from_issue(self, source_issue_id: int, payload: dict[str, Any], *, confirmed: bool) -> dict[str, Any]:
        client = None
        if self.agent is not None and confirmed:
            try:
                client = self.agent._make_client()
            except Exception as exc:
                return {"success": False, "error": f"redmine client unavailable: {exc}"}
        creator = InternalIssueCreator(self.knowledge_db, client=client, allow_create=self._allow_internal_create())
        try:
            return await creator.from_issue(int(source_issue_id), payload=payload, confirmed=confirmed)
        finally:
            if client is not None:
                await client.close()

    async def create_internal_from_case(self, case_id: int, payload: dict[str, Any], *, confirmed: bool) -> dict[str, Any]:
        client = None
        if self.agent is not None and confirmed:
            try:
                client = self.agent._make_client()
            except Exception as exc:
                return {"success": False, "error": f"redmine client unavailable: {exc}"}
        creator = InternalIssueCreator(self.knowledge_db, client=client, allow_create=self._allow_internal_create())
        try:
            return await creator.from_mature_case(int(case_id), payload=payload, confirmed=confirmed)
        finally:
            if client is not None:
                await client.close()


def safe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value
