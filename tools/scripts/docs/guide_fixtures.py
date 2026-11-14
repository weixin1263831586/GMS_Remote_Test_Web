"""Synthetic, read-only browser fixtures for the operation guide screenshots."""

import json
from urllib.parse import parse_qs, urlsplit


LOCAL_WORKER = 'ats-worker-controller'
SUITE_ROOT = '/opt/gms/android-cts-16_r2'
SUITE = {
    'tools_path': f'{SUITE_ROOT}/tools', 'test_type': 'cts', 'version': '16_r2',
    'suite_key': 'cts-16-r2', 'worker_id': LOCAL_WORKER,
}
DEVICES = [
    {'device_id': 'DEMO-RK3576-01', 'id': 'DEMO-RK3576-01', 'serial': 'DEMO-RK3576-01',
     'status': 'device', 'state': 'available', 'locked': False, 'model': 'RK3576',
     'transport': 'local_usb', 'worker_id': LOCAL_WORKER},
    {'device_id': 'DEMO-RK3572-02', 'id': 'DEMO-RK3572-02', 'serial': 'DEMO-RK3572-02',
     'status': 'device', 'state': 'allocated', 'locked': True, 'locked_by': 'demo-user',
     'model': 'RK3572', 'transport': 'usbip', 'worker_id': LOCAL_WORKER},
]
REPORTS = [
    {'timestamp': 'demo-cts-report', 'report_name': '2026.11.14_09.30.00',
     'display_client_id': 'demo-user@192.0.2.30', 'test_type': 'CTS', 'suite_version': '16_r2',
     'worker_id': LOCAL_WORKER, 'pass': 148, 'fail': 2, 'total': 150,
     'suite_path': f'{SUITE_ROOT}/tools'},
    {'timestamp': 'demo-gts-report', 'report_name': '2026.11.14_08.00.00',
     'display_client_id': 'demo-user@192.0.2.30', 'test_type': 'GTS', 'suite_version': '13_r5',
     'worker_id': 'ats-worker-02', 'pass': 96, 'fail': 0, 'total': 96},
]
SOURCES = [
    {'source_id': 'demo-android16', 'provider': 'ssh_git', 'target': 'builder@192.0.2.20',
     'android_version': 'Android16', 'default_revision': 'HEAD', 'mode': 'repo_workspace',
     'ssh_username': 'builder', 'ssh_host': '192.0.2.20', 'ssh_port': 22, 'discover_repos': True,
     'verify_state': 'fresh', 'live': {'verified': True}, 'last_verified_at': '2026-11-14T10:00:00Z'},
    {'source_id': 'demo-android17', 'provider': 'ssh_git', 'target': 'builder@192.0.2.20',
     'android_version': 'Android17', 'default_revision': 'HEAD', 'mode': 'repo_workspace',
     'ssh_username': 'builder', 'ssh_host': '192.0.2.20', 'ssh_port': 22, 'discover_repos': True,
     'verify_state': 'fresh', 'live': {'verified': True}, 'last_verified_at': '2026-11-14T10:00:00Z'},
]
WORKERS = [
    {'id': LOCAL_WORKER, 'name': 'Controller', 'hostname': 'controller', 'address': '192.0.2.10',
     'status': 'busy', 'running_jobs': 1, 'cpu_percent': 31, 'memory_percent': 46,
     'memory_total_gb': 64, 'memory_available_gb': 34.56,
     'disk_total_gb': 1000, 'disk_free_gb': 420,
     'capabilities': {'command_agent': True, 'usbip_client': True, 'adb_proxy': True}},
    {'id': 'ats-worker-02', 'name': 'Lab Worker 02', 'hostname': 'lab-worker-02', 'address': '192.0.2.22',
     'status': 'online', 'running_jobs': 0, 'cpu_percent': 18, 'memory_percent': 35,
     'memory_total_gb': 64, 'memory_available_gb': 41.6,
     'disk_total_gb': 1000, 'disk_free_gb': 590,
     'capabilities': {'command_agent': True, 'usbip_client': True, 'adb_proxy': True}},
]
CLUSTER_PAYLOADS = {
    '/api/cluster/status': {'success': True, 'enabled': True, 'effective_enabled': True,
                            'remote_dispatch_enabled': True, 'local_worker_id': LOCAL_WORKER},
    '/api/cluster/workers': {'success': True, 'workers': WORKERS},
    '/api/cluster/devices': {'success': True, 'devices': [
        {'id': f'{device["worker_id"]}:{device["serial"]}', 'worker_id': device['worker_id'],
         'serial': device['serial'], 'state': device['state'], 'transport': device['transport'],
         'properties': {'model': device['model']}} for device in DEVICES
    ]},
    '/api/cluster/suites': {'success': True, 'suites': [
        {'worker_id': SUITE['worker_id'], 'suite_key': SUITE['suite_key'], 'suite_type': 'CTS',
         'suite_version': SUITE['version'], 'path': SUITE['tools_path'], 'available': True},
        {'worker_id': REPORTS[1]['worker_id'], 'suite_key': 'gts-13-r5', 'suite_type': 'GTS',
         'suite_version': REPORTS[1]['suite_version'], 'path': '/opt/gms/android-gts-13_r5/tools', 'available': True},
    ]},
    '/api/cluster/jobs': {'success': True, 'jobs': [
        {'id': 'job-demo-cts-running', 'assigned_worker_id': DEVICES[1]['worker_id'],
         'status': 'running', 'created_at': '2026-11-14T10:30:00+08:00',
         'leases': [{'serial': DEVICES[1]['serial']}]},
    ]},
    '/api/cluster/worker-tests': {'success': True, 'tests': [
        {'worker_id': DEVICES[1]['worker_id'], 'status': 'running', 'source': 'managed',
         'suite_type': 'CTS', 'devices': [DEVICES[1]['serial']], 'elapsed_seconds': 1280},
    ]},
    '/api/cluster/suite-library': {'success': True, 'archives': []},
}


