"""Python file size ratchet (baseline persisted in baselines/file_size.json).

历史 ceiling 原来以字典形式写在测试内，与代码同文件——同时上调
字典即可绕过「只减不增」约定。现在
ceilings 独立持久化，本测试负责：

1. 运行时校验：文件行数不超过各自 ceiling（未登记走 2000 默认）；
2. ratchet 校验：相对上一个 commit，ceiling 只减不增；上调必须提供
   带 reason/expiry 的 waiver（见 ``_size_ratchet``）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _size_ratchet import BACKEND_SOURCE_SUFFIXES, assert_no_ceiling_raises, load_baseline, tracked_source_paths


ROOT = Path(__file__).resolve().parents[2]
BASELINE_NAME = "file_size.json"


def _limits() -> tuple[dict[str, int], int]:
    data = load_baseline(BASELINE_NAME)
    return data["ceilings"], int(data.get("default_limit") or 2000)


class FileSizeRuleTests(unittest.TestCase):
    def test_new_python_modules_stay_reviewable(self):
        ceilings, default_limit = _limits()
        offenders = []
        for path in tracked_source_paths(BACKEND_SOURCE_SUFFIXES):
            relative = path.relative_to(ROOT).as_posix()
            count = len(path.read_text(encoding='utf-8').splitlines())
            limit = ceilings.get(relative, default_limit)
            if count > limit:
                offenders.append((relative, count, limit))
        self.assertEqual(
            offenders,
            [],
            "first-party source exceeds its registered line ceiling "
            "(baselines/file_size.json; split cohesive responsibilities, do not raise without review)",
        )

    def test_baseline_ratchet_no_ceiling_raises(self):
        """相对上一推送或可信父提交，存量有效上限不可无理由上调。"""
        assert_no_ceiling_raises(BASELINE_NAME)

    def test_registered_waivers_are_not_expired(self):
        """存量 waiver 过期即失败：到期必须拆分回落或重新评审。"""
        from datetime import date

        data = load_baseline(BASELINE_NAME)
        today = date.today()
        expired = []
        for key, waiver in (data.get("waivers") or {}).items():
            expires_raw = str(waiver.get("expires") or "")
            try:
                expires = date.fromisoformat(expires_raw[:10])
            except ValueError:
                expired.append((key, f"unparseable expires {expires_raw!r}"))
                continue
            if expires < today:
                expired.append((key, f"expired {expires.isoformat()}"))
        self.assertEqual(
            expired,
            [],
            "expired size-budget waivers must be re-reviewed (split the file "
            f"or renew with justification): {expired}",
        )


if __name__ == '__main__':
    unittest.main()


def test_effective_size_ratchet_rejects_removed_limit_for_surviving_file():
    from datetime import date

    import pytest
    from _size_ratchet import _assert_effective_limits

    previous = {"default_limit": 2000, "ceilings": {"a.py": 1000}}
    current = {"default_limit": 2000, "ceilings": {}, "waivers": {}}
    with pytest.raises(AssertionError, match=r"a\.py"):
        _assert_effective_limits(previous, current, {"a.py"}, date.today())
    _assert_effective_limits(previous, current, set(), date.today())


def test_size_ratchet_fixed_limit_and_shallow_parent(tmp_path, monkeypatch):
    import json

    import _size_ratchet as ratchet
    import pytest

    monkeypatch.setattr(ratchet, "BASELINES_DIR", tmp_path)
    (tmp_path / "budget.json").write_text(
        json.dumps({"default_limit": 2001, "ceilings": {}}), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="default_limit"):
        ratchet.load_baseline("budget.json")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)

    def fake_git(*args):
        if args == ("cat-file", "-p", "HEAD"):
            return "tree t\nparent missing"
        raise RuntimeError("missing shallow parent")

    monkeypatch.setattr(ratchet, "_git", fake_git)
    with pytest.raises(RuntimeError, match="missing shallow parent"):
        ratchet._comparison_ref()


def test_tracked_source_selection_includes_non_python_and_excludes_vendor(tmp_path, monkeypatch):
    import subprocess

    import _size_ratchet as ratchet

    monkeypatch.setattr(ratchet, "REPO_ROOT", tmp_path)
    names = ["tools/scripts/a.py", "tools/native/src/main.rs", "deploy/install.sh",
             "tools/android/Main.java", "web/templates/page.html", "web/static/vendor/a.js",
             "tools/jadx/bin/jadx.bat", "agent/run"]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" if name == "agent/run" else "", encoding="utf-8")

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=b"\0".join(name.encode() for name in names))

    monkeypatch.setattr(ratchet.subprocess, "run", fake_run)
    paths = ratchet.tracked_source_paths(ratchet.BACKEND_SOURCE_SUFFIXES | ratchet.FRONTEND_SOURCE_SUFFIXES)
    assert {path.relative_to(tmp_path).as_posix() for path in paths} == set(names) - {
        "web/static/vendor/a.js", "tools/jadx/bin/jadx.bat",
    }
