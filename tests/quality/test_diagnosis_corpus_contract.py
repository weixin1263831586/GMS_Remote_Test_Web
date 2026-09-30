"""Golden corpus contract: the corpus itself must always be schema-valid.

The corpus gates AI diagnosis quality; a malformed corpus would silently
grade nothing.  These tests keep ``tests/quality/*.jsonl`` loadable, closed
over its vocabularies, and balanced across the development/holdout split.
They never invoke any AI backend — the corpus contract is pure validation.
"""

import json
import unittest

from tests.quality.diagnosis_corpus import (
    CORPUS_DIR,
    CORPUS_FILES,
    VALID_FORBIDDEN_CLAIMS,
    VALID_PURPOSES,
    VALID_ROOT_CAUSE_CLASSES,
    VALID_SPLITS,
    grade_result,
    load_corpus,
)


class CorpusContractTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_corpus()

    def test_corpus_not_empty_and_split_balanced(self):
        self.assertGreaterEqual(len(self.cases), 4)
        splits = {case["split"] for case in self.cases}
        self.assertIn("development", splits)
        self.assertIn("holdout", splits)
        # holdout 至少一条但不得过半：开发集是主要调优面。
        holdout = [c for c in self.cases if c["split"] == "holdout"]
        self.assertGreaterEqual(len(holdout), 1)
        self.assertLess(len(holdout), len(self.cases))

    def test_ids_unique(self):
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_declared_corpus_file_exists_and_loads(self):
        # fail-closed（全局审查第九节）：声明过的 corpus 文件缺失必须
        # 在这里炸出来，不允许静默跳过把语料池清零、gate 空转。
        for name in CORPUS_FILES:
            path = CORPUS_DIR / name
            self.assertTrue(path.exists(), f"declared corpus file missing: {name}")
        per_file = {
            name: {
                json.loads(line)["id"]
                for line in (CORPUS_DIR / name).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
            for name in CORPUS_FILES
        }
        # 两个池都必须非空，且 purpose 与文件归属一致。
        self.assertGreater(len(per_file["redmine_diagnosis_cases.jsonl"]), 0)
        self.assertGreater(len(per_file["report_failure_cases.jsonl"]), 0)
        for case in self.cases:
            owner = (
                "report_failure_cases.jsonl"
                if case["purpose"] == "report_failure"
                else "redmine_diagnosis_cases.jsonl"
            )
            self.assertIn(
                case["id"], per_file[owner],
                f"{case['id']} lives outside its purpose's corpus file",
            )

    def test_schema_fields_closed(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["split"], VALID_SPLITS)
                self.assertIn(case["purpose"], VALID_PURPOSES)
                self.assertIn("input", case)
                self.assertIn("expected", case)
                expected = case["expected"]
                for cause in expected.get("allowed_root_cause_classes") or []:
                    self.assertIn(
                        cause, VALID_ROOT_CAUSE_CLASSES,
                        f"{case['id']}: unknown root-cause class {cause!r}",
                    )
                for claim in expected.get("forbidden_claims") or []:
                    self.assertIn(
                        claim, VALID_FORBIDDEN_CLAIMS,
                        f"{case['id']}: unknown forbidden claim {claim!r}",
                    )
                self.assertTrue(
                    any(
                        expected.get(key)
                        for key in (
                            "must_retrieve_issue_ids",
                            "must_search_history",
                            "must_use_test_source",
                            "must_use_console_logs",
                        )
                    ),
                    f"{case['id']}: expected must assert at least one "
                    "evidence-recall requirement",
                )

    def test_grader_reports_missing_evidence_as_failure(self):
        case = next(c for c in self.cases if c["id"] == "dynamic-colors-001")
        empty = grade_result({}, case)
        self.assertFalse(empty.passed)
        self.assertIn("history_searched", empty.checks)
        self.assertIn("test_source_used", empty.checks)
        self.assertIn("conclusion_anchored", empty.checks)

    def test_grader_accepts_fully_satisfied_result(self):
        case = next(c for c in self.cases if c["id"] == "dynamic-colors-001")
        result = {
            "issue_id": 648526,
            "history_checked": True,
            "evidence_sources": ["test_source", "console_logs"],
            "retrieved_issue_ids": [648526],
            "root_cause_class": "resource_overlay",
            "claims": ["dynamic colors overlay mismatch"],
            "evidence_refs": ["issue:648526", "test_source:DynamicColorsTest"],
        }
        report = grade_result(result, case)
        self.assertTrue(report.passed, report.failures)

    def test_grader_blocks_forbidden_claim(self):
        case = next(c for c in self.cases if c["id"] == "dynamic-colors-001")
        result = {
            "history_checked": True,
            "evidence_sources": ["test_source"],
            "retrieved_issue_ids": [648526],
            "root_cause_class": "resource_overlay",
            "claims": ["PMIC hardware_failure on the board"],
            "evidence_refs": ["issue:648526"],
        }
        report = grade_result(result, case)
        self.assertFalse(report.passed)
        self.assertIn("no_forbidden_claims", report.checks)
        self.assertFalse(report.checks["no_forbidden_claims"])


if __name__ == "__main__":
    unittest.main()
