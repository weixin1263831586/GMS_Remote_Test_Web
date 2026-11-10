"""Real HTTPS regression: trust the issuing CA, retain chain and name checks."""

import ipaddress
import json
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from features.automation import executors


@pytest.fixture
def tls_controller(tmp_path, monkeypatch):
    cert_dir = tmp_path / "certs"
    cert_dir.mkdir()
    ca_key = ec.generate_private_key(ec.SECP256R1())
    server_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ATS Test CA")])
    now = datetime.now(timezone.utc)

    def certificate(subject, issuer, key):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1)))

    ca = (certificate(ca_name, ca_name, ca_key)
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, None, None), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    leaf = (certificate(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ATS Test Server")]), ca_name, server_key)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_file = cert_dir / "gms-local-ca.crt"
    leaf_file = cert_dir / "gms-local.crt"
    key_file = tmp_path / "server.key"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    leaf_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    key_file.chmod(0o600)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            body = b'{"success":true,"stage_id":"stage-1","command_id":"cmd-1"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(leaf_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"https://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(executors, "_cluster_tls_profile", lambda: (url, ""))
    monkeypatch.setattr("foundation.config_paths.certificates_path", lambda root: cert_dir)
    for name in ("GMS_AUTOMATION_BASE_URL", "GMS_CONTROLLER_CA_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield SimpleNamespace(url=url, ca_file=ca_file, leaf_file=leaf_file, received=received)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_flash_stage_uses_local_ca_and_uploads_to_reserved_worker(tls_controller, tmp_path, monkeypatch):
    image = tmp_path / "update.img"
    image.write_bytes(b"firmware-over-verified-TLS")
    monkeypatch.setattr(executors, "_validated_controller_artifact", lambda value: image)
    monkeypatch.setattr("features.cluster.get_cluster_service", lambda: SimpleNamespace(
        repository=SimpleNamespace(renew_reservation=lambda reservation: reservation == "reservation-1"),
    ))
    executor = executors.HttpAutomationExecutor()
    executor.session.trust_env = False
    try:
        result = executor.flash({
            "id": "ats_tls", "artifact_path": str(image), "worker_id": "worker-1",
            "device_reservation_id": "reservation-1", "devices_json": '[{"serial":"worker-1:ABC"}]',
            "test_plan_json": json.dumps({"flash": {"mode": "firmware"}}),
        })
        assert executor.session.verify == str(tls_controller.ca_file)
        assert result["success"] and result["command_id"] == "cmd-1"
        assert len(tls_controller.received) == 1
        endpoint, body = tls_controller.received[0]
        assert endpoint == "/api/cluster/firmware/stage"
        for value in (image.read_bytes(), b"worker-1", b"worker-1:ABC", b"reservation-1", b"ats_tls"):
            assert value in body
    finally:
        executor.session.close()


def test_valid_ca_still_rejects_a_hostname_mismatch(tls_controller):
    executor = executors.HttpAutomationExecutor()
    executor.session.trust_env = False
    try:
        with pytest.raises(requests.exceptions.SSLError):
            executor.session.get(tls_controller.url.replace("127.0.0.1", "localhost"), timeout=2)
        assert tls_controller.received == []
    finally:
        executor.session.close()


def test_missing_local_ca_does_not_trust_the_server_leaf(tls_controller):
    tls_controller.ca_file.unlink()
    executor = executors.HttpAutomationExecutor()
    executor.session.trust_env = False
    try:
        assert tls_controller.leaf_file.is_file()
        assert executor.session.verify is True
        with pytest.raises(requests.exceptions.SSLError):
            executor.session.get(tls_controller.url, timeout=2)
    finally:
        executor.session.close()


def test_explicit_controller_ca_remains_authoritative(tls_controller, monkeypatch):
    # Misconfigured explicit trust must fail instead of silently switching CAs.
    monkeypatch.setattr(executors, "_cluster_tls_profile", lambda: (
        tls_controller.url, str(tls_controller.leaf_file),
    ))
    executor = executors.HttpAutomationExecutor()
    executor.session.trust_env = False
    try:
        assert executor.session.verify == str(tls_controller.leaf_file)
        with pytest.raises(requests.exceptions.SSLError):
            executor.session.get(tls_controller.url, timeout=2)
    finally:
        executor.session.close()


def test_explicit_ca_file_override_is_used_without_fallback(tmp_path, monkeypatch):
    override = tmp_path / "operator-ca.pem"
    monkeypatch.setenv("GMS_CONTROLLER_CA_FILE", str(override))
    monkeypatch.setattr(executors, "_cluster_tls_profile", lambda: ("https://controller.invalid", ""))
    monkeypatch.delenv("GMS_AUTOMATION_BASE_URL", raising=False)
    executor = executors.HttpAutomationExecutor()
    executor.session.trust_env = False
    try:
        assert executor.session.verify == str(override)
        with pytest.raises(OSError):
            executor.session.get(executor.base_url, timeout=2)
    finally:
        executor.session.close()
