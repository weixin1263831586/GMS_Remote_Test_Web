"""update_doc 值类型校验回归(全局深度审核发现)。

存储层把 updates 直接绑定进 sqlite:任意 JSON 类型(list/dict)此前会
触发 sqlite3.InterfaceError 进入 500 异常路径。API 层现在必须以 422
拒绝错误类型(错误模型:500 只留给意外编程错误)。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import app


def _client() -> TestClient:
    return TestClient(app)


class UpdateDocTypeValidationTests(unittest.TestCase):
    def setUp(self):
        self.client = _client()
        self.client.get("/api/auth/status")  # establish anon session if any

    def _create_doc(self) -> str:
        with patch("features.knowledge.api._user", return_value="u1"):
            resp = self.client.post(
                "/api/knowledge/docs",
                json={"title": "t", "content_md": "body"},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["data"]["doc_id"]

    def _update(self, doc_id: str, payload: dict):
        with patch("features.knowledge.api._user", return_value="u1"):
            return self.client.put(f"/api/knowledge/docs/{doc_id}", json=payload)

    def test_non_string_content_md_rejected_with_422(self):
        doc_id = self._create_doc()
        resp = self._update(doc_id, {"content_md": {"a": 1}})
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertFalse(resp.json().get("success", True))

    def test_non_list_links_rejected_with_422(self):
        doc_id = self._create_doc()
        resp = self._update(doc_id, {"links": 5})
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_non_string_tags_rejected_with_422(self):
        doc_id = self._create_doc()
        resp = self._update(doc_id, {"tags": {"a": 1}})
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_valid_string_update_still_succeeds(self):
        doc_id = self._create_doc()
        resp = self._update(doc_id, {"content_md": "updated body"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["success"])


if __name__ == "__main__":
    unittest.main()