def install_guide_fixtures(page) -> None:
    """Keep screenshots off real hosts, models, credentials and business data."""
    def fulfill(route, payload):
        route.fulfill(content_type='application/json', body=json.dumps(payload, ensure_ascii=False))

    def handle(route):
        url = urlsplit(route.request.url)
        path = url.path
        if path in {'/api/auth/status', '/api/auth/me', '/api/auth/agent-scopes',
                    '/api/users/current', '/api/users/detect'}:
            # These read/write only the temporary instance's authenticated identity.
            route.continue_()
            return
        payload = {'success': True, 'data': {}}
        if path in CLUSTER_PAYLOADS:
            payload = CLUSTER_PAYLOADS[path]
        elif path == '/api/vpn/status' or path.endswith('/vpn-status'):
            payload = {'success': True, 'connected': True}
        elif path == '/api/users/workspace-context':
            payload['data'] = {'context': {'scope_mode': 'cluster', 'worker_id': LOCAL_WORKER}}
        elif path == '/api/devices/list':
            payload = DEVICES
        elif path == '/api/test/suites':
            payload = {'success': True, 'suites': [SUITE]}
        elif path.endswith('/suites/files'):
            directory = parse_qs(url.query).get('path', [''])[0]
            items = [{'name': name, 'path': name, 'type': 'directory', 'size': 0}
                     for name in ('results', 'logs', 'testcases', 'tools')]
            payload['data'] = {'path': directory, 'suite_root': SUITE_ROOT, 'items': items}
        elif path == '/api/reports/list':
            payload = {'success': True, 'reports': REPORTS, 'next_cursor': ''}
        elif path in {'/api/sdk/sources', '/api/sdk/manage/sources'}:
            payload['data'] = {'sources': SOURCES}
        elif path == '/api/sdk/manage/discover':
            payload['data'] = {'candidates': [
                {'path': '/srv/android16', 'kind': 'repo', 'kind_label': 'Android repo'},
                {'path': '/srv/android17', 'kind': 'repo', 'kind_label': 'Android repo'},
            ]}
        elif path == '/api/auth/agent-tokens':
            payload['tokens'] = []
        elif path == '/api/users/list':
            payload['users'] = []
        elif path == '/api/config/ai':
            payload['data'] = {'enabled': True, 'provider': 'demo', 'status': {
                'local_provider': 'demo', 'primary_provider': 'demo', 'probe_target': 'demo',
                'probed': parse_qs(url.query).get('probe') == ['true'],
                'providers': [{'provider': 'demo', 'name': '演示模型', 'model': 'demo-analyzer',
                               'available': True, 'local': True, 'latency_ms': 42, 'state': 'ok'}],
            }}
        elif path.startswith('/api/assistant/'):
            payload['data'] = {'sessions': [], 'session_id': '', 'messages': [], 'steps': []}
        elif path == '/api/agent/sessions':
            payload['data'] = {'sessions': [
                {'session_id': 'guide-session', 'title': 'CTS 模块测试与失败分析（演示）',
                 'status': 'planning', 'updated_at': '2026-11-14T10:31:01'},
                {'session_id': 'guide-history', 'title': '设备与 SDK 版本查询（演示）',
                 'status': 'completed', 'updated_at': '2026-11-14T09:00:00'},
            ]}
        elif path.startswith('/api/automation/'):
            payload['data'] = {'items': []}
            if path.endswith('/worker/status'):
                payload['data'] = {'running': True, 'interval_seconds': 5,
                                   'last_tick_seconds_ago': 1, 'executor': '演示'}
            if path.endswith('/profiles'):
                payload['data']['items'] = [{'id': 'demo-profile', 'name': 'Android 16 CTS 演示'}]
        elif path.startswith('/api/build/'):
            payload['data'] = {'items': [], 'servers': [], 'templates': []}
        elif route.request.method not in {'GET', 'HEAD'}:
            route.fulfill(status=409, content_type='application/json', body=json.dumps({
                'success': False, 'error': 'Guide preview does not execute operations',
            }))
            return
        fulfill(route, payload)

    # Registered first so the more specific APK fixtures can override it.
    page.route('**/api/**', handle)
