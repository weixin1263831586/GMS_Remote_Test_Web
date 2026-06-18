from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from features.automation.executors import (
    HttpAutomationExecutor,
    StubAutomationExecutor,
)
from features.automation.gerrit_trigger import (
    match_profiles,
    normalize_gerrit_event,
    profile_matches_event,
)
from features.automation.models import AutomationRunCreateRequest
from features.automation.orchestrator import AutomationOrchestrator
from features.automation.profiles import load_profiles, upsert_profile
from features.automation.repository import AutomationStore


GerritQuery = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


class AutomationNotFoundError(LookupError):
    pass


class AutomationService:
    def __init__(
        self,
        *,
        store: AutomationStore,
        profiles_path: Path,
        gerrit_query: GerritQuery | None = None,
    ):
        self.store = store
        self.profiles_path = profiles_path
        self.gerrit_query = gerrit_query

    @staticmethod
    def new_run_id() -> str:
        return f'ats_{uuid.uuid4().hex[:12]}'

    def orchestrator(self, executor_name: str = 'stub') -> AutomationOrchestrator:
        executor = (
            HttpAutomationExecutor()
            if executor_name == 'http'
            else StubAutomationExecutor()
        )
        return AutomationOrchestrator(self.store, executor)

    def list_profiles(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return load_profiles(self.profiles_path, enabled_only=enabled_only)

    def save_profile(self, request: dict[str, Any]) -> dict[str, Any]:
        return upsert_profile(self.profiles_path, request or {})

    def dry_run_profile(
        self,
        profile_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        profile = next(
            (
                item
                for item in self.list_profiles()
                if item.get('id') == profile_id
            ),
            None,
        )
        if profile is None:
            raise AutomationNotFoundError('Automation profile not found')
        event = normalize_gerrit_event(
            {
                'type': 'dry-run',
                'change': {
                    'project': request.get('project', ''),
                    'branch': request.get('branch', ''),
                    'number': (
                        request.get('change_id')
                        or request.get('number')
                        or ''
                    ),
                    'subject': request.get('subject', ''),
                    'owner': {'email': request.get('owner', '')},
                },
                'patchSet': {
                    'number': request.get('patchset', ''),
                    'revision': request.get('revision', ''),
                },
            }
        )
        matched = profile_matches_event(profile, event)
        run_request = (
            self._run_request_from_gerrit_event(event, profile).model_dump()
            if matched
            else {}
        )
        return {
            'matched': matched,
            'event': event,
            'profile': profile,
            'run_request': run_request,
        }

    def create_run(self, request: dict[str, Any]) -> dict[str, Any]:
        create_request = AutomationRunCreateRequest(**(request or {}))
        run = self.store.create_run(
            create_request.to_run_dict(self.new_run_id())
        )
        self.store.append_event(
            run['id'],
            run['status'],
            'info',
            'Automation run queued',
            {'profile_id': run['profile_id']},
        )
        return run

    def list_runs(self, *, status: str = '', limit: int = 50):
        return self.store.list_runs(status=status, limit=limit)

    def get_run(self, run_id: str):
        run = self.store.get_run(run_id)
        if run is None:
            raise AutomationNotFoundError('Automation run not found')
        return run

    def list_events(self, run_id: str):
        self.get_run(run_id)
        return self.store.list_events(run_id)

    def cancel_run(self, run_id: str):
        try:
            return self.orchestrator().cancel_run(run_id)
        except ValueError as exc:
            raise AutomationNotFoundError('Automation run not found') from exc

    def retry_run(self, run_id: str):
        old = self.get_run(run_id)
        create_request = AutomationRunCreateRequest(
            profile_id=old['profile_id'],
            source_type=old['source_type'],
            project=old['project'],
            branch=old['branch'],
            gerrit_change_id=old['gerrit_change_id'],
            gerrit_patchset=old['gerrit_patchset'],
            gerrit_subject=old['gerrit_subject'],
            owner=old['owner'],
            artifact_url=old['artifact_url'],
            artifact_path=old['artifact_path'],
            devices=[],
            test_plan={},
        )
        run_data = create_request.to_run_dict(self.new_run_id())
        run_data['devices_json'] = old['devices_json']
        run_data['test_plan_json'] = old['test_plan_json']
        run = self.store.create_run(run_data)
        self.store.append_event(
            run['id'],
            run['status'],
            'info',
            f'Retry created from {run_id}',
            {'source_run_id': run_id},
        )
        return run

    def worker_tick(self, executor_name: str = 'stub'):
        return self.orchestrator(executor_name).advance_next()

    def handle_gerrit_webhook(self, payload: dict[str, Any]):
        event = normalize_gerrit_event(payload or {})
        result = self._create_runs_for_event(
            event,
            self.list_profiles(enabled_only=True),
        )
        return {'event': event, **result}

    async def poll_gerrit_changes(self, limit: int = 100):
        if self.gerrit_query is None:
            raise RuntimeError('Gerrit query provider is not configured')
        created = []
        existing = []
        events = []
        for profile in self.list_profiles(enabled_only=True):
            gerrit = (
                profile.get('gerrit')
                if isinstance(profile.get('gerrit'), dict)
                else {}
            )
            query = str(gerrit.get('query') or '').strip()
            if not query:
                continue
            for change in await self.gerrit_query(query, limit):
                event = self._gerrit_change_to_event(change)
                events.append(event)
                result = self._create_runs_for_event(event, [profile])
                created.extend(result['created'])
                existing.extend(result['existing'])
        return {
            'events': events,
            'created': created,
            'existing': existing,
            'created_count': len(created),
            'existing_count': len(existing),
        }

    @staticmethod
    def _format_template_map(
        raw: dict[str, Any],
        event: dict[str, Any],
    ) -> dict[str, Any]:
        formatted = {}
        for key, value in (raw or {}).items():
            if not isinstance(value, str):
                formatted[key] = value
                continue
            try:
                formatted[key] = value.format(
                    gerrit_change_id=event.get('change_id', ''),
                    gerrit_patchset=event.get('patchset', ''),
                    project=event.get('project', ''),
                    branch=event.get('branch', ''),
                    revision=event.get('revision', ''),
                )
            except (IndexError, KeyError, ValueError):
                formatted[key] = value
        return formatted

    def _run_request_from_gerrit_event(
        self,
        event: dict[str, Any],
        profile: dict[str, Any],
    ) -> AutomationRunCreateRequest:
        jenkins = profile.get('jenkins') if isinstance(
            profile.get('jenkins'),
            dict,
        ) else {}
        test_plan = profile.get('test_plan') if isinstance(
            profile.get('test_plan'),
            dict,
        ) else {}
        flash = profile.get('flash') if isinstance(
            profile.get('flash'),
            dict,
        ) else {}
        device_selector = profile.get('device_selector') if isinstance(
            profile.get('device_selector'),
            dict,
        ) else {}
        reporting = profile.get('reporting') if isinstance(
            profile.get('reporting'),
            dict,
        ) else {}
        return AutomationRunCreateRequest(
            profile_id=profile.get('id', ''),
            source_type=event.get('source_type', 'gerrit_webhook'),
            source_key=f"{event.get('source_key', '')}:{profile.get('id', '')}",
            project=event.get('project', ''),
            branch=event.get('branch', ''),
            gerrit_change_id=event.get('change_id', ''),
            gerrit_patchset=event.get('patchset', ''),
            gerrit_subject=event.get('subject', ''),
            owner=event.get('owner', ''),
            test_plan={
                **test_plan,
                'flash': flash,
                'device_selector': device_selector,
                'reporting': reporting,
                'jenkins': {
                    **jenkins,
                    'parameters': self._format_template_map(
                        jenkins.get('parameters') or {},
                        event,
                    ),
                    'artifact_pattern': jenkins.get(
                        'artifact_pattern',
                        '',
                    ),
                },
            },
        )

    @staticmethod
    def _gerrit_change_to_event(change: dict[str, Any]) -> dict[str, Any]:
        revision = str(
            change.get('current_revision') or change.get('revision') or ''
        )
        revisions = (
            change.get('revisions')
            if isinstance(change.get('revisions'), dict)
            else {}
        )
        revision_info = revisions.get(revision) if revision else {}
        patchset = str(
            change.get('patchset')
            or change.get('patch_set')
            or (revision_info or {}).get('_number')
            or (revision_info or {}).get('number')
            or ''
        )
        return normalize_gerrit_event(
            {
                'type': 'poll',
                'change': {
                    'project': change.get('project', ''),
                    'branch': change.get('branch', ''),
                    'number': (
                        change.get('number')
                        or change.get('_number')
                        or change.get('id')
                        or ''
                    ),
                    'subject': change.get('subject', ''),
                    'owner': change.get('owner') or {},
                },
                'patchSet': {'number': patchset, 'revision': revision},
            }
        )

    def _create_runs_for_event(
        self,
        event: dict[str, Any],
        profiles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        matches = match_profiles(event, profiles)
        created = []
        existing = []
        for profile in matches:
            create_request = self._run_request_from_gerrit_event(
                event,
                profile,
            )
            old_run = self.store.get_run_by_source_key(
                create_request.source_key
            )
            if old_run:
                existing.append(old_run)
                continue
            run_data = create_request.to_run_dict(self.new_run_id())
            jenkins = (
                profile.get('jenkins')
                if isinstance(profile.get('jenkins'), dict)
                else {}
            )
            run_data['jenkins_job'] = str(jenkins.get('job') or '')
            run_data['test_plan_json'] = json.dumps(
                create_request.test_plan or {},
                ensure_ascii=False,
                separators=(',', ':'),
            )
            run = self.store.create_run(run_data)
            self.store.append_event(
                run['id'],
                run['status'],
                'info',
                'Gerrit event matched automation profile',
                {'event': event, 'profile_id': profile.get('id', '')},
            )
            created.append(run)
        return {
            'matched_profiles': [
                profile.get('id', '') for profile in matches
            ],
            'created': created,
            'existing': existing,
        }
