"""AndroidInternalsProvider 单元测试（临时 git 仓库 fixture）。

覆盖：clone 校验 fail-closed、frontmatter 解析、FTS5 索引构建与增量重建
幂等、FTS 语法注入防护、provenance 投影（含缺字段降级）。
"""

from __future__ import annotations

import builtins
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.knowledge.external.android_internals import (
    AndroidInternalsProvider,
    parse_frontmatter,
)
from features.knowledge.external.base import (
    EVIDENCE_LEVEL_BACKGROUND,
    EXTERNAL_KNOWLEDGE_LICENSE,
    ExternalKnowledgeError,
    fts_safe_query,
    search_terms,
)
from features.knowledge.external.content_policy import load_policy
from features.knowledge.external.ranking import (
    _bm25_score,
    parse_aosp_url,
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

#: 与上游 knowledge-pack/policy.yaml 同构的最小 policy fixture。
POLICY_YAML = """\
schema_version: 1
distribution:
  android_internals:
    default: include-body-markdown
    included_paths:
      - src/part2/**
    excluded_paths:
      - src/graphify-out/**
    excluded_tags:
      - internal-only
exported_metadata:
  - title
  - chapter
  - applicable_versions
  - last_verified
  - last_verified_against
  - confidence
  - tags
  - sources
license:
  expression: CC-BY-NC-SA-4.0 OR LicenseRef-AIW-Commercial
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
        # 非正文路径：policy included_paths 之外，ingestion 必须排除。
        (self.repo / "src/notes").mkdir(parents=True, exist_ok=True)
        (self.repo / "src/notes/scratch.md").write_text("draft note", encoding="utf-8")
        pack = self.repo / "knowledge-pack"
        pack.mkdir(parents=True, exist_ok=True)
        (pack / "policy.yaml").write_text(POLICY_YAML, encoding="utf-8")
        _git(self.repo, "init", "-q")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "init")


class FrontmatterTests(unittest.TestCase):
    def test_full_parse_keeps_lists_and_structures(self):
        # ADR 0014：sources/tags 等数组与嵌套结构必须保留——sources 是
        # Wiki→codesearch 锚点的来源，旧实现只投影顶层标量直接丢弃。
        metadata, body = parse_frontmatter(PAGE_A)
        self.assertEqual(metadata["title"], "LMKD 低内存守护")
        self.assertEqual(metadata["chapter"], "10.2")
        self.assertEqual(metadata["confidence"], "high")
        self.assertEqual(
            metadata["sources"],
            [{"type": "official", "path": "https://example.com/a"}],
        )
        self.assertIn("PRESSURE_AFTER_KILL", body)

    def test_unquoted_iso_date_becomes_string(self):
        # 回归：PyYAML 把未加引号的 `last_verified: 2026-08-15` 解析为
        # datetime.date，_apply_pages 的 json.dumps 直接 TypeError，整库
        # reindex 失败（真实上游语料即存在此写法）。解析边界必须统一
        # 转 ISO 字符串。
        text = (
            "---\n"
            "title: 日期字段\n"
            "last_verified: 2026-08-15\n"
            "last_verified_against: AOSP android-17.0.0_r1 retrieved 2026-08-15\n"
            "sources:\n"
            "- type: official\n"
            "  retrieved: 2026-08-15\n"
            "---\n\n"
            "正文。\n"
        )
        metadata, body = parse_frontmatter(text)
        self.assertEqual(metadata["last_verified"], "2026-08-15")
        self.assertIsInstance(metadata["last_verified_against"], str)
        self.assertEqual(metadata["sources"][0]["retrieved"], "2026-08-15")
        import json

        json.dumps(metadata, ensure_ascii=False)  # 不得抛 TypeError
        self.assertIn("正文", body)

    def test_no_frontmatter(self):
        metadata, body = parse_frontmatter("# Just a doc\nbody")
        self.assertEqual(metadata, {})
        self.assertTrue(body.startswith("# Just a doc"))

    def test_broken_yaml_keeps_body_and_scalar_fallback(self):
        # 坏 frontmatter 不丢正文：回退标量投影仍可索引。
        broken = "---\ntitle: [unclosed\n---\n\nbody text\n"
        _metadata, body = parse_frontmatter(broken)
        self.assertIn("body text", body)


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
        # 配额必须被严格遵守（历史实现单长类名的 camel 子词可越界）。
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

    def test_rerank_window_extends_beyond_output_limit(self):
        """候选池与输出上限分离回归（评审 P2）。

        limit=10 时旧实现理论候选 30 实际只有 MAX_HITS_CAP=10——第
        11~30 名里版本更匹配的页面 reranker 根本看不到。本用例构造
        12 页：10 个旧版页（BM25 略强）+ 2 个 Android 16 覆盖页，
        limit=10 且 android_api_level=36 时高版本页必须进入输出前列
        （证明候选窗口 > 输出上限）。
        """
        (self.root / "wiki/src/part2").mkdir(parents=True, exist_ok=True)
        for i in range(10):
            (self.root / f"wiki/src/part2/legacy-{i}.md").write_text(
                "---\n"
                f"title: 旧版机制 {i}\n"
                "chapter: '1.1'\n"
                "status: finalized\n"
                "applicable_versions: Android 4 (API 14) - Android 9 (API 28)\n"
                "confidence: high\n"
                "---\n\n"
                f"sharedmechanismkeyword 旧版第 {i} 页。\n",
                encoding="utf-8",
            )
        for i in range(2):
            (self.root / f"wiki/src/part2/modern-{i}.md").write_text(
                "---\n"
                f"title: 新版机制 {i}\n"
                "chapter: '1.2'\n"
                "status: finalized\n"
                "applicable_versions: Android 10 (API 29) - Android 17 (API 37)\n"
                "confidence: high\n"
                "---\n\n"
                f"sharedmechanismkeyword 新版第 {i} 页。\n",
                encoding="utf-8",
            )
        _git(self.root / "wiki", "add", "-A")
        provider = self._provider()
        with self._indexed(provider):
            provider.reindex()
            hits = provider.search(
                "sharedmechanismkeyword", limit=10, android_api_level=36
            )
            self.assertEqual(len(hits), 10)
            top_paths = [hit.source_path for hit in hits[:2]]
            self.assertTrue(
                all(p.endswith(("modern-0.md", "modern-1.md")) for p in top_paths),
                f"版本匹配页未进入 Top-2（候选池未扩窗）: {top_paths}",
            )

    def test_fts_injection_is_harmless(self):
        provider = self._provider()
        with self._indexed(provider):
            provider.reindex()
            for malicious in ('" OR 1=1 --', 'NEAR(', "*", "a* OR b"):
                hits = provider.search(malicious)
                for hit in hits:
                    self.assertEqual(hit.evidence_level, EVIDENCE_LEVEL_BACKGROUND)

    def test_bm25_score_degenerate_non_negative_best(self):
        # best >= 0（无区分信号）时全部给 1.0，最佳命中不得被算成 0 分。
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
        # 命中声称的 revision 与索引内容对应；git pull 未 reindex
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

    def test_policy_missing_fail_closed(self):
        # ADR 0014：policy 缺失时 reindex 必须显式失败，而不是放宽成
        # "src 下全部 Markdown 都是知识"。
        (self.root / "wiki/knowledge-pack/policy.yaml").unlink()
        provider = self._provider()
        with self._indexed(provider), self.assertRaises(ExternalKnowledgeError):
            provider.reindex()

    def test_policy_parser_dependency_missing_fails_closed(self):
        real_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("simulated missing PyYAML")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=reject_yaml):
            policy = load_policy(self.root / "wiki")

        self.assertTrue(policy.degraded)
        self.assertEqual(policy.included_paths, ())

    def test_policy_excludes_non_canonical_paths(self):
        # scratch.md 位于 included_paths 之外；doc_count 不含它。
        provider = self._provider()
        with self._indexed(provider):
            result = provider.reindex()
            self.assertEqual(result["doc_count"], 2)
            self.assertIn("policy_revision", result)
            self.assertEqual(result["license_expression"], "CC-BY-NC-SA-4.0 OR LicenseRef-AIW-Commercial")

    def test_policy_excluded_tags_drop_page(self):
        page = PAGE_B.replace("status: draft", "status: draft\ntags:\n  - internal-only")
        (self.root / "wiki/src/part2/choreographer.md").write_text(page, encoding="utf-8")
        provider = self._provider()
        with self._indexed(provider):
            result = provider.reindex()
            self.assertEqual(result["doc_count"], 1)
            self.assertEqual(provider.search("doFrame"), [])

    def test_hit_carries_source_anchor_and_policy_license(self):
        provider = self._provider()
        with self._indexed(provider):
            provider.reindex()
            hits = provider.search("LMKD PRESSURE_AFTER_KILL")
            self.assertTrue(hits)
            hit = hits[0]
            # PAGE_A 的 sources 是不可识别为 googlesource 的 URL：仍保留为
            # url-only anchor，供 Agent 直接 fetch。
            self.assertEqual(len(hit.source_anchors), 1)
            self.assertEqual(hit.source_anchors[0].url, "https://example.com/a")
            self.assertEqual(
                hit.license, "CC-BY-NC-SA-4.0 OR LicenseRef-AIW-Commercial"
            )
            self.assertEqual(hit.extra.get("status"), "finalized")

    def test_revision_tri_state_in_status(self):
        # 初次 reindex 建立批准基线；clone HEAD 前进后 available 领先。
        provider = self._provider()
        with self._indexed(provider):
            provider.reindex()
            status = provider.status()
            self.assertTrue(status.approved_revision)
            self.assertEqual(status.source_revision, status.approved_revision)
            fake_new = "b" * 40
            provider._head_revision = lambda: fake_new
            status = provider.status()
            self.assertEqual(status.available_revision, fake_new)
            self.assertNotEqual(status.approved_revision, fake_new)
            self.assertIn("available", status.detail)

    def test_new_revision_requires_approval_before_reindex(self):
        provider = self._provider()
        with self._indexed(provider):
            provider.reindex()
            page = self.root / "wiki/src/part2/lmkd.md"
            page.write_text(PAGE_A + "\n新增验证内容。\n", encoding="utf-8")
            _git(self.root / "wiki", "add", "-A")
            _git(self.root / "wiki", "commit", "-qm", "update wiki")

            with self.assertRaisesRegex(ExternalKnowledgeError, "尚未批准"):
                provider.reindex()

            approved = provider.approve_revision()["approved_revision"]
            pending = provider.status()
            self.assertEqual(pending.approved_revision, approved)
            self.assertNotEqual(pending.source_revision, approved)
            self.assertIn("等待 reindex", pending.detail)

            result = provider.reindex()
            self.assertEqual(result["source_revision"], approved)
            self.assertEqual(result["approved_revision"], approved)
            ready = provider.status()
            self.assertEqual(ready.source_revision, ready.approved_revision)
            self.assertEqual(ready.detail, "")

    def test_aosp_url_anchor_parsing(self):
        anchor = parse_aosp_url(
            "https://android.googlesource.com/platform/frameworks/base/+"
            "/refs/tags/android-17.0.0_r1/services/core/java/com/android/"
            "server/am/OomAdjuster.java"
        )
        self.assertEqual(anchor.repo, "platform/frameworks/base")
        self.assertEqual(anchor.revision, "android-17.0.0_r1")
        self.assertEqual(anchor.evidence_type, "aosp")
        self.assertTrue(anchor.path.endswith("OomAdjuster.java"))

    def test_rerank_prefers_version_matching_hit(self):
        # Android 10–17 覆盖页 vs Android 4 专属页：同 BM25 分下前者应
        # 因版本兼容加分排到前面（version-aware rerank）。
        old_page = """---
title: 旧版 lowmemorykiller 机制
chapter: '1.1'
status: finalized
applicable_versions: Android 4 (API 14) - Android 9 (API 28)
last_verified: '2026-08-15'
confidence: high
---

lmkd 之前的用户态 lowmemorykiller 驱动与 PRESSURE_AFTER_KILL 无关。
"""
        (self.root / "wiki/src/part2/legacy-lmk.md").write_text(old_page, encoding="utf-8")
        _git(self.root / "wiki", "add", "-A")
        provider = self._provider()
        with self._indexed(provider):
            provider.reindex()
            hits = provider.search("PRESSURE_AFTER_KILL", android_api_level=34)
            self.assertGreaterEqual(len(hits), 2)
            self.assertEqual(hits[0].source_path, "src/part2/lmkd.md")
            self.assertGreater(hits[0].score, hits[1].score)


_WIKI_CLONE = Path(__file__).resolve().parents[3] / "tools" / "android-internals-wiki"

#: knowledge-quality CI / 本地 golden replay 通过该环境变量指向
#: prepare_knowledge_quality_corpus.sh 全量重建的临时索引；未设置时
#: （本地开发环境）沿用已部署的伴生库。
_GOLDEN_INDEX_DB = os.environ.get("GMS_WIKI_INDEX_DB", "")


@unittest.skipUnless(_WIKI_CLONE.is_dir(), "android-internals-wiki clone 未部署")
class GoldenQueryCorpusTests(unittest.TestCase):
    """语料级 golden query 回归：外部知识源召回质量门禁。

    官方 ``knowledge-pack/golden-queries.yaml`` 的查询在真实 clone 上重放；
    索引用已部署的伴生库（未建索引的环境跳过），只读不重建，避免测试写
    共享状态。轻量解析 yaml（id/query/expected_path），不引入 yaml 依赖。

    ``GMS_WIKI_INDEX_DB`` 存在时改用该索引（CI knowledge-quality job：
    pinned revision clone + 全量重建的临时 DB），否则保持 skip 语义
    （索引未建的环境跳过，而不是拿空索引误报召回失败）。
    """

    @classmethod
    def setUpClass(cls):
        import features.knowledge.external.android_internals as mod

        cls._mod = mod
        if _GOLDEN_INDEX_DB:
            db = Path(_GOLDEN_INDEX_DB)
            if not db.is_file():
                raise unittest.SkipTest(
                    f"GMS_WIKI_INDEX_DB 指向的索引不存在: {db}"
                )
            cls._db_patch = patch.object(mod, "index_db_path", return_value=db)
            cls._db_patch.start()
            cls.addClassCleanup(cls._db_patch.stop)

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
