"""Exercise TLS shutdown with real sockets and a peer that ignores close_notify."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
import uvicorn

from bootstrap import http_protocol
from bootstrap.http_protocol import ShutdownHttpProtocol


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("tls-shutdown")
    key, cert = root / "key.pem", root / "cert.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
    ], check=True, capture_output=True, timeout=10)
    return key, cert


def _connect(port):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # Isolated loopback fixture certificate.
    connection = context.wrap_socket(
        socket.create_connection(("127.0.0.1", port), timeout=5), server_hostname="localhost",
    )
    connection.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    return connection


def _read_response(connection):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        assert chunk, "connection closed before response headers"
        data += chunk
    header, body = data.split(b"\r\n\r\n", 1)
    length = int(next(line.split(b":", 1)[1] for line in header.split(b"\r\n")
                      if line.lower().startswith(b"content-length:")))
    while len(body) < length:
        chunk = connection.recv(4096)
        assert chunk, "response was truncated during shutdown"
        body += chunk
    return body


async def _start_server(app, tls_files):
    key, cert = tls_files
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off", log_level="warning",
        http=ShutdownHttpProtocol, ssl_keyfile=str(key), ssl_certfile=str(cert),
    ))
    task = asyncio.create_task(server.serve())
    async def wait_started():
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait_started(), 5)
    return server, task, server.servers[0].sockets[0].getsockname()[1]


def test_idle_tls_peer_does_not_hold_shutdown_for_30_seconds(tls_files):
    uvloop = pytest.importorskip("uvloop")

    async def exercise():
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-length", b"2")]})
            await send({"type": "http.response.body", "body": b"ok"})

        server, task, port = await _start_server(app, tls_files)
        connection = await asyncio.to_thread(_connect, port)
        try:
            assert await asyncio.to_thread(_read_response, connection) == b"ok"
            started = time.monotonic()
            server.should_exit = True
            # Keep the TLS peer open and do not read/respond to close_notify.
            await asyncio.wait_for(asyncio.shield(task), 5)
            assert time.monotonic() - started < 4
        finally:
            connection.close()
            server.should_exit = True
            await asyncio.wait_for(task, 5)

    uvloop.run(exercise())


@pytest.mark.parametrize("loop_name", ["asyncio", "uvloop"])
def test_active_response_survives_shutdown_close_deadline(tls_files, monkeypatch, loop_name):
    monkeypatch.setattr(http_protocol, "TLS_CLOSE_WAIT_SECONDS", 0.05)

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        observed_scope = {}

        async def app(scope, receive, send):
            observed_scope["scope"] = scope
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-length", b"2000000")]})
            await send({"type": "http.response.body", "body": b"x" * 2_000_000})

        server, task, port = await _start_server(app, tls_files)
        connection = await asyncio.to_thread(_connect, port)
        try:
            await asyncio.wait_for(entered.wait(), 3)
            server.should_exit = True
            await asyncio.sleep(0.3)  # Longer than the TLS close wait.
            assert not task.done()
            assert observed_scope["scope"]["gms.shutdown_requested"] is True
            release.set()
            # 慢端对端：读取晚于 TLS 关闭等待开始时，响应字节仍须完整送达。
            await asyncio.sleep(0.2)
            assert await asyncio.to_thread(_read_response, connection) == b"x" * 2_000_000
            await asyncio.wait_for(asyncio.shield(task), 5)
        finally:
            release.set()
            connection.close()
            server.should_exit = True
            await asyncio.wait_for(task, 5)

    if loop_name == "uvloop":
        pytest.importorskip("uvloop").run(exercise())
    else:
        asyncio.run(exercise())


def test_pending_write_buffer_is_not_aborted():
    # 已完成的响应可能仍排在慢读者之后，不得中止。
    protocol = object.__new__(ShutdownHttpProtocol)
    protocol._tls_close_handle = None
    protocol.connections = {protocol}
    protocol.cycle = Mock(response_complete=True)
    protocol.transport = Mock()
    protocol.transport.get_write_buffer_size.return_value = 1024
    protocol.loop = Mock()
    protocol._close_completed_tls()
    protocol.transport.abort.assert_not_called()
    protocol.loop.call_later.assert_called_once()


@pytest.mark.parametrize("workers", [1, 2])
def test_server_cli_uses_shutdown_protocol_in_spawned_workers(tmp_path, tls_files, workers):
    pytest.importorskip("uvloop")
    module = tmp_path / "shutdown_fixture.py"
    module.write_text(
        "async def app(scope, receive, send):\n"
        "    await send({'type': 'http.response.start', 'status': 200, "
        "'headers': [(b'content-length', b'2')]})\n"
        "    await send({'type': 'http.response.body', 'body': b'ok'})\n"
    )
    key, cert = tls_files
    connection = None
    with socket.socket() as listener, (tmp_path / "server.log").open("w+") as output:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        process = subprocess.Popen([
            sys.executable, "-m", "bootstrap.server", "shutdown_fixture:app",
            "--app-dir", str(tmp_path), "--fd", str(listener.fileno()),
            "--workers", str(workers), "--lifespan", "off",
            "--ssl-keyfile", str(key), "--ssl-certfile", str(cert),
        ], cwd=Path(__file__).resolve().parents[2], pass_fds=(listener.fileno(),),
            start_new_session=True, stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 10
            while connection is None:
                assert process.poll() is None, "CLI server exited before startup"
                try:
                    connection = _connect(port)
                except OSError:
                    assert time.monotonic() < deadline, "CLI server did not start"
                    time.sleep(0.02)
            assert _read_response(connection) == b"ok"
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            # Uvicorn 在优雅清理完成后重抛已处理信号。
            assert process.wait(timeout=6) in (0, -signal.SIGTERM)
            assert time.monotonic() - started < 6
            output.seek(0)
            assert "Closing completed HTTPS connection after TLS shutdown wait" in output.read()
        finally:
            if connection is not None:
                connection.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
