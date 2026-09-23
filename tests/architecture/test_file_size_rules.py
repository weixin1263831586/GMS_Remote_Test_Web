"""Python file size ratchet (baseline persisted in baselines/file_size.json).

历史 ceiling 原来以字典形式写在测试内，与代码同文件——同时上调
``MIGRATION_LINE_LIMITS`` 即可绕过「只减不增」约定（评审 P2）。现在
ceilings 独立持久化，本测试负责：

1. 运行时校验：文件行数不超过各自 ceiling（未登记走 600 默认）；
2. ratchet 校验：相对上一个 commit，ceiling 只减不增；上调必须提供
   带 reason/expiry 的 waiver（见 ``_size_ratchet``）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _size_ratchet import assert_no_ceiling_raises, load_baseline


ROOT = Path(__file__).resolve().parents[2]
BASELINE_NAME = "file_size.json"


def _limits() -> tuple[dict[str, int], int]:
    data = load_baseline(BASELINE_NAME)
    return data["ceilings"], int(data.get("default_limit") or 600)


class FileSizeRuleTests(unittest.TestCase):
    def test_new_python_modules_stay_reviewable(self):
        ceilings, default_limit = _limits()
        offenders = []
        for base in ('bootstrap', 'foundation', 'features', 'workflows'):
            for path in (ROOT / base).rglob('*.py'):
                relative = str(path.relative_to(ROOT))
                count = len(path.read_text(encoding='utf-8').splitlines())
                limit = ceilings.get(relative, default_limit)
                if count > limit:
                    offenders.append((relative, count, limit))
        self.assertEqual(
            offenders,
            [],
            "python file exceeds its registered line ceiling "
            "(baselines/file_size.json; shrink the file, don't raise it)",
        )

    def test_baseline_ratchet_no_ceiling_raises(self):
        """相对 HEAD，ceiling 不允许上调/新增，除非同文件 waivers 豁免。"""
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
