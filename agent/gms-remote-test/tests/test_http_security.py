"""Credential redirects and package trust must fail before writing host state."""

import argparse
import base64
import hashlib
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest


_BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BASE / ('runtime' if (_BASE / 'runtime').is_dir() else 'scripts')))
from gms_agent import package_manager as pm  # noqa: E402
from gms_agent.client import GmsApiError, GmsClient  # noqa: E402
from gms_agent.http_transport import open_request  # noqa: E402


@pytest.fixture
def redirect_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith('/sink'):
                received.append(self.headers.get('Authorization'))
                self.send_response(200)
            else:
                code = 307 if self.path == '/api/auth/agent-enroll' else int(self.path.rsplit('/', 1)[-1])
                self.send_response(code)
                self.send_header('Location', f'http://localhost:{self.server.server_port}/sink')
            self.end_headers()

        def do_POST(self):
            self.do_GET()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('code', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_credentials_and_post_body_never_follow_redirect(redirect_server, code, method):
    origin, received = redirect_server
    request = urllib.request.Request(
        f'{origin}/{code}', method=method,
        headers={'Authorization': 'Bearer test-sentinel'},
        data=b'{"code":"test-one-shot"}' if method == 'POST' else None,
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        open_request(request)
    assert error.value.code == code
    assert received == []


def test_sdk_blocks_redirects(redirect_server, monkeypatch):
    origin, received = redirect_server
    monkeypatch.delenv('GMS_AUTH_TOKEN_FILE', raising=False)
    with pytest.raises(GmsApiError) as error:
        GmsClient(server_url=origin).request('GET', '/302')
    assert error.value.status == 302
    assert received == []


def test_package_download_blocks_redirects(redirect_server):
    origin, received = redirect_server
    with pytest.raises(urllib.error.HTTPError):
        pm.http_get(f'{origin}/302')
    assert received == []


def test_enrollment_does_not_replay_code_on_redirect(redirect_server, monkeypatch):
    origin, received = redirect_server
    monkeypatch.setattr(pm, 'resolve_controller', lambda *_args: (origin, ''))
    with patch.object(pm, 'write_enrollment_token') as write:
        assert pm.cmd_enroll(argparse.Namespace(profile='', server=origin, code='test-code')) == 7
    write.assert_not_called()
    assert received == []


def test_ed25519_signature_binds_artifact_version_and_size():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    key = base64.b64encode(pem).decode()
    manifest = {'name': 'gms-remote-test', 'version': '1.2.3',
                'artifacts': {'universal': {'sha256': 'abcd', 'size': 12}}}
    signature = base64.b64encode(private.sign(pm._manifest_signature_payload(manifest))).decode()
    assert pm._verify_manifest_signature(manifest, signature, key)
    manifest['version'] = '1.2.2'
    assert not pm._verify_manifest_signature(manifest, signature, key)
    assert not pm._verify_manifest_signature(manifest, 'malformed', key)


@pytest.mark.parametrize('url', ['http://controller/pkg.zip', 'https://evil/pkg.zip',
                                 'https://controller:444/pkg.zip', 'https://user@controller/pkg.zip'])
def test_artifact_origin_rejects_downgrade_and_foreign_targets(url):
    assert not pm.artifact_url_ok(url, 'https://controller')


@pytest.mark.parametrize('version', ['../../escape', '/tmp/escape', '', '1.2.3/../../escape'])
def test_registry_rejects_manifest_version_before_download_or_disk(version):
    manifest = {'version': version, 'artifacts': {'universal': {'url': 'https://controller/pkg.zip', 'sha256': 'abc'}}}
    with patch.object(pm, 'http_get', return_value=(json.dumps(manifest).encode(), {})) as fetch, pytest.raises(RuntimeError, match='version'):
        pm.fetch_registry_package('https://controller', '')
    assert fetch.call_count == 1


def test_registry_rejects_checksum_mismatch_before_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, 'VERSIONS_DIR', tmp_path / 'versions')
    manifest = {'version': '1.2.3', 'artifacts': {'universal': {
        'url': 'https://controller/pkg.zip', 'sha256': hashlib.sha256(b'valid').hexdigest(),
    }}}
    with patch.object(pm, 'http_get', side_effect=[(json.dumps(manifest).encode(), {}), (b'tampered', {})]), pytest.raises(RuntimeError, match='SHA-256'):
        pm.fetch_registry_package('https://controller', '')
    assert not pm.VERSIONS_DIR.exists()
