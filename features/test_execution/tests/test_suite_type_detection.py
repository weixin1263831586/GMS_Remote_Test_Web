from features.test_execution.suites import detect_test_type_from_suite_path


def test_detect_cts_verifier_nested_layout():
    path = (
        "/home/test/Software/suites/android-cts-verifier-16_r3-linux_x86-arm/"
        "android-cts-verifier/android-cts-v-host/tools"
    )
    assert detect_test_type_from_suite_path(path) == "cts-v"


def test_detect_cts_v_host_layout():
    assert detect_test_type_from_suite_path(
        "/opt/suites/android-cts-v-host/tools"
    ) == "cts-v"


def test_detect_gts_root_before_generic_gts():
    assert detect_test_type_from_suite_path(
        "/opt/suites/android-gts-root-16_r1/tools"
    ) == "gts-root"


def test_detect_regular_suite_types():
    assert detect_test_type_from_suite_path("/opt/android-cts/tools") == "cts"
    assert detect_test_type_from_suite_path("/opt/android-vts/tools") == "vts"
    assert detect_test_type_from_suite_path("/opt/android-gts/tools") == "gts"
    assert detect_test_type_from_suite_path("/opt/android-sts/tools") == "sts"


def test_unknown_suite_path_returns_none():
    assert detect_test_type_from_suite_path("/opt/custom-suite/tools") is None
