"""AndroidInternalsProvider 单元测试（临时 git 仓库 fixture）。

覆盖：clone 校验 fail-closed、frontmatter 解析、FTS5 索引构建与增量重建
幂等、FTS 语法注入防护、provenance 投影（含缺字段降级）。
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.knowledge.external.android_internals import (
    AndroidInternalsProvider,
    _bm25_score,
    parse_frontmatter,
)
from features.knowledge.external.base import (
    EVIDENCE_LEVEL_BACKGROUND,
    EXTERNAL_KNOWLEDGE_LICENSE,
    fts_safe_query,
    search_terms,
)


PAGE_A = """---
title: LMKD 低内存守护
chapter: '10.2'
status: finalized
applicable_versions: Android 10 (API 29) - Android 17 (API 37)
last_verified: '2026-08-15'
last_verified_against: AOSP android-17.0.0_r1
confidence: high
sources:
- type: official
  path: https://example.com/a
---

# LMKD

lmkd 在 memory pressure 时 kill 进程，PRESSURE_AFTER_KILL 是常见 reason。
"""

PAGE_B = """---
title: Choreographer 帧调度
chapter: '13.1'
status: draft
---

doFrame 由 VSync 驱动，FrameTimeline 记录帧 deadline。
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


class _WikiRepo:
    def __init__(self, root: Path) -> None:
        self.repo = root / "wiki"
        (self.repo / "src/part2").mkdir(parents=True, exist_ok=True)
        (self.repo / "src/part2/lmkd.md").write_text(PAGE_A, encoding="utf-8")
        (self.repo / "src/part2/choreographer.md").write_text(PAGE_B, encoding="utf-8")
        _git(self.repo, "init", "-q")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "init")


class FrontmatterTests(unittest.TestCase):
    def test_scalar_projection_and_list_skip(self):
        scalars, body = parse_frontmatter(PAGE_A)
        self.assertEqual(scalars["title"], "LMKD 低内存守护")
        self.assertEqual(scalars["chapter"], "10.2")
        self.assertEqual(scalars["confidence"], "high")
        self.assertNotIn("sources", scalars)
        self.assertIn("PRESSURE_AFTER_KILL", body)

    def test_no_frontmatter(self):
        scalars, body = parse_frontmatter("# Just a doc\nbody")
        self.assertEqual(scalars, {})
        self.assertTrue(body.startswith("# Just a doc"))


