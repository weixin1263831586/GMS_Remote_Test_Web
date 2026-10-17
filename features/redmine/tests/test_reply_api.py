"""Redmine 回复端点输入校验与错误映射回归。

审核项：/api/redmine/reply 曾把任意 issue_id 字符串拼进 Redmine 路径、
把远端失败一律映射 500、附件无大小上限、文件名写入日志。
"""

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.datastructures import Headers, UploadFile

from features.redmine import reply_api


class _FakeForm(dict):
    def getlist(self, key):
        return self.get(key, [])


class _FakeRequest:
    def __init__(self, content_type="application/json", payload=None, form=None):
        self.headers = {"content-type": content_type}
        self._payload = payload if payload is not None else {}
        self._form = form

    async def json(self):
        return self._payload

    async def form(self):
        return self._form


def _manager(with_credentials=True):
    creds = {"username": "user", "password": "pass"} if with_credentials else {}

    def _load_creds():
        return creds

    def _load_key():
        return ""

    def _get_config():
        return {"base_url": "https://redmine.example.test"}

    return SimpleNamespace(
        load_redmine_credentials=_load_creds,
        load_redmine_api_key=_load_key,
        get_redmine_config=_get_config,
    )


def _ok_client():
    def _ctor(*args, **kwargs):
        async def _reply_issue(issue_id, notes, files=None):
            return {"issue_url": f"https://redmine.example.test/issues/{issue_id}", "attachments": len(files or [])}

        return SimpleNamespace(reply_issue=_reply_issue, close=_noop_close)

    return _ctor


async def _noop_close():
    return None


class RedmineReplyValidationTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, request):
        with patch.object(reply_api, "get_redmine_config_for_request", return_value=_manager()):
            return await reply_api.redmine_reply(request, _user=None)

    async def test_non_numeric_issue_id_is_rejected(self):
        for bad in ("../../admin/settings", "12abc", "-5", "0", ""):
            response = await self._call(
                _FakeRequest(payload={"issue_id": bad, "reply_text": "hello"})
            )
            self.assertEqual(response.status_code, 400, bad)

    async def test_remote_failure_maps_to_502_upstream_failure(self):
        def _failing_client(*args, **kwargs):
            async def _reply_issue(issue_id, notes, files=None):
                raise RuntimeError("connection reset by peer")

            return SimpleNamespace(reply_issue=_reply_issue, close=_noop_close)

        with patch.object(reply_api, "get_redmine_config_for_request", return_value=_manager()), \
                patch.object(reply_api, "RedmineClient", _failing_client):
            response = await reply_api.redmine_reply(
                _FakeRequest(payload={"issue_id": "12345", "reply_text": "hello"}), _user=None
            )
        self.assertEqual(response.status_code, 502)
        import json as json_module
        payload = json_module.loads(response.body)
        self.assertEqual(payload["code"], "UPSTREAM_FAILURE")
        self.assertFalse(payload["success"])

    async def test_oversize_attachment_is_rejected_before_upload(self):
        upload = UploadFile(
            io.BytesIO(b"abcdef"),
            filename="huge.log",
            headers=Headers({"content-type": "text/plain"}),
        )
        form = _FakeForm({"issue_id": "12345", "reply_text": "hello", "files": [upload]})
        with patch.object(reply_api, "get_redmine_config_for_request", return_value=_manager()), \
                patch.object(reply_api, "_MAX_REPLY_ATTACHMENT_BYTES", 4):
            response = await reply_api.redmine_reply(_FakeRequest("multipart/form-data", form=form), _user=None)
        self.assertEqual(response.status_code, 400)

    async def test_filename_never_reaches_logs_and_success_envelope_kept(self):
        upload = UploadFile(
            io.BytesIO(b"abcdef"),
            filename="customer\r\ninjected-name.log",
            headers=Headers({"content-type": "text/plain"}),
        )
        form = _FakeForm({"issue_id": "42", "reply_text": "hello", "files": [upload]})
        with patch.object(reply_api, "get_redmine_config_for_request", return_value=_manager()), \
                patch.object(reply_api, "RedmineClient", _ok_client()):
            with self.assertLogs("features.redmine.reply_api", level="INFO") as logs:
                response = await reply_api.redmine_reply(
                    _FakeRequest("multipart/form-data", form=form), _user=None
                )
        output = "\n".join(logs.output)
        self.assertNotIn("customer\r\ninjected-name.log", output)
        self.assertNotIn("injected-name", output)
        self.assertEqual(response.status_code, 200)
        import json as json_module
        payload = json_module.loads(response.body)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["attachments"], 1)


if __name__ == "__main__":
    unittest.main()
