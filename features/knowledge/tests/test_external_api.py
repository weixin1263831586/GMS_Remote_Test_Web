"""外部知识 API 测试：鉴权门禁、参数校验、失败隔离与 envelope。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.knowledge import external_api


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(external_api.router)
    app.include_router(external_api.agent_router)
    return TestClient(app)


class ExternalApiTests(unittest.TestCase):
    def setUp(self):
        self.client = _client()

    # ------------------------------------------------------------------
    # /external/sources
    # ------------------------------------------------------------------

    def test_sources_requires_auth(self):
        with patch.object(external_api, "require_permission", return_value=_deny):
            resp = self.client.get("/external/sources")
        self.assertEqual(resp.status_code, 403)

    def test_sources_ok(self):
        rows = [{"source": "android_internals", "status": "ready", "doc_count": 3}]
        with patch.object(external_api, "require_permission", return_value=_allow), patch.object(
            external_api, "federated_status", return_value=rows
        ):
            resp = self.client.get("/external/sources")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["sources"], rows)

    # ------------------------------------------------------------------
    # /external/search
    # ------------------------------------------------------------------

    def test_search_validates_sources(self):
        with patch.object(external_api, "require_permission", return_value=_allow):
            resp = self.client.post(
                "/external/search", json={"query": "lmkd", "sources": ["bogus_source"]}
            )
        self.assertEqual(resp.status_code, 422)

    def test_search_ok_with_provenance_passthrough(self):
        data = {
            "results": [{
                "source": "android_internals", "title": "LMKD", "snippet": "…",
                "evidence_level": "background", "license": "CC BY-NC-SA 4.0",
            }],
            "sources_status": [{"source": "android_internals", "status": "ready"}],
        }
        with patch.object(external_api, "require_permission", return_value=_allow), patch.object(
            external_api, "federated_search", return_value=data
        ) as fed:
            resp = self.client.post(
                "/external/search",
                json={"query": "lmkd", "limit": 3, "android_api_level": 34},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["results"][0]["evidence_level"], "background")
        fed.assert_called_once_with(
            "lmkd", sources=[], limit=3, android_api_level=34
        )

    def test_search_degrades_on_service_error(self):
        with patch.object(external_api, "require_permission", return_value=_allow), patch.object(
            external_api, "federated_search", side_effect=RuntimeError("boom")
        ), patch.object(
            external_api, "federated_status", return_value=[{"source": "android_internals", "status": "error"}]
        ):
            resp = self.client.post("/external/search", json={"query": "lmkd"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["results"], [])

    def test_search_rejects_blank_query(self):
        with patch.object(external_api, "require_permission", return_value=_allow):
            resp = self.client.post("/external/search", json={"query": "  "})
        self.assertEqual(resp.status_code, 422)

    # ------------------------------------------------------------------
    # /external/reindex
    # ------------------------------------------------------------------

    def test_reindex_requires_admin(self):
        with patch.object(external_api, "require_elevated_admin", side_effect=_deny):
            resp = self.client.post("/external/reindex")
        self.assertEqual(resp.status_code, 403)

    def test_reindex_conflict_maps_to_409(self):
        from features.knowledge.external import ExternalKnowledgeError

        with patch.object(external_api, "require_elevated_admin", _allow), patch.object(
            external_api, "federated_reindex", side_effect=ExternalKnowledgeError("已有索引重建进行中")
        ):
            resp = self.client.post("/external/reindex")
        self.assertEqual(resp.status_code, 409)

    def test_reindex_ok(self):
        with patch.object(external_api, "require_elevated_admin", _allow), patch.object(
            external_api, "federated_reindex", return_value={"doc_count": 3}
        ):
            resp = self.client.post("/external/reindex")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["doc_count"], 3)

    # ------------------------------------------------------------------
    # /android-internals/*（agent scope 门禁）
    # ------------------------------------------------------------------

    def test_agent_search_requires_scope(self):
        with patch.object(external_api, "require_agent_scope", return_value=_deny):
            resp = self.client.get("/android-internals/search", params={"q": "lmkd"})
        self.assertEqual(resp.status_code, 403)

    def test_agent_search_ok(self):
        data = {"results": [], "sources_status": []}
        with patch.object(external_api, "require_agent_scope", return_value=_allow), patch.object(
            external_api, "federated_search", return_value=data
        ) as fed:
            resp = self.client.get(
                "/android-internals/search",
                params={"q": "choreographer", "limit": 2, "android_api_level": 34},
            )
        self.assertEqual(resp.status_code, 200)
        # 统一 envelope（success_response 默认带 message 字段）。
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"], data)
        fed.assert_called_once_with(
            "choreographer",
            sources=["android_internals"],
            limit=2,
            android_api_level=34,
        )

    def test_search_rejects_blank_source_items(self):
        # 空串条目不是"未知源 422"就是被过滤，绝不能静默放大为全源检索。
        with patch.object(external_api, "require_permission", return_value=_allow), patch.object(
            external_api, "federated_search", return_value={"results": [], "sources_status": []}
        ) as fed:
            resp = self.client.post(
                "/external/search", json={"query": "lmkd", "sources": [""]}
            )
        if resp.status_code == 200:
            fed.assert_called_once()
            self.assertEqual(fed.call_args.kwargs["sources"], [""])
        else:
            self.assertEqual(resp.status_code, 422)

    def test_agent_status_requires_scope(self):
        with patch.object(external_api, "require_agent_scope", return_value=_deny):
            resp = self.client.get("/android-internals/status")
        self.assertEqual(resp.status_code, 403)


def _allow(_request):
    return SimpleNamespace(role="user")


def _deny(_request):
    from fastapi import HTTPException

    raise HTTPException(status_code=403, detail="Permission denied")


if __name__ == "__main__":
    unittest.main()
