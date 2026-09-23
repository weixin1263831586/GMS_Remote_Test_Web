"""mainline LIKE ESCAPE 回归(全局深度审核发现)。

``escape_like`` 转义了 ``%``/``_``/``\\``,但 list 端点的 LIKE 语句此前
缺 ``ESCAPE "\\"`` 子句:转义后的反斜杠被 SQLite 按字面解释,含 ``%``/
``_`` 的关键词既匹配不准,也允许用户用通配符制造全表扫描。
"""

from __future__ import annotations

import sqlite3
import unittest

from features.system.mainline_issues.repository import escape_like, init_db


class LikeEscapeClauseTests(unittest.TestCase):
    def _rows(self, conn: sqlite3.Connection, q: str) -> list[str]:
        like = f"%{escape_like(q)}%"
        rows = conn.execute(
            "SELECT test_module FROM mainline_known_issues "
            "WHERE test_module LIKE ? ESCAPE '\\'",
            (like,),
        ).fetchall()
        return [row[0] for row in rows]

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_db(self.conn)
        for module in ("Cts%Wildcard_Module", "CtsXWildcardXModule"):
            self.conn.execute(
                """
                INSERT INTO mainline_known_issues (
                    source_url, source_title, release_year, release_label,
                    product_section, issue_type, android_versions, category,
                    test_module, test_case, exemption_id, issue_text,
                    first_seen_at, last_seen_at
                ) VALUES (
                    'https://example.com/x', 't', 2026, 'Android 16 QPR1',
                    'Android', 'CTS', '16', 'stress',
                    ?, 'pkg.Cls#method', 'b/123', 'text',
                    '2026-01-01', '2026-01-01'
                )
                """,
                (module,),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_literal_percent_is_matched_literally(self):
        self.assertEqual(self._rows(self.conn, "Cts%Wildcard"), ["Cts%Wildcard_Module"])

    def test_literal_underscore_is_matched_literally(self):
        self.assertEqual(self._rows(self.conn, "Wildcard_Module"), ["Cts%Wildcard_Module"])

    def test_plain_substring_still_matches(self):
        result = self._rows(self.conn, "Wildcard")
        self.assertEqual(len(result), 2)


class ListEndpointSqlUsesEscape(unittest.TestCase):
    """list 端点生成的 SQL 必须携带 ESCAPE 子句(源码级回归)。"""

    def test_api_source_contains_escape_clause(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2]
            / "features/system/mainline_issues/api.py"
        ).read_text(encoding="utf-8")
        self.assertIn("ESCAPE", source)
        # 10 列 LIKE 全部都要有 ESCAPE;粗算配对数不少于 LIKE 数。
        self.assertLessEqual(source.count("LIKE ?"), source.count("ESCAPE"))


if __name__ == "__main__":
    unittest.main()
