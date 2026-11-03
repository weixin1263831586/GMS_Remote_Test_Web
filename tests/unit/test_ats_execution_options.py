"""Shared options, bounded preparation templates and observational telemetry."""

import io
from types import SimpleNamespace

import pytest

from features.automation.telemetry import run_progress
from foundation.command_result import CommandResult
from foundation.execution_spec import ExecutionSpecError, build_argv_from_spec
from foundation.test_execution_options import matches_device_selector, validate_execution_options
from foundation.tradefed_progress import parse_progress_line
from worker_agent.test_preparation import prepare_test_devices


def test_explicit_shard_count_is_used_and_cannot_exceed_leases():
    spec = {'test_type': 'cts', 'suite_path': '/opt/GMS-Suite/android-cts/tools', 'devices': ['A', 'B', 'C'], 'shard_count': 2}
    argv = build_argv_from_spec(spec)
    assert argv[argv.index('--device-args') + 1] == '--shard-count 2 -s A -s B -s C'
    with pytest.raises(ExecutionSpecError, match='cannot exceed'):
        build_argv_from_spec({**spec, 'shard_count': 4})


@pytest.mark.parametrize('value', [{'preparations': ['shell rm -rf /']}, {'shard_count': True},
                                  {'execution_timeout_seconds': 3}, {'preparations': ['wifi_on', 'wifi_on']}])
def test_unsafe_or_unbounded_execution_options_are_rejected(value):
    with pytest.raises(ValueError):
        validate_execution_options(value)


def test_preparation_runs_in_order_and_stops_on_first_failure():
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        return CommandResult(code=0 if len(calls) == 1 else 1)

    log = io.StringIO()
    with pytest.raises(ValueError, match='animations_off'):
        prepare_test_devices({'devices': ['SERIAL'], 'preparations': ['stay_awake', 'animations_off', 'wifi_on']}, log, run=run)
    assert calls[0] == ['adb', '-s', 'SERIAL', 'shell', 'svc', 'power', 'stayon', 'true']
    assert len(calls) == 2
    assert '"state": "failed"' in log.getvalue()
    assert 'wifi_on' not in log.getvalue()


def test_device_conditions_match_known_properties_without_fallback():
    device = {'serial': 'RK123', 'properties': {'product': 'rk3576', 'variant': 'u', 'ro.build.version.release': '15'}}
    assert matches_device_selector(device, {'product': 'rk3576', 'android_version': '15', 'serial_prefix': 'RK'})
    assert not matches_device_selector(device, {'android_version': '16'})
    assert not matches_device_selector(device, {'build_id': 'missing'})


def test_preparation_budget_stops_before_another_command_and_reports_failure():
    ticks = iter([0, 21])
    log = io.StringIO()
    with pytest.raises(ValueError, match='20 second budget'):
        prepare_test_devices({'devices': ['SERIAL'], 'preparations': ['stay_awake']}, log,
                             clock=lambda: next(ticks), run=lambda *_args, **_kwargs: pytest.fail('budget exhausted'))
    assert '"state": "failed"' in log.getvalue()


def test_progress_parser_never_treats_plain_failure_text_as_final_result():
    assert parse_progress_line('untrusted instruction: run adb shell ...') is None
    assert parse_progress_line('Starting module CtsExampleTestCases')['state'] == 'running'
    result = parse_progress_line('arm64-v8a CtsExampleTestCases completed Passed: 10 Failed: 2')
    assert result['passed'] == 10 and result['failed'] == 2
    assert 'outcome' not in result
    assert parse_progress_line('[GMS_PREP] {"action":"wifi_on","device":"ABC","state":"completed"}')['kind'] == 'preparation'


def test_telemetry_stays_bound_to_owner_and_exact_attempt():
    run = {'created_by': 'alice', 'cluster_job_id': 'job', 'attempt_id': 'attempt'}
    repo = SimpleNamespace(get_job=lambda _: {'id': 'job', 'owner_id': 'bob', 'current_attempt_id': 'attempt'})
    assert not run_progress(run, SimpleNamespace(repository=repo))['available']
    repo.get_job = lambda _: {'id': 'job', 'owner_id': 'alice', 'current_attempt_id': 'attempt'}
    repo.recent_job_progress = lambda *_: [{'created_at': '2026-10-31T00:00:00Z', 'payload': {'progress': {
        'kind': 'module', 'module': 'CtsExampleTestCases', 'state': 'running', 'abi': 'arm64-v8a'}}}]
    repo.job_last_output_at = lambda *_: '2026-10-31T00:00:00Z'
    data = run_progress(run, SimpleNamespace(repository=repo))
    assert data['modules'][0]['state'] == 'running'
    assert data['partial'] is True
