import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RuntimeDataRecoveryTests(unittest.TestCase):
    def test_page_read_apis_recover_after_data_root_deletion(self):
        with tempfile.TemporaryDirectory() as runtime_parent:
            data_root = Path(runtime_parent) / "data"
            browse_root = Path(runtime_parent) / "GMS-Suite"
            browse_root.mkdir()
            (browse_root / "system.img").write_bytes(b"image")
            port = _free_port()
            base_url = f"http://127.0.0.1:{port}"
            env = os.environ.copy()
            env.update({
                "GMS_PORT": str(port),
                "GMS_DATA_ROOT": str(data_root),
                "GMS_AUTH_REQUIRED": "false",
                "GMS_SECURE_COOKIES": "false",
                "ATS_WORKER_ENABLED": "0",
            })
            process = subprocess.Popen(
                ["python", "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            output = []

            def _drain_output():
                if process.stdout:
                    output.extend(process.stdout)

            output_thread = threading.Thread(target=_drain_output, daemon=True)
            output_thread.start()

            def _request(path: str, *, method: str = "GET", body: dict | None = None):
                payload = None if body is None else json.dumps(body).encode("utf-8")
                request = Request(
                    base_url + path,
                    data=payload,
                    method=method,
                    headers={"Content-Type": "application/json"},
                )
                try:
                    with urlopen(request, timeout=10) as response:
                        content = response.read().decode("utf-8")
                        return response.status, content
                except HTTPError as error:
                    return error.code, error.read().decode("utf-8")

            try:
                deadline = time.time() + 30
                while time.time() < deadline:
                    if process.poll() is not None:
                        self.fail("test server exited early:\n" + "".join(output))
                    try:
                        status, _ = _request("/api/system/health")
                        if status == 200:
                            break
                    except OSError:
                        time.sleep(0.2)
                else:
                    self.fail("test server did not become ready:\n" + "".join(output))

                requests = [
                    ("GET", "/api/auth/status", None),
                    ("GET", "/api/cluster/status", None),
                    ("GET", "/api/cluster/workers", None),
                    ("GET", "/api/test/status?logs=false", None),
                    ("GET", "/api/notifications?limit=100", None),
                    ("GET", "/api/users/list", None),
                    ("GET", "/api/reports/list?worker_id=worker-local", None),
                    ("GET", "/api/automation/dashboard", None),
                    ("GET", "/api/automation/runs?limit=100", None),
                    ("GET", "/api/build/jobs?limit=20", None),
                    ("GET", "/api/knowledge/spaces", None),
                    ("GET", "/api/knowledge/tree?space_id=gms", None),
                    ("GET", "/api/knowledge/docs?space_id=gms", None),
                    ("POST", "/api/files/list", {"path": str(browse_root)}),
                ]

                # Initialize every long-lived store once, then reproduce the
                # operator action that originally broke the running process.
                for method, path, body in requests:
                    status, content = _request(path, method=method, body=body)
                    self.assertLess(status, 500, f"before deletion: {path}: {status} {content}")

                shutil.rmtree(data_root)

                for method, path, body in requests:
                    status, content = _request(path, method=method, body=body)
                    self.assertLess(status, 500, f"after deletion: {path}: {status} {content}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                output_thread.join(timeout=2)
                if process.stdout:
                    process.stdout.close()


if __name__ == "__main__":
    unittest.main()