class FtsQueryTests(unittest.TestCase):
    def test_terms_quoted_and_clean(self):
        query = fts_safe_query('LMKD "pressure" AND kill; DROP')
        self.assertNotIn("AND", query.split('"')[0])
        for token in query.split(" OR "):
            self.assertTrue(token.startswith('"') and token.endswith('"'), token)

    def test_cjk_bigram(self):
        query = fts_safe_query("低内存")
        self.assertIn('"低内"', query)
        self.assertIn('"内存"', query)

    def test_empty(self):
        self.assertEqual(fts_safe_query("   "), "")

    def test_camel_identifiers_split_into_subwords(self):
        # CTS 类名整词在索引里零命中；拆出子词才能召回 statsd/atom 机制文。
        terms = search_terms("GraphicsAtomTests colorModeEvents")
        for expected in ("graphics", "atom", "color", "mode", "events"):
            self.assertIn(expected, terms)
        self.assertIn("graphicsatomtests", terms)  # 整词兜底保留

    def test_generic_scaffolding_terms_dropped(self):
        # 真实案例（ADR 0014 诊断第 5 路召回）：断言模板词 OR 匹配会把
        # colorModeEvents 失败拉成 FrameTimeline 机制文，必须过滤。
        terms = search_terms(
            "GraphicsAtomTests colorModeEvents CtsStatsdAtomHostTestCases "
            "expected to be at least: 2 but was 1"
        )
        for banned in ("expected", "least", "cts", "test", "tests", "host", "android"):
            self.assertNotIn(banned, terms)
        self.assertIn("statsd", terms)  # 模块名拆词后的领域词保留

    def test_generic_filter_keeps_real_domain_words(self):
        # LMKD/pressure_after_kill 是金标查询：确保过滤不伤真实查询。
        query = fts_safe_query("LMKD PRESSURE_AFTER_KILL")
        self.assertIn('"lmkd"', query)
        self.assertIn("pressure", query)

    def test_search_terms_strict_limit(self):
        # M4：配额必须被严格遵守（历史实现单长类名的 camel 子词可越界）。
        terms = search_terms(
            "CtsStatsdAtomHostTestCases GraphicsAtomTests colorModeEvents binder",
            limit=4,
        )
        self.assertLessEqual(len(terms), 4)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _WikiRepo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _provider(self, repo_root: Path | None = None) -> AndroidInternalsProvider:
        return AndroidInternalsProvider(str(repo_root or self.root / "wiki"))

    @staticmethod
    def _indexed(provider: AndroidInternalsProvider):
        """上下文内 index_db_path 指向临时库，覆盖 reindex/status/search 全链路。"""
        import contextlib

        import features.knowledge.external.android_internals as mod

        @contextlib.contextmanager
        def _ctx():
            with tempfile.TemporaryDirectory() as data_root:
                db = Path(data_root) / "android_internals.sqlite3"
                with patch.object(mod, "index_db_path", return_value=db):
                    yield provider

        return _ctx()

    def test_invalid_repo_fail_closed(self):
        provider = self._provider(self.root / "missing")
        status = provider.status()
        self.assertEqual(status.status, "disabled")
        self.assertEqual(provider.search("lmkd"), [])

    def test_reindex_and_search_with_provenance(self):
        provider = self._provider()
        with self._indexed(provider):
            result = provider.reindex()
            self.assertEqual(result["doc_count"], 2)
            status = provider.status()
            self.assertEqual(status.status, "ready")
            self.assertTrue(status.source_revision)

            hits = provider.search("LMKD PRESSURE_AFTER_KILL")
            self.assertTrue(hits)
            hit = hits[0]
            self.assertEqual(hit.source, "android_internals")
            self.assertEqual(hit.evidence_level, EVIDENCE_LEVEL_BACKGROUND)
            self.assertEqual(hit.license, EXTERNAL_KNOWLEDGE_LICENSE)
            self.assertEqual(hit.title, "LMKD 低内存守护")
            self.assertEqual(hit.chapter, "10.2")
            self.assertEqual(hit.confidence, "high")
            self.assertEqual(hit.last_verified, "2026-08-15")
            self.assertIn("AOSP android-17.0.0_r1", hit.last_verified_against)
            self.assertIn("src/part2/lmkd.md", hit.source_path)
            self.assertEqual(hit.source_revision, status.source_revision)
            self.assertIn("PRESSURE_AFTER_KILL", hit.snippet)

    def test_incremental_reindex_skips_unchanged_and_removes_stale(self):
        provider = self._provider()
        with self._indexed(provider):
            provider.reindex()
            result = provider.reindex()
            self.assertEqual(result["updated"], 0)
            self.assertEqual(result["removed"], 0)
            (self.root / "wiki/src/part2/choreographer.md").unlink()
            _git(self.root / "wiki", "add", "-A")
            result = provider.reindex()
            self.assertEqual(result["removed"], 1)
            self.assertEqual(provider.search("doFrame"), [])

    def test_fts_injection_is_harmless(self):
        provider = self._provider()
        with self._indexed(provider):
            provider.reindex()
            for malicious in ('" OR 1=1 --', 'NEAR(', "*", "a* OR b"):
                hits = provider.search(malicious)
                for hit in hits:
                    self.assertEqual(hit.evidence_level, EVIDENCE_LEVEL_BACKGROUND)

    def test_bm25_score_degenerate_non_negative_best(self):
        # M3：best >= 0（无区分信号）时全部给 1.0，最佳命中不得被算成 0 分。
        self.assertEqual(_bm25_score(0.0, 0.0), 1.0)
        self.assertEqual(_bm25_score(-3.2, -3.2), 1.0)
        self.assertAlmostEqual(_bm25_score(-1.6, -3.2), 0.5)

    def test_schema_gap_triggers_full_rebuild(self):
        # 重建型迁移/外部破坏造成 pages↔fts 行数缺口时，必须清 pages 走
        # 全量重建，增量 hash 逻辑不得误跳过已存在页。
        import features.knowledge.external.android_internals as mod

        provider = self._provider()
        with self._indexed(provider):
            provider.reindex()
            db_path = mod.index_db_path()
            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM wiki_fts WHERE path = ?", ("src/part2/choreographer.md",))
            conn.commit()
            conn.close()
            result = provider.reindex()
            self.assertEqual(result["doc_count"], 2)
            self.assertEqual(result["updated"], 2)  # 缺口 → 全量重建
            # 全量重建修复缺口：两页均可检索。
            self.assertTrue(provider.search("doFrame"))
            self.assertTrue(provider.search("LMKD"))

    def test_hit_revision_comes_from_index_not_live_head(self):
        # M2 语义：命中声称的 revision 与索引内容对应；git pull 未 reindex
        # 时不得把新 HEAD 冒充给旧内容。
        provider = self._provider()
        with self._indexed(provider):
            provider.reindex()
            stored = provider.status().source_revision
            self.assertTrue(stored)
            provider._head_revision = lambda: "a" * 40  # 模拟 pull 后未 reindex
            hits = provider.search("LMKD")
            self.assertTrue(hits)
            self.assertEqual(hits[0].source_revision, stored)
            self.assertNotEqual(hits[0].source_revision, "a" * 40)


