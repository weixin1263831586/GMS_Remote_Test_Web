"""畸形 JSON body 必须以 422 拒绝，而不是落进 handle_api_errors 兜底 500。

裸 ``await request.json()`` 在非法 JSON 时抛 JSONDecodeError，历史上
直接变成 500（错误模型违规：500 只留给编程错误）。六个写端点统一走
``_json_object_body``；int 强转（sort_order/limit）同理回 422。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import app


def _client() -> TestClient:
    return TestClient(app)


class MalformedJsonBodyTests(unittest.TestCase):
    def setUp(self):
        self.client = _client()
        self.client.get("/api/auth/status")  # establish anon session if any

    def _post_raw(self, path: str, body: bytes, content_type: str):
        with patch("features.knowledge.api._user", return_value="u1"):
            return self.client.post(
                path, content=body, headers={"Content-Type": content_type}
            )

    def test_create_space_malformed_json_returns_422(self):
        resp = self._post_raw("/api/knowledge/spaces", b"{not json", "application/json")
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertFalse(resp.json().get("success", True))

    def test_create_doc_malformed_json_returns_422(self):
        resp = self._post_raw("/api/knowledge/docs", b"[1,2,3", "application/json")
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_create_folder_array_body_returns_422(self):
        # 合法 JSON 但不是对象：同样必须 422，不能 AttributeError → 500。
        resp = self._post_raw("/api/knowledge/folders", b"[1,2]", "application/json")
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_move_node_non_integer_sort_order_returns_422(self):
        with patch("features.knowledge.api._user", return_value="u1"):
            resp = self.client.post(
                "/api/knowledge/nodes/whatever/move", json={"sort_order": "abc"}
            )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_ask_non_integer_limit_returns_422(self):
        with patch("features.knowledge.api._user", return_value="u1"):
            resp = self.client.post(
                "/api/knowledge/ask", json={"question": "q", "limit": "x"}
            )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_ask_malformed_json_returns_422(self):
        resp = self._post_raw("/api/knowledge/ask", b"{{", "application/json")
        self.assertEqual(resp.status_code, 422, resp.text)


if __name__ == "__main__":
    unittest.main()
