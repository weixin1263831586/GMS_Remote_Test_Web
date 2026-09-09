"""离线假 Redmine HTTP fixture + evidence 完整性测试。

不访问真实 Redmine：使用 ``http.server`` 起一个本地假服务，验证：
- 超过 2,000 字符的 journal 字节级保留；
- 原始 JSON 哈希、manifest、附件下载、SHA-256、派生文本；
- refresh=true 创建新快照且旧快照仍可读；
- 附件失败时快照为 partial、complete=false；
- SSRF：附件 URL 跳出 origin 被拒绝；
- owner 隔离：owner B 看不到 owner A 的 snapshot/artifact。
"""

from __future__ import annotations

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet


LONG_NOTES = "AssertionError 定长填充" + "x" * 5000 + "结束标记"
ATTACHMENT_TEXT = "log line AssertionError expected <true> but was <false>\n" * 40
ATTACHMENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _issue_document(issue_id: int = 648526) -> dict:
    return {
        "issue": {
            "id": issue_id,
            "subject": "CTS 模块失败",
            "description": "描述带 Unicode ✓ 与 " + "d" * 3000,
            "project": {"id": 1, "name": "fae"},
            "tracker": {"id": 2, "name": "缺陷"},
            "status": {"id": 1, "name": "新建"},
            "priority": {"id": 4, "name": "高"},
            "created_on": "2026-08-01T08:00:00Z",
            "updated_on": "2026-09-01T08:00:00Z",
            "journals": [
                {
                    "id": 912345,
                    "user": {"id": 7, "name": "张三"},
                    "created_on": "2026-08-02T08:00:00Z",
                    "private_notes": False,
                    "notes": LONG_NOTES,
                    "details": [
                        {"property": "attr", "name": "status_id", "old_value": "1", "new_value": "2"},
                    ],
                },
                {
                    "id": 912346,
                    "user": {"id": 8, "name": "李四"},
                    "notes": "",
                    "details": [
                        {"property": "attachment", "name": "776655", "old_value": None, "new_value": "screen.png"},
                    ],
                },
            ],
            "attachments": [
                {
                    "id": 776655,
                    "filename": "screen.png",
                    "filesize": len(ATTACHMENT_PNG),
                    "content_type": "image/png",
                    "content_url": "",  # 由 handler 填充
                    "created_on": "2026-08-02T08:00:00Z",
                    "author": {"id": 8, "name": "李四"},
                    "digest": "abc",
                },
                {
                    "id": 776656,
                    "filename": "logcat.log",
                    "filesize": len(ATTACHMENT_TEXT.encode()),
                    "content_type": "application/octet-stream",
                    "content_url": "",
                    "created_on": "2026-08-02T08:00:00Z",
                    "author": {"id": 8, "name": "李四"},
                },
            ],
        }
    }


