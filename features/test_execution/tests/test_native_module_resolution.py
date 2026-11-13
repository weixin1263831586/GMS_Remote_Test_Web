"""Exact native module queries must never silently resolve to a nearby JAR."""

from features.test_execution.suite_module_apk import _pick_suite_module_artifact
from features.test_execution.suite_modules import normalize_module_query


def module(name, filenames):
    return {"module": name, "suite_version": "17_r3", "suite_path": "/suite/tools",
            "files": [{"file_name": filename, "relative_path": f"{name}/{filename}"} for filename in filenames]}


def test_normalization_preserves_identifiers_inside_natural_language():
    for query in ("CtsPerfettoTestCases", "分析 CtsPerfettoTestCases 模块", "查询CtsPerfettoTestCases模块"):
        assert normalize_module_query(query) == "CtsPerfettoTestCases"


def test_native_module_does_not_fall_back_to_another_modules_jar():
    payload = {"query": "CtsPerfettoTestCases", "normalized_query": "CtsPerfettoTestCases",
               "modules": [module("CtsPerfettoTestCases", ["CtsPerfettoTestCases.config"]),
                           module("CtsPerfettoAtomHostTestCases", ["CtsPerfettoAtomHostTestCases.jar"])]}
    assert _pick_suite_module_artifact(payload, "apk") is None


def test_an_exact_apk_still_resolves_and_keeps_its_suite_version():
    payload = {"query": "CtsExampleTestCases", "normalized_query": "CtsExampleTestCases",
               "modules": [module("CtsExampleTestCases", ["CtsExampleTestCases.apk"]),
                           module("CtsExampleHostTestCases", ["CtsExampleHostTestCases.jar"])]}
    resolved = _pick_suite_module_artifact(payload, "apk")
    assert resolved["module"] == "CtsExampleTestCases" and resolved["suite_version"] == "17_r3"
