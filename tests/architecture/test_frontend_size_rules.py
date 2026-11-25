"""First-party frontend line limits and independent startup payload budget."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _size_ratchet import FRONTEND_SOURCE_SUFFIXES, assert_no_ceiling_raises, load_baseline, tracked_source_paths


ROOT = Path(__file__).resolve().parents[2]
BASELINE_NAME = "frontend_size.json"
ASSET_SUFFIXES = FRONTEND_SOURCE_SUFFIXES


class FrontendSizeRuleTests(unittest.TestCase):
    @staticmethod
    def _scanned_assets():
        yield from tracked_source_paths(ASSET_SUFFIXES)

    def test_web_assets_stay_within_budgets(self):
        data = load_baseline(BASELINE_NAME)
        default = int(data.get("default_limit") or 2000)
        offenders = []
        for path in self._scanned_assets():
            relative = str(path.relative_to(ROOT))
            count = len(path.read_text(encoding='utf-8').splitlines())
            limit = data["ceilings"].get(relative, default)
            if count > limit:
                offenders.append((relative, count, limit))
        self.assertEqual(offenders, [], f"frontend asset exceeds its line budget: {offenders}")

    def test_baseline_ratchet_no_ceiling_raises(self):
        assert_no_ceiling_raises(BASELINE_NAME)

    def test_registered_waivers_are_not_expired(self):
        from datetime import date

        data = load_baseline(BASELINE_NAME)
        expired = []
        for key, waiver in (data.get("waivers") or {}).items():
            expires_raw = str(waiver.get("expires") or "")
            try:
                expires = date.fromisoformat(expires_raw[:10])
            except ValueError:
                expired.append((key, f"unparseable expires {expires_raw!r}"))
                continue
            if expires < date.today():
                expired.append((key, f"expired {expires.isoformat()}"))
        self.assertEqual(expired, [], f"expired frontend line-budget waivers: {expired}")

    def test_total_first_party_js_budget(self):
        base_js = sorted((ROOT / 'web/static/js').glob('*.js'))
        total = sum(path.stat().st_size for path in base_js)
        self.assertLessEqual(total, 231 * 1024,
                             f"base JS total {total} bytes exceeds the 231 KB budget")


if __name__ == '__main__':
    unittest.main()
