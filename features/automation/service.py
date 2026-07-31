from __future__ import annotations

import copy
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from features.automation.executors import (
    HttpAutomationExecutor,
    StubAutomationExecutor,
)
from features.automation.gerrit_trigger import (
    match_profiles,
    normalize_gerrit_event,
)
from features.automation.models import TERMINAL_STATUSES, AutomationRunCreateRequest
from features.automation.orchestrator import AutomationOrchestrator
from features.automation.profile_dry_run import dry_run_profile
from features.automation.profiles import load_profiles, upsert_profile
from features.automation.repository import AutomationStore
from foundation.secrets import decrypt_secret, encrypt_secret


GerritQuery = Callable[[str, str, int], Awaitable[list[dict[str, Any]]]]


class AutomationNotFoundError(LookupError):
    pass


class AutomationService:
    def __init__(
        self,
        *,
        store: AutomationStore,
        profiles_path: Path,
        gerrit_query: GerritQuery | None = None,
        device_selector: Any = None,
        device_manager: Any = None,
        cluster_provider: Callable[[], Any] | None = None,
    ):
        self.store = store
        self.profiles_path = profiles_path
        self.gerrit_query = gerrit_query
        self._device_selector = device_selector
        self._device_manager = device_manager
        self._cluster_provider = cluster_provider

    @staticmethod
    def new_run_id() -> str:
        return f'ats_{uuid.uuid4().hex[:12]}'

    def orchestrator(self, executor_name: str = 'stub') -> AutomationOrchestrator:
        executor = (
            HttpAutomationExecutor(
                build_password_provider=self.get_build_password,
                device_selector=self._device_selector,
                device_manager=self._device_manager,
            )
            if executor_name == 'http'
            else StubAutomationExecutor()
        )
        return AutomationOrchestrator(self.store, executor)

    def get_build_password(self, run_id: str) -> str:
        encrypted = self.store.get_run_secret(run_id, "build_server_password")
        return decrypt_secret(encrypted) if encrypted else ""

    def list_profiles(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return load_profiles(self.profiles_path, enabled_only=enabled_only)

    def save_profile(self, request: dict[str, Any]) -> dict[str, Any]:
        return upsert_profile(self.profiles_path, request or {})

    def dry_run_profile(
        self,
        profile_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        return dry_run_profile(
            self,
            profile_id,
            request,
            not_found_error=AutomationNotFoundError,
        )

    def create_run(
        self, request: dict[str, Any], *, created_by: str = ""
    ) -> dict[str, Any]:
        # 深拷贝后再移除密码，避免修改调用方请求。
        # API bodies are usually disposable, but programmatic callers reuse
        # these dictionaries after validation errors and for audit logging.
        body = copy.deepcopy(request or {})
        build_password = str(body.pop('build_server_password', '') or '')
        test_plan = body.get('test_plan')
        if isinstance(test_plan, dict):
            build = test_plan.get('build')
            if isinstance(build, dict):
                build_password = build_password or str(build.pop('server_password', '') or '')
        plan = test_plan if isinstance(test_plan, dict) else {}
        build_plan = plan.get('build') if isinstance(plan.get('build'), dict) else {}
        flash_plan = plan.get('flash') if isinstance(plan.get('flash'), dict) else {}
        has_build = bool(build_plan.get('provider') or build_plan.get('server_id') or build_plan.get('template_id'))
        has_artifact = bool(str(body.get('artifact_path') or body.get('artifact_url') or '').strip())
        if flash_plan.get('mode') != 'skip' and not (has_build or has_artifact or body.get('jenkins_job')):
            raise ValueError('Firmware artifact or build configuration is required')
        if not str(plan.get('test_type') or '').strip():
            raise ValueError('test_plan.test_type is required')
        self._prepare_cluster_plan(body, plan)
        create_request = AutomationRunCreateRequest(**body)
        run_data = create_request.to_run_dict(self.new_run_id())
        run_data["created_by"] = str(created_by or "")
        encrypted_secrets = {}
        if build_password:
            encrypted_secrets["build_server_password"] = encrypt_secret(build_password)
        run = self.store.create_run(run_data, encrypted_secrets=encrypted_secrets)
        self.store.append_event(
            run['id'], run['status'], 'info', 'Automation run queued',
            {'profile_id': run['profile_id']},
            event_type='run.created', operation_id=f"{run['id']}:create",
            from_status='',
            to_status=run['status'],
        )
        return run

    def preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        """Validate a proposed run against live Worker inventory without creating it."""
        body = copy.deepcopy(request or {})
        body.pop('build_server_password', None)
        plan = body.get('test_plan') if isinstance(body.get('test_plan'), dict) else {}
        if not str(plan.get('test_type') or '').strip():
            raise ValueError('test_plan.test_type is required')
        build = plan.get('build') if isinstance(plan.get('build'), dict) else {}
        flash = plan.get('flash') if isinstance(plan.get('flash'), dict) else {}
        has_build = bool(
            build.get('provider') or build.get('server_id') or build.get('template_id')
        )
        has_artifact = bool(
            str(body.get('artifact_path') or body.get('artifact_url') or '').strip()
        )
        if flash.get('mode') != 'skip' and not (
            has_build or has_artifact or body.get('jenkins_job')
        ):
            raise ValueError('Firmware artifact or build configuration is required')
        cluster = self._prepare_cluster_plan(body, plan)
        return {
            'ready': True,
            'worker_id': plan.get('worker_id', ''),
            'test_suite': plan.get('test_suite', ''),
            'test_type': plan.get('test_type', ''),
            'devices': body.get('devices') or [],
            'flash_mode': flash.get('mode') or 'firmware',
            'build_configured': has_build or bool(body.get('jenkins_job')),
            'artifact_configured': has_artifact,
            **cluster,
        }

    def _prepare_cluster_plan(
        self, body: dict[str, Any], plan: dict[str, Any]
    ) -> dict[str, Any]:
        if self._cluster_provider is None:
            return {'runtime_checked': False}
        try:
            cluster = self._cluster_provider()
        except Exception as exc:
            raise ValueError(f'Cluster service is unavailable: {exc}') from exc
        if not cluster.effective_enabled:
            raise ValueError('Cluster mode must be enabled for durable ATS execution')

        build = plan.get('build') if isinstance(plan.get('build'), dict) else {}
        jenkins = plan.get('jenkins') if isinstance(plan.get('jenkins'), dict) else {}
        if build.get('provider') == 'ssh' or build.get('server_id'):
            from features.build import get_build_service

            build_service = get_build_service()
            server_ids = {item.get('id') for item in build_service.list_servers()}
            template_ids = {item.get('id') for item in build_service.list_templates()}
            if not build.get('server_id') or build.get('server_id') not in server_ids:
                raise ValueError(f"Build server is not configured: {build.get('server_id') or '-'}")
            if not build.get('template_id') or build.get('template_id') not in template_ids:
                raise ValueError(f"Build template is not configured: {build.get('template_id') or '-'}")
        if body.get('jenkins_job') and not str(jenkins.get('base_url') or '').strip():
            raise ValueError('test_plan.jenkins.base_url is required for a Jenkins build')

        artifact_url = str(body.get('artifact_url') or '').strip()
        if artifact_url:
            artifact_origin = urlparse(artifact_url)
            jenkins_origin = urlparse(str(jenkins.get('base_url') or ''))
            if (
                artifact_origin.scheme not in {'http', 'https'}
                or artifact_origin.scheme != jenkins_origin.scheme
                or artifact_origin.netloc != jenkins_origin.netloc
            ):
                raise ValueError('Artifact URL must use the configured Jenkins origin')
        artifact_path = str(body.get('artifact_path') or '').strip()
        if artifact_path and not build:
            from foundation.config import config_manager, settings

            path = Path(artifact_path).expanduser().resolve()
            config = config_manager.load_config()
            configured_roots = (
                (config.get('firmware_shares') or {}).get('allowed_prefixes')
                or config.get('firmware_share_allowed_prefixes')
                or ['/home/', '/data/', '/mnt/']
            )
            allowed_roots = [settings.data_root.resolve()]
            allowed_roots.extend(Path(str(root)).expanduser().resolve() for root in configured_roots)
            if not path.is_file() or not any(
                path == root or path.is_relative_to(root) for root in allowed_roots
            ):
                raise ValueError('Firmware artifact is missing or outside the configured firmware roots')
            if path.stat().st_size <= 0:
                raise ValueError('Firmware artifact is empty')

        test_type = str(plan.get('test_type') or '').strip().upper()
        worker_id = str(
            plan.get('worker_id') or cluster.config.local_worker_id
        ).strip()
        auto_selected = worker_id == 'auto'
        suite_path = str(plan.get('test_suite') or '').strip()
        selector = plan.get('device_selector') if isinstance(
            plan.get('device_selector'), dict
        ) else {}
        minimum = max(1, int(selector.get('min_count') or 1))
        flash = plan.get('flash') if isinstance(plan.get('flash'), dict) else {}
        requires_local_usb = flash.get('mode') != 'skip'
        full_suite = not (
            plan.get('retry_dir') or plan.get('test_module')
            or plan.get('test_case') or plan.get('modules')
        )
        required_memory = (
            float(os.getenv('GMS_CTS_FULL_MEMORY_GB', '28'))
            if full_suite and test_type == 'CTS' else 0
        )
        if requires_local_usb and minimum != 1:
            raise ValueError('Firmware flashing requires device_selector.min_count = 1')

        all_suites = [
            suite for suite in cluster.repository.list_suites()
            if suite.get('available')
            and (not test_type or str(suite.get('suite_type') or '').upper() == test_type)
            and (not suite_path or suite.get('tools_path') == suite_path)
        ]
        if auto_selected:
            selected = None
            last_error = ''
            for suite in sorted(
                all_suites,
                key=lambda item: (
                    str(item.get('suite_version') or ''),
                    str(item.get('last_scanned_at') or ''),
                ),
                reverse=True,
            ):
                try:
                    candidate, _ = cluster.select_worker(
                        suite.get('suite_key', ''),
                        minimum,
                        require_agent=True,
                        excluded_transports={'adb_proxy'} if requires_local_usb else None,
                    )
                except ValueError as exc:
                    last_error = str(exc)
                    continue
                selected = next(
                    (
                        item for item in all_suites
                        if item.get('worker_id') == candidate
                        and item.get('suite_key') == suite.get('suite_key')
                    ),
                    None,
                )
                if selected:
                    worker_id = candidate
                    break
            if not selected:
                raise ValueError(last_error or 'No Worker has the requested suite and idle devices')
        else:
            worker = next(
                (item for item in cluster.list_workers() if item.get('id') == worker_id),
                None,
            )
            if not worker or worker.get('status') not in {'online', 'busy'}:
                raise ValueError(f'Worker {worker_id} is not online')
            if not cluster.has_command_agent(worker_id):
                raise ValueError(f'Worker {worker_id} has no durable command Agent')
            if int(worker.get('running_jobs') or 0) >= int(worker.get('max_jobs') or 1):
                raise ValueError(f'Worker {worker_id} has no free test slot')
            minimum_disk = float(os.getenv('GMS_CLUSTER_MIN_DISK_FREE_GB', '50'))
            disk_free = float(worker.get('disk_free_gb') or 0)
            if disk_free and disk_free < minimum_disk:
                raise ValueError(
                    f'Worker {worker_id} has {disk_free:.1f} GB free; '
                    f'{minimum_disk:.1f} GB is required'
                )
            available_memory = float(worker.get('memory_available_gb') or 0)
            if required_memory and available_memory and available_memory < required_memory:
                raise ValueError(
                    f'Worker {worker_id} has {available_memory:.1f} GB memory; '
                    f'{required_memory:.1f} GB is required for full CTS'
                )
            selected = next(
                (suite for suite in all_suites if suite.get('worker_id') == worker_id),
                None,
            )
            if not selected:
                requested = suite_path or test_type
                raise ValueError(f'Test suite {requested} is not available on Worker {worker_id}')

        if required_memory and auto_selected:
            auto_worker = next(
                (item for item in cluster.list_workers() if item.get('id') == worker_id),
                {},
            )
            available_memory = float(auto_worker.get('memory_available_gb') or 0)
            if available_memory and available_memory < required_memory:
                raise ValueError(
                    f'Worker {worker_id} has {available_memory:.1f} GB memory; '
                    f'{required_memory:.1f} GB is required for full CTS'
                )

        suite_path = str(selected.get('tools_path') or '')
        plan['worker_id'] = worker_id
        plan['test_suite'] = suite_path
        body['test_plan'] = plan

        requested_devices = []
        for item in body.get('devices') or []:
            value = str(
                item.get('serial') or item.get('id') or ''
                if isinstance(item, dict)
                else item
            ).strip()
            if not value:
                continue
            if ':' in value and not value.startswith(f'{worker_id}:'):
                raise ValueError(f'Device {value} belongs to another Worker')
            device_id = value if value.startswith(f'{worker_id}:') else f'{worker_id}:{value}'
            if device_id not in requested_devices:
                requested_devices.append(device_id)
        inventory = {
            item['id']: item for item in cluster.repository.list_devices(worker_id)
        }
        if requested_devices:
            unavailable = [
                device_id for device_id in requested_devices
                if device_id not in inventory or inventory[device_id].get('state') != 'available'
            ]
            if unavailable:
                raise ValueError(f'Devices are unavailable on {worker_id}: {", ".join(unavailable)}')
            unsupported = [
                device_id for device_id in requested_devices
                if (
                    requires_local_usb
                    and str(inventory[device_id].get('transport') or '').lower()
                    == 'adb_proxy'
                )
            ]
            if unsupported:
                raise ValueError(
                    'ADB Proxy devices cannot be used for ATS firmware flashing '
                    f'because they have no USB/Fastboot channel: {", ".join(unsupported)}; '
                    'set flash.mode=skip for test-only runs'
                )
            if len(requested_devices) < minimum:
                raise ValueError(f'At least {minimum} devices are required')
        else:
            prefix = str(selector.get('serial_prefix') or '')
            board = str(selector.get('board') or '').lower()
            available = [
                item for item in inventory.values()
                if item.get('state') == 'available'
                and (
                    not requires_local_usb
                    or str(item.get('transport') or '').lower() != 'adb_proxy'
                )
                and (
                    not prefix
                    or str(item.get('serial') or '').startswith(prefix)
                )
                and (
                    not board
                    or board in str(
                        (item.get('properties') or {}).get('board')
                        or (item.get('properties') or {}).get('product')
                        or ''
                    ).lower()
                )
            ]
            if len(available) < minimum:
                raise ValueError(
                    f'Worker {worker_id} has {len(available)} idle devices; {minimum} required'
                )
        if requires_local_usb and requested_devices and len(requested_devices) != 1:
            raise ValueError('Firmware flashing requires exactly one device')
        body['devices'] = requested_devices
        return {
            'runtime_checked': True,
            'suite_key': selected.get('suite_key', ''),
            'available_device_count': sum(
                item.get('state') == 'available'
                and (
                    not requires_local_usb
                    or str(item.get('transport') or '').lower() != 'adb_proxy'
                )
                for item in inventory.values()
            ),
        }

    def list_runs(
        self, *, status: str = '', limit: int = 50, created_by: str = ''
    ):
        return self.store.list_runs(
            status=status, limit=limit, created_by=created_by
        )

    def list_run_summaries(
        self, *, status: str = '', limit: int = 50, created_by: str = ''
    ):
        return self.store.list_run_summaries(
            status=status, limit=limit, created_by=created_by
        )

    def get_run(self, run_id: str):
        run = self.store.get_run(run_id)
        if run is None:
            raise AutomationNotFoundError('Automation run not found')
        return run

    def list_events(self, run_id: str):
        self.get_run(run_id)
        return self.store.list_events(run_id)

    def cancel_run(self, run_id: str, executor_name: str = 'http'):
        try:
            return self.orchestrator(executor_name).cancel_run(run_id)
        except ValueError as exc:
            raise AutomationNotFoundError('Automation run not found') from exc

    def retry_run(self, run_id: str):
        old = self.get_run(run_id)
        if old.get('status') not in TERMINAL_STATUSES:
            raise ValueError('Only terminal automation runs can be retried')
        create_request = AutomationRunCreateRequest(
            profile_id=old['profile_id'],
            source_type=old['source_type'],
            project=old['project'],
            branch=old['branch'],
            gerrit_change_id=old['gerrit_change_id'],
            gerrit_patchset=old['gerrit_patchset'],
            gerrit_subject=old['gerrit_subject'],
            owner=old['owner'],
            jenkins_job=old.get('jenkins_job', ''),
            artifact_url=old['artifact_url'],
            artifact_path=old['artifact_path'],
            devices=[],
            test_plan={},
        )
        run_data = create_request.to_run_dict(self.new_run_id())
        run_data['devices_json'] = old['devices_json']
        run_data['test_plan_json'] = old['test_plan_json']
        run_data['created_by'] = old.get('created_by', '')
        retry_password = self.get_build_password(run_id)
        encrypted_secrets = {}
        if retry_password:
            encrypted_secrets["build_server_password"] = encrypt_secret(retry_password)
        run = self.store.create_run(run_data, encrypted_secrets=encrypted_secrets)
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

    @staticmethod
    def _require_owner_id(owner_id: str) -> str:
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("Automation owner id is required")
        return owner

    def handle_gerrit_webhook(
        self,
        payload: dict[str, Any],
        *,
        created_by: str,
    ):
        owner_id = self._require_owner_id(created_by)
        event = normalize_gerrit_event(payload or {})
        result = self._create_runs_for_event(
            event,
            self.list_profiles(enabled_only=True),
            created_by=owner_id,
        )
        return {'event': event, **result}

    async def poll_gerrit_changes(
        self,
        limit: int = 100,
        *,
        created_by: str,
    ):
        owner_id = self._require_owner_id(created_by)
        if self.gerrit_query is None:
            raise RuntimeError('Gerrit query provider is not configured')
        created = []
        existing = []
        rejected = []
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
            for change in await self.gerrit_query(owner_id, query, limit):
                event = self._gerrit_change_to_event(change)
                events.append(event)
                result = self._create_runs_for_event(
                    event,
                    [profile],
                    created_by=owner_id,
                )
                created.extend(result['created'])
                existing.extend(result['existing'])
                rejected.extend(result['rejected'])
        return {
            'events': events,
            'created': created,
            'existing': existing,
            'rejected': rejected,
            'created_count': len(created),
            'existing_count': len(existing),
            'rejected_count': len(rejected),
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
        build = profile.get('build') if isinstance(
            profile.get('build'),
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
                'build': {
                    **build,
                    'parameters': self._format_template_map(
                        build.get('parameters') or {},
                        event,
                    ),
                },
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
        *,
        created_by: str,
    ) -> dict[str, Any]:
        owner_id = self._require_owner_id(created_by)
        matches = match_profiles(event, profiles)
        created = []
        existing = []
        rejected = []
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
            jenkins = (
                profile.get('jenkins')
                if isinstance(profile.get('jenkins'), dict)
                else {}
            )
            request_data = create_request.model_dump()
            request_data['jenkins_job'] = str(jenkins.get('job') or '')
            try:
                run = self.create_run(
                    request_data,
                    created_by=owner_id,
                )
            except ValueError as exc:
                rejected.append({
                    'profile_id': profile.get('id', ''),
                    'error': str(exc),
                })
                continue
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
            'rejected': rejected,
        }
