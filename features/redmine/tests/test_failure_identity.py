"""FailureIdentity 确定性指纹与关系层契约测试（全局审查：失败身份与关系分层）。"""

import unittest

from features.redmine.failure_identity import (
    VALID_RELATIONS,
    assertion_class,
    build_failure_clusters,
    failure_identity,
    normalize_error_signature,
    relation_class,
)


def _identity(**overrides):
    kwargs = {
        "suite": "CTS",
        "android_version": "17",
        "device_class": "rk3588",
    }
    kwargs.update(overrides)
    return kwargs


class NormalizationTests(unittest.TestCase):
    def test_volatile_parts_are_stripped(self):
        first = "java.lang.AssertionError: expected <3> but was <5> at 0x7f8a2c00 on 192.168.1.10"
        second = "java.lang.AssertionError: expected <9> but was <2> at 0xdeadbeef on 10.0.0.1"
        self.assertEqual(
            normalize_error_signature(first),
            normalize_error_signature(second),
        )

    def test_different_assertions_keep_distinct_signatures(self):
        first = normalize_error_signature("junit.framework.AssertionFailedError: color mismatch")
        second = normalize_error_signature("java.lang.NullPointerException: overlay lookup")
        self.assertNotEqual(first, second)

    def test_assertion_class_extraction(self):
        self.assertEqual(
            assertion_class("at org.junit.Assert.fail; junit.framework.AssertionFailedError: x"),
            "junit.framework.AssertionFailedError",
        )
        self.assertEqual(assertion_class("no exception here"), "")


class IdentityTests(unittest.TestCase):
    def test_same_root_cause_same_fingerprint(self):
        base = [
            {"module": "CtsSystemUiTestCases", "name": "DynamicColorsTest",
             "reason": "AssertionError: palette mismatch at 0x11 count=3"},
        ]
        other = [
            {"module": "CtsSystemUiTestCases", "name": "DynamicColorsTest",
             "reason": "AssertionError: palette mismatch at 0x99 count=77"},
        ]
        first = failure_identity(base, **_identity())
        second = failure_identity(other, **_identity())
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["assertion_class"], "AssertionError")

    def test_any_context_change_changes_fingerprint(self):
        failures = [{"module": "M", "name": "T", "reason": "AssertionError: x"}]
        base = failure_identity(failures, **_identity())
        other = failure_identity(failures, **_identity(android_version="16"))
        self.assertNotEqual(base["fingerprint"], other["fingerprint"])

    def test_empty_failures_still_bounded(self):
        identity = failure_identity([], **_identity())
        self.assertTrue(identity["fingerprint"])
        self.assertEqual(identity["error_signature"], "")


class RelationTests(unittest.TestCase):
    def test_same_failure_by_fingerprint(self):
        failures = [{"module": "M", "name": "T", "reason": "AssertionError: x"}]
        first = failure_identity(failures, **_identity())
        second = failure_identity(failures, **_identity())
        self.assertEqual(relation_class(first, second), "SAME_FAILURE")

    def test_same_test_different_cause_never_merges(self):
        # 钉子案例：同名用例、不同根因 → 不得进同一 Cluster。
        first = failure_identity(
            [{"module": "CtsJank", "name": "TestA", "reason": "AssertionError: overlay mismatch"}],
            **_identity(),
        )
        second = failure_identity(
            [{"module": "CtsJank", "name": "TestA", "reason": "java.lang.NullPointerException: service died"}],
            **_identity(),
        )
        self.assertEqual(
            relation_class(first, second), "SAME_TEST_DIFFERENT_CAUSE",
        )
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_similar_symptom_across_tests_is_only_candidate(self):
        first = failure_identity(
            [{"module": "A", "name": "Test1", "reason": "AssertionError: overlay mismatch"}],
            **_identity(),
        )
        second = failure_identity(
            [{"module": "B", "name": "Test2", "reason": "AssertionError: overlay mismatch"}],
            **_identity(),
        )
        self.assertEqual(relation_class(first, second), "SIMILAR_SYMPTOM")

    def test_unrelated(self):
        first = failure_identity(
            [{"module": "A", "name": "T1", "reason": "AssertionError: overlay"}],
            **_identity(),
        )
        second = failure_identity(
            [{"module": "B", "name": "T2", "reason": "java.lang.IllegalStateException: camera"}],
            **_identity(device_class="rk3576"),
        )
        self.assertEqual(relation_class(first, second), "UNRELATED")

    def test_relation_vocabulary_is_closed(self):
        failures = [{"module": "M", "name": "T", "reason": "AssertionError: x"}]
        first = failure_identity(failures, **_identity())
        second = failure_identity(failures, **_identity(android_version="15"))
        produced = {relation_class(first, second), relation_class(first, first)}
        self.assertTrue(produced <= set(VALID_RELATIONS))


class ClusterTests(unittest.TestCase):
    def test_only_exact_fingerprints_cluster(self):
        identities = [
            failure_identity([{"module": "M", "name": "T", "reason": "AssertionError: x at 0x1"}], **_identity()),
            failure_identity([{"module": "M", "name": "T", "reason": "AssertionError: x at 0x2"}], **_identity()),
            failure_identity([{"module": "M", "name": "T", "reason": "java.lang.NullPointerException: y"}], **_identity()),
        ]
        clusters = build_failure_clusters(
            identities, subject_ids=[101, 202, 303],
        )
        self.assertEqual(len(clusters), 2)
        self.assertEqual(clusters[0]["size"], 2)
        self.assertEqual(sorted(clusters[0]["members"]), [101, 202])
        self.assertEqual(clusters[1]["members"], [303])

    def test_cluster_keeps_representative_identity(self):
        identity = failure_identity(
            [{"module": "M", "name": "T", "reason": "AssertionError: x"}],
            **_identity(),
        )
        clusters = build_failure_clusters([identity], subject_ids=[7])
        self.assertEqual(clusters[0]["identity"]["module"], "M")
        self.assertEqual(clusters[0]["identity"]["testcase"], "T")


if __name__ == "__main__":
    unittest.main()
