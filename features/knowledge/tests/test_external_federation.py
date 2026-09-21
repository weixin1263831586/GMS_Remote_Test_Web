"""FederatedKnowledgeService 测试：失败隔离、source 过滤、fail-closed、
evidence_level 兜底盖章、便捷入口与单例重置。"""

from __future__ import annotations

import unittest

from features.knowledge.external.android_internals import AndroidInternalsProvider
from features.knowledge.external.base import (
    ExternalKnowledgeError,
    ExternalKnowledgeProvider,
    KnowledgeHit,
    ProviderStatus,
)
from features.knowledge.external.federation import (
    FederatedKnowledgeService,
    reset_singleton_for_tests,
)


def _hit(source: str, score: float, *, evidence_level: str = "background") -> KnowledgeHit:
    return KnowledgeHit(
        source=source, title=f"{source} title", snippet="snippet",
        score=score, evidence_level=evidence_level,
    )


class _FakeProvider:
    def __init__(self, source_id: str, *, hits: list[KnowledgeHit] | None = None,
                 error: Exception | None = None) -> None:
        self.source_id = source_id
        self._hits = hits or []
        self._error = error

    def status(self) -> ProviderStatus:
        if self._error:
            raise RuntimeError("boom")
        return ProviderStatus(source=self.source_id, enabled=True, status="ready", doc_count=len(self._hits))

    def search(self, query: str, *, limit: int = 5) -> list[KnowledgeHit]:
        if self._error:
            raise self._error
        return self._hits[:limit]


class _BrokenStatusProvider(_FakeProvider):
    def status(self) -> ProviderStatus:
        raise RuntimeError("status boom")


class FederationTests(unittest.TestCase):
    def tearDown(self):
        reset_singleton_for_tests()

    def test_merge_orders_by_score_and_caps_limit(self):
        # M3 契约变更：单源内分数降序，多源轮转合并（不再跨源比分数）。
        service = FederatedKnowledgeService([
            _FakeProvider("a", hits=[_hit("a", 0.5)]),
            _FakeProvider("b", hits=[_hit("b", 0.9)]),
        ])
        out = service.search("q", limit=5)
        self.assertEqual(sorted(r["source"] for r in out["results"]), ["a", "b"])
        self.assertTrue(all(r["evidence_level"] == "background" for r in out["results"]))
        self.assertTrue(all(r["license"] for r in out["results"]))

    def test_failure_isolation(self):
        service = FederatedKnowledgeService([
            _FakeProvider("good", hits=[_hit("good", 0.8)]),
            _FakeProvider("bad", error=ExternalKnowledgeError("kaboom")),
        ])
        out = service.search("q")
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["results"][0]["source"], "good")
        bad = next(s for s in out["sources_status"] if s["source"] == "bad")
        self.assertEqual(bad["status"], "error")

    def test_status_isolation(self):
        service = FederatedKnowledgeService([
            _BrokenStatusProvider("broken", hits=[_hit("broken", 0.5)]),
        ])
        rows = service.status()
        self.assertEqual(rows[0]["status"], "error")
        out = service.search("q")
        self.assertEqual(len(out["results"]), 1)
        # M2：search 路径 status 抛错也必须留状态行，不允许静默缺失。
        self.assertEqual(out["sources_status"][0]["status"], "error")

    def test_explicit_empty_sources_is_fail_closed(self):
        # sources=[""] 是"过滤后为空"，不是"检索全部"。
        service = FederatedKnowledgeService([_FakeProvider("a", hits=[_hit("a", 0.5)])])
        out = service.search("q", sources=[""])
        self.assertEqual(out["results"], [])
        self.assertEqual(out["sources_status"][0]["status"], "ready")

    def test_default_empty_list_means_all_sources(self):
        # 回归护栏：Web API 的 sources 字段默认是 []，语义必须是"不过滤"；
        # 误判成 fail-closed 会让检索端点静默失效（二轮评审实测抓到）。
        service = FederatedKnowledgeService([_FakeProvider("a", hits=[_hit("a", 0.5)])])
        out = service.search("q", sources=[])
        self.assertEqual(len(out["results"]), 1)
        out = service.search("q", sources=None)
        self.assertEqual(len(out["results"]), 1)

    def test_multi_source_merge_round_robin_per_source(self):
        # M3b：单源内部按分数降序，多源轮转合并，防高分源整体挤掉低分源。
        service = FederatedKnowledgeService([
            _FakeProvider("a", hits=[_hit("a", 0.9), _hit("a", 0.8), _hit("a", 0.7)]),
            _FakeProvider("b", hits=[_hit("b", 0.6)]),
        ])
        out = service.search("q", limit=2)
        self.assertEqual([r["source"] for r in out["results"]], ["a", "b"])

    def test_source_filter(self):
        service = FederatedKnowledgeService([
            _FakeProvider("a", hits=[_hit("a", 0.5)]),
            _FakeProvider("b", hits=[_hit("b", 0.9)]),
        ])
        out = service.search("q", sources=["a"])
        self.assertEqual([r["source"] for r in out["results"]], ["a"])

    def test_empty_query_returns_empty_with_status(self):
        service = FederatedKnowledgeService([_FakeProvider("a", hits=[_hit("a", 0.5)])])
        out = service.search("   ")
        self.assertEqual(out["results"], [])
        self.assertEqual(out["sources_status"][0]["status"], "ready")

    def test_evidence_level_stamped_even_if_provider_forgets(self):
        service = FederatedKnowledgeService([
            _FakeProvider("a", hits=[_hit("a", 0.5, evidence_level="evidence")]),
        ])
        out = service.search("q")
        self.assertEqual(out["results"][0]["evidence_level"], "background")

    def test_sources_method_and_registry_protocol(self):
        provider = _FakeProvider("a")
        self.assertIsInstance(provider, ExternalKnowledgeProvider)
        service = FederatedKnowledgeService([provider])
        self.assertEqual(service.sources(), ["a"])


class FromConfigTests(unittest.TestCase):
    def tearDown(self):
        reset_singleton_for_tests()

    def test_from_config_disabled_without_section(self):
        self.assertIsNone(AndroidInternalsProvider.from_config({}))
        self.assertIsNone(AndroidInternalsProvider.from_config({"external_knowledge": {"providers": {}}}))
        self.assertIsNone(AndroidInternalsProvider.from_config({
            "external_knowledge": {"providers": {"android_internals": {"enabled": False}}}
        }))

    def test_from_config_valid(self):
        provider = AndroidInternalsProvider.from_config({
            "external_knowledge": {"providers": {"android_internals": {
                "enabled": True, "repo_root": "/tmp/some-wiki", "max_hits": 3,
            }}}
        })
        self.assertIsNotNone(provider)
        assert provider is not None
        self.assertEqual(provider.max_hits, 3)

    def test_from_config_invalid_max_hits_falls_back(self):
        provider = AndroidInternalsProvider.from_config({
            "external_knowledge": {"providers": {"android_internals": {
                "enabled": True, "repo_root": "/tmp/some-wiki", "max_hits": "bogus",
            }}}
        })
        assert provider is not None
        self.assertEqual(provider.max_hits, 5)


if __name__ == "__main__":
    unittest.main()