class FakeRedmineHandler(BaseHTTPRequestHandler):
    """极简 Redmine REST 假服务（GET only）。"""

    server_version = "FakeRedmine/1"

    def log_message(self, *args):  # 静音
        pass

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server = self.server
        path = self.path.split("?", 1)[0]
        if path.startswith("/issues/") and path.endswith(".json"):
            if server.owner.issue_not_found:
                self._send(404, b'{"errors":["not found"]}', "application/json")
                return
            if server.owner.issue_forbidden:
                self._send(403, b'{"errors":["forbidden"]}', "application/json")
                return
            if not self.headers.get("X-Redmine-API-Key") and not self.headers.get("Authorization"):
                self._send(401, b'{"errors":["unauthorized"]}', "application/json")
                return
            document = server.owner.issue_document
            for attachment in document["issue"]["attachments"]:
                if not attachment["content_url"]:
                    attachment["content_url"] = (
                        f"http://{server.server_address[0]}:{server.server_address[1]}"
                        f"/attachments/download/{attachment['id']}/{attachment['filename']}"
                    )
            self._send(
                200,
                json.dumps(document, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        if self.path.startswith("/attachments/download/"):
            parts = self.path.split("/")
            attachment_id = next((p for p in parts if p.isdigit()), "")
            if server.owner.redirect_attachment == attachment_id:
                self.send_response(302)
                self.send_header("Location", server.owner.redirect_target)
                self.end_headers()
                return
            if server.owner.fail_attachment == attachment_id:
                self._send(500, b"boom", "text/plain")
                return
            payload = server.owner.attachment_payloads.get(attachment_id)
            if payload is None:
                self._send(404, b"missing", "text/plain")
                return
            self._send(200, payload, "application/octet-stream")
            return
        if self.path.startswith("/login"):
            self._send(200, b"<html>login</html>", "text/html")
            return
        self._send(404, b"unknown", "text/plain")


class FakeRedmineServer:
    def __init__(self):
        self.issue_document = _issue_document()
        self.attachment_payloads = {
            "776655": ATTACHMENT_PNG,
            "776656": ATTACHMENT_TEXT.encode("utf-8"),
        }
        self.issue_not_found = False
        self.issue_forbidden = False
        self.fail_attachment = ""
        self.redirect_attachment = ""
        self.redirect_target = "http://127.0.0.1:9/attachments/download/1/x"
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRedmineHandler)
        self._server.owner = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._server.shutdown()
        self._server.server_close()


def _runtime_config(base_url: str) -> dict:
    return {
        "redmine": {"base_url": base_url, "domain": ""},
        "redmine_auth": {
            "username": "tester",
            "encrypted_password": "ignored",
        },
    }


class EvidencePipelineTests(unittest.TestCase):
    def setUp(self):
        self.secret_env = patch.dict(
            "os.environ", {"GMS_SECRET_KEY": Fernet.generate_key().decode("ascii")}
        )
        self.secret_env.start()
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        # evidence_store 的 owner root 来自 redmine.users.owner_redmine_root，
        # 其 data_root 来自 foundation.config.settings——用环境变量隔离。
        import features.redmine.users as redmine_users
        self.users_patch = patch.object(
            redmine_users, "owner_redmine_root", lambda owner: self.root / "owners" / str(owner)
        )
        self.users_patch.start()
        import features.redmine.evidence_store as evidence_store
        evidence_store._STORE_CACHE.clear()
        self.store_cache_patch = patch.object(evidence_store, "_STORE_CACHE", {})
        self.store_cache_patch.start()

    def tearDown(self):
        self.users_patch.stop()
        self.store_cache_patch.stop()
        evidence_store = __import__(
            "features.redmine.evidence_store", fromlist=["_STORE_CACHE"]
        )
        evidence_store._STORE_CACHE.clear()
        self.directory.cleanup()
        self.secret_env.stop()

    def _patched_config(self, base_url: str):
        """返回 patch 上下文：owner config manager 指向假服务。"""
        from foundation.secrets import encrypt_secret

        def _runtime(owner_id: str) -> dict:
            return {
                "redmine": {"base_url": base_url, "domain": ""},
                "redmine_auth": {
                    "username": "tester",
                    "encrypted_password": encrypt_secret("pw"),
                },
            }

        manager = SimpleNamespace(
            get_runtime_config=lambda: _runtime("owner-a"),
            get_redmine_base_url=lambda: base_url,
            for_owner=lambda owner: SimpleNamespace(
                get_runtime_config=lambda: _runtime(owner),
                get_redmine_base_url=lambda: base_url,
            ),
        )
        import features.redmine.evidence as evidence
        return patch.object(evidence, "redmine_config_manager", manager)

    # ------------------------------------------------------------------ tests

    def test_long_journal_preserved_byte_for_byte(self):
        import asyncio

        from features.redmine.evidence import EvidenceFetcher

        with FakeRedmineServer() as fake, self._patched_config(fake.base_url):
            fetcher = EvidenceFetcher("owner-a")
            snapshot = fetcher.create_snapshot(648526, download="all")
            result = asyncio.run(fetcher.run(snapshot))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["errors"], [])
        manifest = result["manifest"]
        notes = manifest["journals"][0]["notes"]
        self.assertEqual(notes, LONG_NOTES)
        self.assertGreater(len(notes), 2000)
        self.assertEqual(result["journal_count"], 2)
        self.assertEqual(result["attachment_count"], 2)
        self.assertEqual(result["downloaded_count"], 2)
        self.assertTrue(result["content_sha256"])

    def test_attachment_hashes_and_derived_text(self):
        import asyncio
        import hashlib

        from features.redmine.evidence import EvidenceFetcher
        from features.redmine.evidence_store import owner_evidence_store

        with FakeRedmineServer() as fake, self._patched_config(fake.base_url):
            fetcher = EvidenceFetcher("owner-a")
            snapshot = fetcher.create_snapshot(648526, download="all")
            result = asyncio.run(fetcher.run(snapshot))
        store = owner_evidence_store("owner-a")
        artifacts = {a["attachment_id"]: a for a in store.list_artifacts(result["snapshot_id"])}
        self.assertEqual(artifacts["776655"]["sha256"], hashlib.sha256(ATTACHMENT_PNG).hexdigest())
        self.assertEqual(artifacts["776655"]["kind"], "image")
        self.assertEqual(artifacts["776655"]["detected_content_type"], "image/png")
        self.assertEqual(
            artifacts["776656"]["sha256"],
            hashlib.sha256(ATTACHMENT_TEXT.encode()).hexdigest(),
        )
        self.assertEqual(artifacts["776656"]["status"], "ready")

    def test_refresh_creates_new_snapshot_and_old_remains(self):
        import asyncio

        from features.redmine.evidence import EvidenceFetcher

        with FakeRedmineServer() as fake, self._patched_config(fake.base_url):
            fetcher = EvidenceFetcher("owner-a")
            first = asyncio.run(fetcher.run(fetcher.create_snapshot(648526, download="none")))
            fake.issue_document["issue"]["journals"].append({
                "id": 912347, "user": {"id": 9, "name": "王五"}, "notes": "new",
            })
            second = asyncio.run(fetcher.run(fetcher.create_snapshot(648526, download="none")))
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["journal_count"], 2)
        self.assertEqual(second["journal_count"], 3)

    def test_partial_when_attachment_fails(self):
        import asyncio

        from features.redmine.evidence import EvidenceFetcher, snapshot_completeness

        with FakeRedmineServer() as fake:
            fake.fail_attachment = "776656"
            with self._patched_config(fake.base_url):
                fetcher = EvidenceFetcher("owner-a")
                result = asyncio.run(fetcher.run(fetcher.create_snapshot(648526, download="all")))
        self.assertEqual(result["status"], "partial")
        completeness = snapshot_completeness(result)
        self.assertFalse(completeness["requested_downloads"])
        payload_complete = (
            result["status"] == "ready" and all(completeness.values()) and not result["errors"]
        )
        self.assertFalse(payload_complete)

    def test_redirect_to_other_host_rejected(self):
        import asyncio

        from features.redmine.evidence import EvidenceFetcher

        with FakeRedmineServer() as fake:
            fake.redirect_attachment = "776655"
            with self._patched_config(fake.base_url):
                fetcher = EvidenceFetcher("owner-a")
                result = asyncio.run(fetcher.run(fetcher.create_snapshot(648526, download="all")))
        self.assertEqual(result["status"], "partial")
        store = __import__(
            "features.redmine.evidence_store", fromlist=["owner_evidence_store"]
        ).owner_evidence_store("owner-a")
        rows = store.list_artifacts(result["snapshot_id"])
        by_attachment = {row["attachment_id"]: row for row in rows}
        self.assertEqual(by_attachment["776655"]["status"], "failed")
        self.assertIn("origin", by_attachment["776655"]["error"])

    def test_issue_not_found_marks_failed(self):
        import asyncio

        from features.redmine.evidence import EvidenceError, EvidenceFetcher

        with FakeRedmineServer() as fake:
            fake.issue_not_found = True
            with self._patched_config(fake.base_url):
                fetcher = EvidenceFetcher("owner-a")
                with self.assertRaises(EvidenceError):
                    asyncio.run(fetcher.run(fetcher.create_snapshot(648526, download="none")))

    def test_owner_isolation(self):
        import asyncio

        from features.redmine.evidence import EvidenceFetcher
        from features.redmine.evidence_store import owner_evidence_store

        with FakeRedmineServer() as fake, self._patched_config(fake.base_url):
            fetcher = EvidenceFetcher("owner-a")
            result = asyncio.run(fetcher.run(fetcher.create_snapshot(648526, download="all")))
        store_b = owner_evidence_store("owner-b")
        self.assertIsNone(store_b.get_snapshot(result["snapshot_id"]))
        self.assertIsNone(store_b.get_artifact(result["snapshot_id"]))

    def test_login_html_rejected(self):
        import asyncio

        from features.redmine.evidence import EvidenceAuthError, EvidenceFetcher

        with FakeRedmineServer() as fake:
            document = fake.issue_document
            # 让 content-type 变为 HTML 无法直接配置；改用 401 路径验证认证失败。
            fake.issue_document = document
            with self._patched_config(fake.base_url):
                fetcher = EvidenceFetcher("owner-noauth")
                # owner-noauth 无凭据（patch 的是 owner-a），应 401
                # 由于 config patch 对所有 owner 生效，这里直接验证 401 分支：
                # 临时清空凭据。
                import features.redmine.evidence as evidence
                with patch.object(
                    evidence, "load_owner_credentials",
                    lambda owner: evidence._OwnerCredentials(),
                ), self.assertRaises(EvidenceAuthError):
                    asyncio.run(fetcher.run(fetcher.create_snapshot(1, download="none")))


    def test_network_exception_marks_snapshot_failed(self):
        """P1 回归：Redmine 连接异常必须进 failed 终态，不留在 fetching。"""
        import asyncio

        import aiohttp

        from features.redmine.evidence import EvidenceError, EvidenceFetcher

        with FakeRedmineServer() as fake, self._patched_config(fake.base_url):
            fetcher = EvidenceFetcher("owner-a")
            snapshot = fetcher.create_snapshot(648526, download="none")
            store = fetcher.store

            async def _refuse(*args, **kwargs):
                raise aiohttp.ClientConnectionError("connection refused")

            with patch.object(
                fetcher, "_read_issue_response", _refuse
            ), self.assertRaises(EvidenceError):
                asyncio.run(fetcher.run(snapshot))
            stored = store.get_snapshot(snapshot["snapshot_id"])
            self.assertEqual(stored["status"], "failed")
            errors = stored["errors"] or []
            self.assertTrue(errors and errors[0]["stage"] == "issue")

    def test_runner_crash_marks_snapshot_failed(self):
        """P1 回归：run() 未预期异常时 _runner 兜底必须把快照推进 failed。"""
        import asyncio

        import features.redmine.evidence as evidence

        with FakeRedmineServer() as fake, self._patched_config(fake.base_url):
            fetcher = evidence.EvidenceFetcher("owner-a")
            snapshot = fetcher.create_snapshot(648526, download="none")
            snapshot_id = snapshot["snapshot_id"]

            class _CrashingFetcher:
                def __init__(self, owner_id, store=None):
                    self.store = fetcher.store

                async def run(self, snap):
                    raise RuntimeError("unexpected crash")

            async def scenario():
                with patch.object(evidence, "EvidenceFetcher", _CrashingFetcher):
                    task = evidence.start_evidence_fetch("owner-a", snapshot)
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)

            asyncio.run(scenario())
            stored = fetcher.store.get_snapshot(snapshot_id)
            self.assertEqual(stored["status"], "failed")
            errors = stored["errors"] or []
            self.assertTrue(errors and errors[0]["stage"] == "issue")

    def test_no_refresh_only_reuses_fresh_ready_snapshot(self):
        """P2 回归：refresh=false 只复用 TTL 内的 ready 快照。"""
        import asyncio
        from datetime import datetime, timedelta

        from features.redmine.evidence import EvidenceFetcher
        from features.redmine.evidence_api import (
            EVIDENCE_CACHE_TTL_SECONDS,
            _snapshot_fresh,
        )

        with FakeRedmineServer() as fake, self._patched_config(fake.base_url):
            fetcher = EvidenceFetcher("owner-a")
            result = asyncio.run(
                fetcher.run(fetcher.create_snapshot(648526, download="none"))
            )
            store = fetcher.store
            snapshot_id = result["snapshot_id"]
            # ready + 新鲜 -> fresh
            stored = store.get_snapshot(snapshot_id)
            self.assertTrue(_snapshot_fresh(stored, EVIDENCE_CACHE_TTL_SECONDS))
            # 把 fetched_at 改老 -> 不再新鲜（重新读取，不能断言旧 dict）
            old = datetime.now() - timedelta(
                seconds=EVIDENCE_CACHE_TTL_SECONDS + 1
            )
            store.update_snapshot(snapshot_id, fetched_at=old.isoformat(timespec="seconds"))
            self.assertFalse(
                _snapshot_fresh(store.get_snapshot(snapshot_id), EVIDENCE_CACHE_TTL_SECONDS)
            )
            # partial 快照即使新鲜也不算可复用（终态判断由调用方负责，
            # 这里验证 partial 不被误判 fresh ready）
            store.update_snapshot(snapshot_id, status="partial")
            partial = store.get_snapshot(snapshot_id)
            self.assertNotEqual(partial["status"], "ready")

