"""DiagnosisReadModel canonical 形状契约测试（全局审查第二十节）。

覆盖：builder 章节封闭性、归一化器的防御性（非 dict/缺键/超长文本）、
conclusion 封闭词表 fail-closed、validator 形状校验、适配器纯函数性
（无 IO、不改输入）。
"""

from __future__ import annotations

import unittest

from foundation.diagnosis_read_model import (
    CONCLUSION_STATUSES,
    DIAGNOSIS_READ_MODEL_SCHEMA_VERSION,
    build_conclusion,
    build_diagnosis_read_model,
    build_failure_cluster_stub,
    normalize_background_entries,
    normalize_evidence_entries,
    normalize_recommended_actions,
    normalize_similar_cases,
    validate_diagnosis_read_model,
)


class BuildDiagnosisReadModelTests(unittest.TestCase):
    def test_minimal_model_has_all_sections_with_explicit_empty_values(self):
        model = build_diagnosis_read_model(
            producer="test.producer", subject={"issue_id": 1},
        )
        self.assertEqual(model["schema_version"], DIAGNOSIS_READ_MODEL_SCHEMA_VERSION)
        for key in (
            "failure_identity", "failure_cluster", "conclusion",
            "evidence", "background", "similar_cases", "recommended_actions",
        ):
            self.assertIn(key, model)
        self.assertEqual(model["evidence"], [])
        self.assertIsNone(model["failure_identity"])

    def test_subject_and_producer_required(self):
        model = build_diagnosis_read_model(producer="", subject={})
        self.assertEqual(model["producer"], "")
        self.assertEqual(model["subject"], {})


class NormalizerTests(unittest.TestCase):
    def test_recommended_actions_accept_strings_and_dicts(self):
        actions = normalize_recommended_actions([
            {"action": "b", "step": 2},
            "plain string action",
            {"title": "titled", "reason": "r"},
            {"action": "   "},
            None,
        ])
        # step 相同按插入序（stable sort）；字符串项自动补 step。
        self.assertEqual([a["action"] for a in actions], [
            "b", "plain string action", "titled",
        ])
        self.assertEqual([a["step"] for a in actions], [2, 2, 3])
        self.assertEqual([a["reason"] for a in actions], ["", "", "r"])

    def test_recommended_actions_bad_step_falls_back_to_position(self):
        actions = normalize_recommended_actions([{"action": "x", "step": "NaN"}])
        self.assertEqual(actions[0]["step"], 1)

    def test_evidence_entries_drop_empty_and_keep_four_keys(self):
        entries = normalize_evidence_entries([
            {"kind": "ai_evidence", "source": "log", "reference": "r", "fact": "f"},
            {"kind": "x"},
            "bare fact",
            42,
        ])
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            set(entries[0]), {"kind", "source", "reference", "fact"},
        )
        self.assertEqual(entries[1]["fact"], "bare fact")

    def test_evidence_accepts_url_and_path_aliases(self):
        entries = normalize_evidence_entries([
            {"url": "https://example.com", "detail": "d"},
            {"path": "a/b.java", "analysis": "x"},
        ])
        self.assertEqual(entries[0]["reference"], "https://example.com")
        self.assertEqual(entries[1]["reference"], "a/b.java")

    def test_background_entries_keep_layer_stamp_but_require_content(self):
        entries = normalize_background_entries([
            {"title": "LMKD", "source": "android_internals", "evidence_level": "background"},
            {"title": "  ", "source": " "},
            "not a dict",
        ])
        self.assertEqual(len(entries), 1)
        # evidence_level 是联邦层的 background 盖章（证据层级），不是核验状态。
        self.assertEqual(entries[0]["evidence_level"], "background")
        self.assertEqual(entries[0]["anchor_status"], "unknown")
        self.assertNotIn("fact", entries[0])

    def test_background_anchor_status_aggregates_from_verifications(self):
        entries = normalize_background_entries([
            {"title": "LMKD", "source": "s", "evidence_level": "background",
             "anchor_verifications": [
                 {"evidence_level": "unknown"}, {"evidence_level": "path_matched"},
             ]},
            {"title": "Binder", "source": "s",
             "anchor_verifications": [{"evidence_level": "path_missing"}]},
            {"title": "Zygote", "source": "s", "anchor_verifications": []},
            {"title": "Pinner", "source": "s"},
            {"title": "Bad", "source": "s", "anchor_verifications": "not-a-list"},
        ])
        self.assertEqual(
            [e["anchor_status"] for e in entries],
            ["path_matched", "path_missing", "unknown", "unknown", "unknown"],
        )

    def test_background_provenance_keys_are_preserved(self):
        entries = normalize_background_entries([
            {"title": "LMKD", "source": "s", "source_revision": "abc123",
             "license": "CC BY-NC-SA 4.0", "last_verified": "2026-09-29"},
        ])
        self.assertEqual(entries[0]["source_revision"], "abc123")
        self.assertEqual(entries[0]["license"], "CC BY-NC-SA 4.0")
        self.assertEqual(entries[0]["last_verified"], "2026-09-29")

    def test_similar_cases_keep_native_relation_vocabulary(self):
        entries = normalize_similar_cases([
            {"issue_id": 648526, "subject": "s", "similarity": "same",
             "reusable_fix": "fix", "reference_fact": "fact"},
            {"title": "no id"},
            {},
        ])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["relation"], "same")
        self.assertEqual(entries[0]["fact"], "fact")
        self.assertIsNone(entries[1]["id"])

    def test_long_text_is_truncated_not_rejected(self):
        entries = normalize_evidence_entries([
            {"fact": "x" * 5000, "reference": "y" * 5000},
        ])
        self.assertEqual(len(entries[0]["fact"]), 2000)
        self.assertEqual(len(entries[0]["reference"]), 2000)


