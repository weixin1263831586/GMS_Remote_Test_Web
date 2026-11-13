"""source_repo_rank 排序器测试。

验证强相关仓排序信号：
- subsystem 映射：lmkd/binder/surfaceflinger 等机制关键词命中对应子仓；
- 路径词法：query token 与仓路径 token 重合；
- knowledge 锚点（注入）：anchor 前缀按权重放大，提取器异常降级为空；
- 零分仓保持原顺序、稳定排序；配置注入/撤销。
"""

from __future__ import annotations

import unittest

from features.system.source_repo_rank import (
    configure_knowledge_hints,
    knowledge_hints_for_query,
    rank_repos,
)


REPOS = [
    "art",
    "bionic",
    "cts",
    "external/perfetto",
    "frameworks/base",
    "frameworks/native/libs/binder",
    "frameworks/native/services/surfaceflinger",
    "frameworks/av/media",
    "kernel-6.1",
    "packages/apps/settings",
    "system/memory/lmkd",
    "system/netd",
    "system/sepolicy",
    "system/vold",
    "vendor/rockchip/hardware",
]


class SubsystemRankTests(unittest.TestCase):
    def test_lmkd_query_ranks_lmkd_repo_first(self):
        ranked = rank_repos("lmkd kills activity and PSI", REPOS)
        self.assertEqual(ranked[0][0], "system/memory/lmkd")
        self.assertGreater(ranked[0][1], 0)
        self.assertIn("subsystem:", ranked[0][2])
        # activitymanager/am 命中 frameworks/base（services/am 前缀）。
        top = {repo for repo, score, _ in ranked if score > 0}
        self.assertIn("frameworks/base", top)

    def test_binder_query_ranks_binder_repo_first(self):
        ranked = rank_repos("binder transaction timeout", REPOS)
        self.assertEqual(ranked[0][0], "frameworks/native/libs/binder")

    def test_surfaceflinger_query_ranks_sf_repo_first(self):
        ranked = rank_repos("vsync in surfaceflinger", REPOS)
        self.assertEqual(ranked[0][0], "frameworks/native/services/surfaceflinger")

    def test_path_token_match_without_subsystem(self):
        # "netd" 在 subsystem 表里有映射（system/netd）；换一个纯路径词。
        ranked = rank_repos("perfetto trace_processor", REPOS)
        self.assertEqual(ranked[0][0], "external/perfetto")
        self.assertIn("path:perfetto", ranked[0][2])

    def test_unrelated_query_keeps_original_order_with_zero_scores(self):
        ranked = rank_repos("xyzzy", REPOS)
        self.assertEqual([repo for repo, _, _ in ranked], REPOS)
        self.assertTrue(all(score == 0 for _, score, _ in ranked))

    def test_stopwords_do_not_score(self):
        # 停用词（error/manager/...）不应给任何仓加分。
        ranked = rank_repos("manager error service", REPOS)
        self.assertTrue(all(score == 0 for _, score, _ in ranked))


class KnowledgeAnchorRankTests(unittest.TestCase):
    def tearDown(self):
        configure_knowledge_hints(None)

    def test_anchor_hint_boosts_prefix(self):
        configure_knowledge_hints(
            lambda query: [("system/memory/lmkd", 2.0)]
            if "lmkd" in query else []
        )
        ranked = rank_repos(
            "lmkd pressure", REPOS,
            knowledge_hints=knowledge_hints_for_query("lmkd pressure"),
        )
        self.assertEqual(ranked[0][0], "system/memory/lmkd")
        # anchor（3.0×2.0=6.0）是最大单项贡献，reason 反映主信号。
        self.assertIn("anchor:", ranked[0][2])

    def test_anchor_prefix_partial_match(self):
        # knowledge 给 frameworks/base，仓是 frameworks/base（相等）与
        # 无关仓：只有前缀命中者得分。
        configure_knowledge_hints(lambda query: [("frameworks/base", 1.5)])
        ranked = rank_repos(
            "wtf", REPOS, knowledge_hints=knowledge_hints_for_query("wtf"),
        )
        self.assertEqual(ranked[0][0], "frameworks/base")
        # 兄弟仓（frameworks/av、frameworks/native）只拿部分重合分，
        # 严格低于完整前缀命中。
        for repo, score, _ in ranked[1:]:
            if repo.startswith("frameworks/"):
                self.assertLess(score, ranked[0][1])
            else:
                self.assertEqual(score, 0.0)

    def test_provider_exception_degrades_to_empty(self):
        def broken(query):
            raise RuntimeError("knowledge unavailable")

        configure_knowledge_hints(broken)
        self.assertEqual(knowledge_hints_for_query("lmkd"), [])
        ranked = rank_repos("lmkd", REPOS, knowledge_hints=knowledge_hints_for_query("lmkd"))
        # 无 hint 也不崩溃：subsystem 信号仍生效。
        self.assertEqual(ranked[0][0], "system/memory/lmkd")

    def test_malformed_hints_ignored(self):
        configure_knowledge_hints(lambda query: [("ok", 1.0), "bad", (), ("x", "nan")])
        hints = knowledge_hints_for_query("q")
        self.assertEqual(hints, [("ok", 1.0)])


class CodesearchHintRankTests(unittest.TestCase):
    """codesearch 符号落点信号：最强先验（真实代码索引），优先于其他。"""

    def tearDown(self):
        from features.system.source_repo_rank import configure_codesearch_hints

        configure_codesearch_hints(None)

    def test_codesearch_hint_beats_subsystem(self):
        from features.system.source_repo_rank import (
            codesearch_hints_for_query,
            configure_codesearch_hints,
        )

        # "binder" 的 subsystem 映射给 frameworks/native/libs/binder 多前
        # 缀叠加（8.1）+ path token（1.5）= 9.6；codesearch 落点用更高
        # 权重 4.0（真实代码索引，3.0×4.0=12）压过叠加的静态映射——符号
        # 在索引中的位置就是最强的"哪个模块"证据。
        repos = [*REPOS, "system/libbinder"]
        configure_codesearch_hints(
            lambda query: [("system/libbinder", 4.0)]
            if "binder" in query else []
        )
        ranked = rank_repos(
            "binder transaction timeout", repos,
            codesearch_hints=codesearch_hints_for_query("binder transaction"),
        )
        self.assertEqual(ranked[0][0], "system/libbinder")
        self.assertIn("codesearch:", ranked[0][2])
        self.assertEqual(ranked[1][0], "frameworks/native/libs/binder")

    def test_codesearch_exception_degrades(self):
        from features.system.source_repo_rank import (
            codesearch_hints_for_query,
            configure_codesearch_hints,
        )

        configure_codesearch_hints(
            lambda query: (_ for _ in ()).throw(RuntimeError("opengrok down"))
        )
        self.assertEqual(codesearch_hints_for_query("binder"), [])
        ranked = rank_repos("binder", REPOS)
        self.assertEqual(ranked[0][0], "frameworks/native/libs/binder")


if __name__ == "__main__":
    unittest.main()