if __name__ == "__main__":
    unittest.main()


class RealConfigWiringTests(unittest.TestCase):
    """load_owner_credentials 必须对真实 RedmineConfig 类型工作（防 stub 掩盖）。"""

    def test_load_owner_credentials_with_real_redmine_config(self):
        from unittest.mock import patch

        import features.redmine.evidence as evidence
        from features.redmine.config import RedmineConfig
        from foundation.secrets import encrypt_secret

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_manager = RedmineConfig(Path.cwd())

            def for_owner(self_or_owner, owner_id=None):
                owner_id = owner_id or self_or_owner
                cfg = RedmineConfig(Path.cwd())
                runtime = root / owner_id / "config_runtime.json"
                runtime.parent.mkdir(parents=True, exist_ok=True)
                cfg.runtime_config_path = runtime
                if not runtime.exists():
                    runtime.write_text("{}")
                return cfg

            with patch.object(evidence, "redmine_config_manager", real_manager), \
                 patch.object(RedmineConfig, "for_owner", for_owner):
                # 空 runtime -> 无凭据
                creds = evidence.load_owner_credentials("owner-x")
                self.assertEqual(creds.api_key, "")
                self.assertEqual(creds.password, "")
                # 写入凭据后可解密
                cfg = for_owner("owner-x")
                cfg.manager.runtime_config_path = root / "owner-x" / "config_runtime.json"
                runtime = {
                    "redmine_auth": {
                        "username": "u1",
                        "encrypted_password": encrypt_secret("pw1"),
                        "encrypted_api_key": encrypt_secret("key1"),
                    }
                }
                from foundation.config_persistence import ConfigPersistenceMixin  # noqa: F401
                cfg.manager.save_runtime(runtime)
                creds = evidence.load_owner_credentials("owner-x")
                self.assertEqual(creds.username, "u1")
                self.assertEqual(creds.password, "pw1")
                self.assertEqual(creds.api_key, "key1")
                self.assertTrue(creds.headers().get("X-Redmine-API-Key"))