class ConclusionTests(unittest.TestCase):
    def test_status_fail_closed_to_unknown(self):
        self.assertEqual(build_conclusion(status="made_up")["status"], "unknown")
        for status in CONCLUSION_STATUSES:
            self.assertEqual(build_conclusion(status=status)["status"], status)

    def test_confidence_rejects_bool_and_non_numeric(self):
        self.assertIsNone(build_conclusion(confidence=True)["confidence"])
        self.assertIsNone(build_conclusion(confidence="0.9")["confidence"])
        self.assertEqual(build_conclusion(confidence=0.7)["confidence"], 0.7)

    def test_missing_information_strings_only(self):
        conclusion = build_conclusion(missing_information=["a", "", 3, None])
        self.assertEqual(conclusion["missing_information"], ["a", "3"])


class FailureClusterStubTests(unittest.TestCase):
    def test_none_without_identity_or_fingerprint(self):
        self.assertIsNone(build_failure_cluster_stub(None))
        self.assertIsNone(build_failure_cluster_stub({"module": "m"}))

    def test_members_only_from_explicit_related(self):
        cluster = build_failure_cluster_stub(
            {"fingerprint": "abc"}, related_members=[1, 2],
        )
        self.assertEqual(cluster["member_count"], 3)
        self.assertTrue(cluster["aggregated"])


class ValidateTests(unittest.TestCase):
    def test_valid_model_passes(self):
        model = build_diagnosis_read_model(
            producer="p", subject={"id": 1},
            conclusion=build_conclusion(status="likely"),
        )
        self.assertEqual(validate_diagnosis_read_model(model), [])

    def test_invalid_models_report_problems(self):
        self.assertEqual(validate_diagnosis_read_model("nope"), ["model must be a dict"])
        problems = validate_diagnosis_read_model({
            "schema_version": 99,
            "subject": {},
            "conclusion": {"status": "made_up"},
            "evidence": "not-a-list",
        })
        self.assertIn("schema_version must be 1", problems)
        self.assertIn("missing field: producer", problems)
        self.assertIn("conclusion.status outside closed vocabulary", problems)
        self.assertIn("evidence must be a list", problems)

    def test_none_valued_optional_sections_are_valid(self):
        model = build_diagnosis_read_model(producer="p", subject={"id": 1})
        model["failure_cluster"] = "should-be-dict-or-none"
        self.assertIn("failure_cluster must be a dict or None",
                      validate_diagnosis_read_model(model))


if __name__ == "__main__":
    unittest.main()
