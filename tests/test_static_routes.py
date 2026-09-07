"""static_routes 单元测试：全部 mock subprocess，不改真实路由。"""

import ipaddress
from unittest import mock

import pytest

from foundation import static_routes


def _config(enabled=True, routes=None):
    return {
        'static_routes': {
            'enabled': enabled,
            'routes': routes if routes is not None else [
                {'destination': '10.10.10.0/24', 'gateway': '172.16.14.1'},
            ],
        },
    }


def test_parse_route_valid():
    parsed = static_routes._parse_route(
        {'destination': '10.10.10.29/32', 'gateway': '172.16.14.1'}
    )
    network, gateway = parsed
    assert network == ipaddress.ip_network('10.10.10.29/32')
    assert gateway == ipaddress.ip_address('172.16.14.1')


def test_parse_route_invalid_entries():
    assert static_routes._parse_route({'destination': '10.10.10.0/24', 'gateway': ''}) is None
    assert static_routes._parse_route({'destination': 'not-an-ip', 'gateway': '172.16.14.1'}) is None
    assert static_routes._parse_route({'destination': '10.10.10.0/24', 'gateway': '999.1.1.1'}) is None


def test_apply_disabled_returns_empty():
    assert static_routes.apply_static_routes(_config(enabled=False)) == []


def test_apply_missing_section_returns_empty():
    assert static_routes.apply_static_routes({}) == []


@mock.patch.object(static_routes, '_replace_route', return_value=(True, ''))
@mock.patch.object(static_routes, '_route_matches', return_value=False)
def test_apply_adds_missing_routes(mock_matches, mock_replace):
    config = _config(routes=[
        {'destination': '10.10.10.0/24', 'gateway': '172.16.14.1'},
        {'destination': '10.10.10.29/32', 'gateway': '172.16.14.1'},
    ])
    results = static_routes.apply_static_routes(config)
    assert [r['status'] for r in results] == ['ok', 'ok']
    assert mock_replace.call_count == 2
    mock_replace.assert_any_call('10.10.10.0/24', '172.16.14.1')
    mock_replace.assert_any_call('10.10.10.29/32', '172.16.14.1')


@mock.patch.object(static_routes, '_replace_route')
@mock.patch.object(static_routes, '_route_matches', return_value=True)
def test_apply_skips_existing_routes(mock_matches, mock_replace):
    results = static_routes.apply_static_routes(_config())
    assert results == [{
        'destination': '10.10.10.0/24',
        'gateway': '172.16.14.1',
        'status': 'exists',
    }]
    mock_replace.assert_not_called()


@mock.patch.object(static_routes, '_replace_route', return_value=(False, 'RTNETLINK answers: Operation not permitted'))
@mock.patch.object(static_routes, '_route_matches', return_value=False)
def test_apply_reports_failure_without_raising(mock_matches, mock_replace):
    results = static_routes.apply_static_routes(_config())
    assert results == [{
        'destination': '10.10.10.0/24',
        'gateway': '172.16.14.1',
        'status': 'failed',
        'error': 'RTNETLINK answers: Operation not permitted',
    }]


@mock.patch.object(static_routes, '_replace_route', return_value=(True, ''))
@mock.patch.object(static_routes, '_route_matches', return_value=False)
def test_apply_skips_invalid_entries_but_processes_rest(mock_matches, mock_replace):
    config = _config(routes=[
        {'destination': 'bad-ip', 'gateway': '172.16.14.1'},
        'not-a-dict',
        {'destination': '10.10.10.206/32', 'gateway': '172.16.14.1'},
    ])
    results = static_routes.apply_static_routes(config)
    assert len(results) == 1
    assert results[0]['destination'] == '10.10.10.206/32'


@mock.patch.object(static_routes.subprocess, 'run')
@mock.patch.object(static_routes.os, 'geteuid', return_value=1000)
@mock.patch.object(static_routes.shutil, 'which', return_value='/usr/bin/sudo')
def test_replace_route_uses_sudo_for_non_root(mock_which, mock_euid, mock_run):
    mock_run.return_value = mock.Mock(returncode=0, stdout='', stderr='')
    ok, error = static_routes._replace_route('10.10.10.0/24', '172.16.14.1')
    assert ok is True
    assert error == ''
    command = mock_run.call_args[0][0]
    assert command == [
        'sudo', '-n', 'ip', 'route', 'replace', '10.10.10.0/24', 'via', '172.16.14.1',
    ]


@mock.patch.object(static_routes.subprocess, 'run')
@mock.patch.object(static_routes.os, 'geteuid', return_value=0)
def test_replace_route_direct_as_root(mock_euid, mock_run):
    mock_run.return_value = mock.Mock(returncode=0, stdout='', stderr='')
    ok, _ = static_routes._replace_route('10.10.10.0/24', '172.16.14.1')
    assert ok is True
    command = mock_run.call_args[0][0]
    assert command == ['ip', 'route', 'replace', '10.10.10.0/24', 'via', '172.16.14.1']


@pytest.mark.parametrize('stdout,expected', [
    ('10.10.10.0/24 via 172.16.14.1 dev eno1', True),
    ('10.10.10.0/24 via 172.16.14.254 dev eno1', False),
    # 子串陷阱：'via 172.16.14.1' 是 'via 172.16.14.10' 的前缀，
    # 逐 token 匹配必须判定为"网关不同"。
    ('10.10.10.0/24 via 172.16.14.10 dev eno1', False),
    ('10.10.10.0/24 via 172.16.14.100 dev eno1', False),
    ('', False),
])
def test_route_matches(stdout, expected):
    with mock.patch.object(static_routes.subprocess, 'run') as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout=stdout, stderr='')
        assert static_routes._route_matches('10.10.10.0/24', '172.16.14.1') is expected