_WIKI_CLONE = Path(__file__).resolve().parents[3] / "tools" / "android-internals-wiki"


@unittest.skipUnless(_WIKI_CLONE.is_dir(), "android-internals-wiki clone 未部署")
class GoldenQueryCorpusTests(unittest.TestCase):
    """语料级 golden query 回归：外部知识源召回质量门禁。

    官方 ``knowledge-pack/golden-queries.yaml`` 的查询在真实 clone 上重放；
    索引用已部署的伴生库（未建索引的环境跳过），只读不重建，避免测试写
    共享状态。轻量解析 yaml（id/query/expected_path），不引入 yaml 依赖。
    """

    @classmethod
    def setUpClass(cls):
        import features.knowledge.external.android_internals as mod

        cls._mod = mod

    def _rows(self) -> list[dict]:
        path = _WIKI_CLONE / "knowledge-pack" / "golden-queries.yaml"
        rows: list[dict] = []
        for chunk in path.read_text(encoding="utf-8").split("- id: ")[1:]:
            def grab(key: str, _chunk: str = chunk) -> str:
                m = re.search(rf"^\s+{key}: (.+)$", _chunk, re.M)
                return m.group(1).strip() if m else ""
            rows.append({
                "id": chunk.splitlines()[0].strip(),
                "query": grab("query"),
                "expected_path": grab("expected_path"),
                "expect_no_results": "expect_no_results: true" in chunk,
            })
        return rows

    def _search(self, query: str, limit: int = 5):
        provider = AndroidInternalsProvider(str(_WIKI_CLONE))
        with patch.object(self._mod, "index_db_path", return_value=self._mod.index_db_path()):
            return provider.search(query, limit=limit)

    def test_official_golden_queries_recall(self):
        # expected_path 属于 knowledge-pack 发布渠道语料，与 src/ 工作树
        # 存在版本偏移（如 ch26 合并重命名）；provider 只索引 src/**.md，
        # 因此只对语料内金标断言 recall@5，语料外金标显式跳过。
        for row in self._rows():
            if row["expect_no_results"]:
                with self.subTest(id=row["id"]):
                    self.assertEqual(self._search(row["query"]), [], "negative golden 必须零召回")
                continue
            if not row["expected_path"] or not (_WIKI_CLONE / row["expected_path"]).exists():
                continue  # pack 渠道语料，不在 src/**.md 索引范围
            with self.subTest(id=row["id"]):
                paths = {hit.source_path for hit in self._search(row["query"])}
                self.assertIn(row["expected_path"], paths)

    def test_pack_only_golden_ids_are_pinned(self):
        # 语义护栏：当前 3 个金标是 pack 渠道独有；clone 对齐 pack 语料后
        # 此断言失败，应把对应 id 移入 recall 断言并删除 pin。
        missing = sorted(
            row["id"] for row in self._rows()
            if row["expected_path"] and not (_WIKI_CLONE / row["expected_path"]).exists()
        )
        self.assertEqual(
            missing,
            ["ebpf-binder-semantics", "observability-architecture", "recyclerview-rendering"],
        )

    def test_scaffolding_query_does_not_drift_to_unrelated_mechanics(self):
        # 真实案例回归：GraphicsAtomTests#colorModeEvents（statsd 域）曾因
        # 断言模板词 OR 匹配整体漂移到 FrameTimeline 渲染文。拆词+停用词
        # 之后，top 命中必须落在 statsd/可观测性家族。
        query = (
            "GraphicsAtomTests colorModeEvents CtsStatsdAtomHostTestCases "
            "expected to be at least: 2 but was 1"
        )
        hits = self._search(query, limit=6)
        self.assertTrue(hits, "scaffolding 查询仍应命中 statsd/可观测性家族")
        joined = " ".join(hit.source_path.lower() for hit in hits)
        self.assertTrue(
            any(key in joined for key in ("statsd", "observability", "logd")),
            f"expected statsd-family recall, got: {[h.source_path for h in hits[:3]]}",
        )


if __name__ == "__main__":
    unittest.main()
