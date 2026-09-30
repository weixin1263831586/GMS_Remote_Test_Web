"""FailureIdentity 确定性指纹与关系层契约测试（全局审查：失败身份与关系分层）。"""

import unittest

from features.redmine.failure_identity import (
    VALID_RELATIONS,
    assertion_class,
    build_failure_clusters,
    extract_test_identity,
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
        first = "AssertionError: expected <3> but was <5> at 0x7f8a2c00 on 192.168.1.10 pid=4321 took 1234ms"
        second = "AssertionError: expected <3> but was <5> at 0xdeadbeef on 10.0.0.1 pid=8765 took 9876ms"
        self.assertEqual(
            normalize_error_signature(first),
            normalize_error_signature(second),
        )

    def test_timestamps_uuids_and_reference_ids_are_stripped(self):
        first = "failed at 2026-09-30 11:08:44.123 tid=771 attempt=2 (see #481603)"
        second = "failed at 2026-10-01 23:59:59.999 tid=42 attempt=9 (see #481999)"
        self.assertEqual(
            normalize_error_signature(first),
            normalize_error_signature(second),
        )
        with_uuid = "corrupt 550e8400-e29b-41d4-a716-446655440000 vs plain"
        self.assertEqual(
            normalize_error_signature(with_uuid),
            normalize_error_signature("corrupt vs plain"),
        )

    def test_domain_numbers_are_preserved(self):
        # expected/actual、size、API level 是根因签名本身，必须区分
        # （全局审查 4.3：vbkey size 0 vs 32、API 36 vs 37）。
        self.assertNotEqual(
            normalize_error_signature("AssertionError: expected <3> but was <5>"),
            normalize_error_signature("AssertionError: expected <9> but was <2>"),
        )
        self.assertNotEqual(
            normalize_error_signature("vbkey size 0 mismatch"),
            normalize_error_signature("vbkey size 32 mismatch"),
        )
        self.assertNotEqual(
            normalize_error_signature("requires API level 36"),
            normalize_error_signature("requires API level 37"),
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


class ExtractTestIdentityTests(unittest.TestCase):
    def test_xts_subject_shapes(self):
        extracted = extract_test_identity(
            "[CTS] CtsSystemUiTestCases: android.systemui.BarTest#testOverlay 失败"
        )
        self.assertEqual(extracted["suite"], "CTS")
        self.assertEqual(extracted["module"], "CtsSystemUiTestCases")
        self.assertEqual(extracted["testcase"], "android.systemui.BarTest#testOverlay")

    def test_dotted_method_and_suite_alias(self):
        extracted = extract_test_identity("gts GtsSecurityHostTestCases cent.BazTest.testA failed")
        self.assertEqual(extracted["suite"], "GTS")
        self.assertEqual(extracted["module"], "GtsSecurityHostTestCases")
        self.assertEqual(extracted["testcase"], "cent.BazTest.testA")

    def test_test_class_module_suffix(self):
        extracted = extract_test_identity(
            "VTS VtsKernelTestClass android.kernel.FooTest#testBoot failed"
        )
        self.assertEqual(extracted["suite"], "VTS")
        self.assertEqual(extracted["module"], "VtsKernelTestClass")

    def test_free_text_degrades_to_empty(self):
        extracted = extract_test_identity("设备 A 无法开机，logcat 无响应，问题很严重")
        self.assertEqual(extracted, {"suite": "", "module": "", "testcase": ""})

    def test_plain_english_tests_is_not_a_module(self):
        extracted = extract_test_identity("regression Tests in suite cts are flaky")
        self.assertEqual(extracted["suite"], "CTS")
        self.assertEqual(extracted["module"], "")


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

    def test_device_serial_is_observation_metadata_never_fingerprint(self):
        # 全局审查 4.2：同型号两台设备的同一失败必须同一指纹，
        # serial 只随身份落库作观察元数据。
        failures = [{"module": "M", "name": "T", "reason": "AssertionError: x"}]
        device_a = failure_identity(failures, **_identity(), device_serial="RK3576A001")
        device_b = failure_identity(failures, **_identity(), device_serial="RK3576B002")
        self.assertEqual(device_a["fingerprint"], device_b["fingerprint"])
        self.assertEqual(device_a["device_serial"], "RK3576A001")
        self.assertEqual(device_b["device_serial"], "RK3576B002")

    def test_device_class_still_changes_fingerprint(self):
        failures = [{"module": "M", "name": "T", "reason": "AssertionError: x"}]
        base = failure_identity(failures, **_identity())
        other = failure_identity(failures, **_identity(device_class="rk3576"))
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
