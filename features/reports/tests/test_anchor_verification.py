"""Wiki→codesearch 串联溯源管线测试（ADR 0014 阶段 ②-④）。

verify_background_anchors 把 background 命中的 source_anchors 逐一送到
本地 codesearch 检查路径存在性；这里用桩替换 run_codesearch_process，
不真正起子进程。语义契约：路径命中只产生 path_matched 级别，
绝不产生 verified=True 之类过强语义。
"""

import unittest
from unittest.mock import patch

from features.reports import anchor_verification as av
from foundation.command_result import CommandResult


def _hit(title: str, anchors: list[dict]) -> dict:
    return {"title": title, "source_anchors": anchors}


def _anchor(path: str = "services/core/java/com/android/server/am/OomAdjuster.java") -> dict:
    return {
        "repo": "platform/frameworks/base",
        "revision": "android-17.0.0_r1",
        "path": path,
        "evidence_type": "aosp",
    }


class _CodesearchStub:
    """按 keywords 返回命中的 codesearch 子进程桩。"""

    def __init__(self, known: set[str]):
        self.known = known
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], cwd: str, *, timeout: float = 30.0):
        self.calls.append(cmd)
        keywords = cmd[cmd.index("--keywords") + 1]
        if keywords in self.known:
            return CommandResult(
                stdout=f"[path] frameworks/base/{keywords}\n  project: Android17\n"
            )
        return CommandResult(stdout="")


class AnchorVerificationTests(unittest.TestCase):
    def test_path_matched_anchor_reports_local_path(self):
        path = "services/core/java/com/android/server/am/OomAdjuster.java"
        stub = _CodesearchStub({path})
        with patch.object(av, "run_codesearch_process", stub), \
                patch.dict(av.__dict__, {"MAX_VERIFIED_ANCHORS": 4}):
            out = av.verify_background_anchors(
                [_hit("LMKD", [_anchor()])], android_version="17"
            )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["path_present"])
        self.assertEqual(out[0]["evidence_level"], av.EVIDENCE_PATH_MATCHED)
        self.assertNotIn("verified", out[0])
        self.assertEqual(
            out[0]["verification"]["local_path"],
            f"frameworks/base/{path}",
        )
        self.assertEqual(out[0]["anchor"]["repo"], "platform/frameworks/base")
        command = stub.calls[0]
        self.assertEqual(command[command.index("--keywords") + 1], path)
        self.assertEqual(command[command.index("--project") + 1], "Android17")

    def test_path_missing_anchor_is_false_not_removed(self):
        stub = _CodesearchStub(set())
        with patch.object(av, "run_codesearch_process", stub):
            out = av.verify_background_anchors([_hit("LMKD", [_anchor()])])
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["path_present"])
        self.assertEqual(out[0]["evidence_level"], av.EVIDENCE_PATH_MISSING)
        self.assertIsNone(out[0]["verification"])

    def test_url_only_anchor_is_skipped_without_search(self):
        stub = _CodesearchStub(set())
        anchor = {"repo": "", "path": "", "url": "https://example.com/x"}
        with patch.object(av, "run_codesearch_process", stub):
            out = av.verify_background_anchors([_hit("t", [anchor])])
        self.assertEqual(stub.calls, [])
        # url-only anchor 无从溯源：整体跳过，不产生条目。
        self.assertEqual(out, [])

    def test_budget_caps_codesearch_calls_and_dedups(self):
        stub = _CodesearchStub(set())
        hits = [
            _hit(f"h{i}", [_anchor(path=f"a/b/File{i}.java")]) for i in range(8)
        ] + [_hit("dup", [_anchor(path="a/b/File0.java")])]
        with patch.object(av, "run_codesearch_process", stub):
            out = av.verify_background_anchors(hits)
        self.assertEqual(len(stub.calls), av.MAX_VERIFIED_ANCHORS)
        # 重复 anchor 不重跑子进程，但已缓存结论仍要回挂
        # 到后续 hit，不能因全局去重丢失关联。
        self.assertEqual(len(out), av.MAX_VERIFIED_ANCHORS + 1)
        self.assertEqual(out[-1]["anchor"]["path"], "a/b/File0.java")

    def test_crashing_verifier_degrades_to_unknown(self):
        def boom(cmd: list[str], cwd: str, *, timeout: float = 30.0):
            raise RuntimeError("codesearch down")

        with patch.object(av, "run_codesearch_process", boom):
            out = av.verify_background_anchors([_hit("t", [_anchor()])])
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["path_present"])
        self.assertEqual(out[0]["evidence_level"], av.EVIDENCE_UNKNOWN)

    def test_unavailable_codesearch_is_unknown(self):
        with patch.object(av, "run_codesearch_process", return_value=None):
            out = av.verify_background_anchors([_hit("t", [_anchor()])])
        self.assertIsNone(out[0]["path_present"])
        self.assertEqual(out[0]["evidence_level"], av.EVIDENCE_UNKNOWN)

    def test_same_basename_at_wrong_path_is_not_matched(self):
        result = CommandResult(
            stdout="[path] unrelated/module/OomAdjuster.java\n  project: Android17\n"
        )
        with patch.object(av, "run_codesearch_process", return_value=result):
            out = av.verify_background_anchors([_hit("t", [_anchor()])])
        self.assertFalse(out[0]["path_present"])
        self.assertEqual(out[0]["evidence_level"], av.EVIDENCE_PATH_MISSING)


if __name__ == "__main__":
    unittest.main()
