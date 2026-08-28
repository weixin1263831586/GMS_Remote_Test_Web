from __future__ import annotations

import glob
import logging
import os
import re
import shlex
from typing import Any

from . import runtime
from .suites import (
    build_suite_info,
    get_default_suites_path,
    is_config_host_local,
    list_local_test_suites,
)


logger = logging.getLogger(__name__)


# ==================== Suite helpers ====================

def get_available_test_suites(config: dict[str, Any], base_path: str | None = None) -> list[dict[str, str]]:
    """Return all test suites visible to the current host/config."""
    base_path = base_path or config.get("suites_path") or get_default_suites_path(config)
    if is_config_host_local(config):
        return list_local_test_suites(base_path)

    with runtime.ssh_manager.optional_connection(config) as ssh:
        if not ssh:
            raise RuntimeError("SSH connection failed")

        find_cmd = f"find {shlex.quote(base_path)} -maxdepth 5 -type f -executable -name '*-tradefed' 2>/dev/null | sort"
        output, _, _ = runtime.ssh_manager.execute_command(ssh, find_cmd, timeout=30)
        suites = []
        if output.strip():
            for line in output.strip().split("\n"):
                suite = build_suite_info(line)
                if suite:
                    suites.append(suite)
        return suites


def _suite_reference_names(suite: dict[str, Any]) -> list[str]:
    tools_path = str(suite.get("tools_path") or "").rstrip("/")
    suite_root = tools_path[: -len("/tools")] if tools_path.endswith("/tools") else tools_path
    names = [str(suite.get("version") or "")]
    if suite_root:
        names.append(os.path.basename(suite_root))
    if tools_path:
        names.append(os.path.basename(tools_path))
    return [name for name in names if name]


def _suite_reference_ambiguous_message(reference: str, suites: list[dict[str, Any]]) -> str:
    versions = [str(suite.get("version") or suite.get("tools_path") or "") for suite in suites[:8]]
    more = f" (+{len(suites) - len(versions)} more)" if len(suites) > len(versions) else ""
    return f"Suite reference '{reference}' is ambiguous: {', '.join(versions)}{more}"


def _deduplicate_suite_locations(suites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse multiple Tradefed launchers that share one tools directory."""
    unique = []
    seen_paths = set()
    for suite in suites:
        tools_path = str(suite.get("tools_path") or "").rstrip("/\\")
        if tools_path and tools_path in seen_paths:
            continue
        if tools_path:
            seen_paths.add(tools_path)
        unique.append(suite)
    return unique


def resolve_suite_reference(
    config: dict[str, Any], reference: str, base_path: str | None = None
) -> tuple[dict[str, str] | None, str]:
    """Resolve a short suite name (e.g. android-cts-17_r1) to a suite entry.

    Returns ``(suite, message)``. ``suite`` is the matched suite dict or None;
    ``message`` explains an empty match (ambiguous or not found). References
    containing a path separator resolve to ``(None, "")`` so callers can keep
    treating them as plain paths. Matching: exact suite version / directory
    name first, then a unique case-insensitive substring match.
    """
    reference = str(reference or "").strip()
    if not reference or "/" in reference or "\\" in reference:
        return None, ""

    suites = _deduplicate_suite_locations(get_available_test_suites(config, base_path))

    exact = [suite for suite in suites if reference in _suite_reference_names(suite)]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, _suite_reference_ambiguous_message(reference, exact)

    lowered = reference.lower()
    fuzzy = [
        suite for suite in suites
        if any(lowered in name.lower() for name in _suite_reference_names(suite))
    ]
    if len(fuzzy) == 1:
        return fuzzy[0], ""
    if len(fuzzy) > 1:
        return None, _suite_reference_ambiguous_message(reference, fuzzy)
    return None, f"No test suite matches '{reference}'. List available suites via GET /api/test/suites."


def _normalize_suite_match_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _normalize_suite_version_text(version: str) -> str:
    version = (version or "").strip().lower()
    if not version:
        return ""
    for old, new in (("android-", ""), ("cts-verifier", "ctsverifier"), ("cts-v", "ctsv")):
        version = version.replace(old, new)
    return _normalize_suite_match_text(version)


def _score_suite_version_match(suite_version: str, report_version: str) -> int:
    suite_norm = _normalize_suite_version_text(suite_version)
    report_norm = _normalize_suite_version_text(report_version)
    if not suite_norm or not report_norm:
        return 0
    if suite_norm == report_norm:
        return 100
    if report_norm in suite_norm or suite_norm in report_norm:
        return 80
    return 0


def _build_apk_source_path_guess(test_name: str, class_names: list[str] | None = None) -> dict[str, Any]:
    class_names = [c for c in (class_names or []) if c]
    candidate = class_names[0] if class_names else ""
    if not candidate and test_name and "#" in test_name:
        candidate = test_name.split("#", 1)[0]

    simple_class = (candidate or "").split("$")[0].strip()
    if not simple_class or "." not in simple_class:
        return {"source_path": "", "class_name": simple_class, "line_number": 0}

    return {"source_path": f"{simple_class.replace('.', '/')}.java", "class_name": simple_class, "line_number": 0}


def _build_artifact_candidate(full_path: str, suite_root: str, include_size: bool = False) -> dict[str, Any]:
    name = os.path.basename(full_path)
    lower = name.lower()
    entry = {"name": name, "path": os.path.relpath(full_path, suite_root), "full_path": full_path, "is_apk": lower.endswith(".apk"), "is_jar": lower.endswith(".jar")}
    if include_size:
        entry["size"] = os.path.getsize(full_path) if os.path.exists(full_path) else 0
    return entry


def _collect_suite_artifact_candidates_local(suite_root: str, max_results: int = 200) -> list[dict[str, Any]]:
    candidates = []
    if not os.path.isdir(suite_root):
        return candidates
    for root, _, files in os.walk(suite_root):
        for file_name in files:
            lower = file_name.lower()
            if not (lower.endswith(".apk") or lower.endswith(".jar")):
                continue
            full_path = os.path.join(root, file_name)
            candidates.append(_build_artifact_candidate(full_path, suite_root, include_size=True))
            if len(candidates) >= max_results:
                return candidates
    return candidates


def _collect_suite_artifact_candidates_remote(ssh, suite_root: str, max_results: int = 200) -> list[dict[str, Any]]:
    find_cmd = f"find {shlex.quote(suite_root)} -type f \\( -iname '*.apk' -o -iname '*.jar' \\) 2>/dev/null | sort"
    output, _, _ = runtime.ssh_manager.execute_command(ssh, find_cmd, timeout=45)
    candidates = []
    if not output.strip():
        return candidates
    for line in output.strip().split("\n"):
        full_path = line.strip()
        if not full_path:
            continue
        candidates.append(_build_artifact_candidate(full_path, suite_root))
        if len(candidates) >= max_results:
            break
    return candidates


def _collect_preferred_suite_artifact_candidates_local(suite_root: str, module: str, max_results: int = 20) -> list[dict[str, Any]]:
    candidates = []
    if not module or not os.path.isdir(suite_root):
        return candidates
    seen = set()
    patterns = [os.path.join(suite_root, "**", f"{module}.apk"), os.path.join(suite_root, "**", f"{module}.jar")]
    for pattern in patterns:
        for full_path in glob.glob(pattern, recursive=True):
            if not os.path.isfile(full_path):
                continue
            normalized = os.path.abspath(full_path)
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(_build_artifact_candidate(normalized, suite_root, include_size=True))
            if len(candidates) >= max_results:
                return candidates
    return candidates


def _collect_preferred_suite_artifact_candidates_remote(ssh, suite_root: str, module: str, max_results: int = 20) -> list[dict[str, Any]]:
    candidates = []
    if not module:
        return candidates
    apk_name = shlex.quote(f"{module}.apk")
    jar_name = shlex.quote(f"{module}.jar")
    find_cmd = f"find {shlex.quote(suite_root)} -type f \\( -iname {apk_name} -o -iname {jar_name} \\) 2>/dev/null | sort"
    output, _, _ = runtime.ssh_manager.execute_command(ssh, find_cmd, timeout=45)
    if not output.strip():
        return candidates
    seen = set()
    for line in output.strip().split("\n"):
        full_path = line.strip()
        if not full_path:
            continue
        normalized = os.path.abspath(full_path)
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(_build_artifact_candidate(normalized, suite_root))
        if len(candidates) >= max_results:
            break
    return candidates


def _score_suite_artifact_candidate(candidate: dict[str, Any], search_terms: list[str], suite_version: str = "", test_type: str = "", module: str = "", source_path: str = "") -> dict[str, Any]:
    haystack = _normalize_suite_match_text(" ".join([candidate.get("name", ""), candidate.get("path", ""), suite_version, test_type]))
    score = 0
    reasons = []
    candidate_path = (candidate.get("path", "") or "").replace("\\", "/").lower()
    candidate_name = (candidate.get("name", "") or "").lower()
    module_name = (module or "").strip()
    module_norm = _normalize_suite_match_text(module_name)

    if candidate.get("is_apk"):
        score += 5
        reasons.append("apk")
    if candidate.get("is_jar"):
        score += 3
        reasons.append("jar")

    for term in search_terms:
        norm = _normalize_suite_match_text(term)
        if not norm:
            continue
        if norm in haystack:
            score += 25 + min(len(norm), 20)
            reasons.append(term)
            continue
        token_parts = [part for part in re.split(r"[^a-zA-Z0-9]+", term) if len(part) >= 4]
        if any(_normalize_suite_match_text(part) in haystack for part in token_parts):
            score += 10
            reasons.append(term)

    if module_norm:
        exact_module_apk = f"{module_name.lower()}.apk"
        exact_module_jar = f"{module_name.lower()}.jar"
        if candidate_name in {exact_module_apk, exact_module_jar}:
            score += 160
            reasons.append("exact-module-binary")
        if module_norm in _normalize_suite_match_text(candidate_name) or module_norm in haystack:
            score += 35
            reasons.append(f"module:{module_name}")
        if candidate_path.endswith(f"/{exact_module_apk}") or candidate_path.endswith(f"/{exact_module_jar}"):
            score += 60
            reasons.append("module-binary")
        elif f"/{module_name.lower()}/" in candidate_path:
            score += 45
            reasons.append("module-path")

    if source_path:
        source_norm = _normalize_suite_match_text(source_path)
        if source_norm and source_norm in haystack:
            score += 20
            reasons.append("source-path")

    if "testcase" in candidate_name or "testcases" in candidate_name:
        score += 10
    if "android" in candidate.get("path", "").lower():
        score += 2

    scored = dict(candidate)
    scored["score"] = score
    scored["reasons"] = list(dict.fromkeys(reasons))[:8]
    return scored


_SUITE_TYPE_ALIASES: dict[str, set] = {"cts": {"cts", "cts-v"}, "gts-root": {"gts"}, "apts": {"gts"}}


def _canonical_suite_types(test_type: str) -> set:
    test_type = (test_type or "").strip().lower()
    if test_type in _SUITE_TYPE_ALIASES:
        return _SUITE_TYPE_ALIASES[test_type]
    return {test_type}


def make_empty_suite_target(test_type: str = "", suite_version: str = "", suite_path: str = "", suite_root: str = "", suite_name: str = "", test_name: str = "", class_names: list[str] | None = None, match_notes: list[str] | None = None) -> dict[str, Any]:
    return {
        "test_type": test_type, "suite_version": suite_version, "suite_path": suite_path,
        "suite_root": suite_root, "suite_name": suite_name, "suite_candidates": [],
        "artifact": None, "artifact_confidence": 0, "artifact_candidates": [],
        "source_guess": _build_apk_source_path_guess(test_name, class_names),
        "match_notes": match_notes or [],
    }


def _extract_suite_artifact_terms(source_path: str = "", module: str = "", test_name: str = "", class_names: list[str] | None = None) -> list[str]:
    terms: list[str] = []
    def add(term: str):
        term = (term or "").strip()
        if term and term not in terms:
            terms.append(term)
    add(module)
    add(test_name)
    for class_name in (class_names or [])[:3]:
        add(class_name)
    normalized_source = (source_path or "").replace("\\", "/").strip("/")
    if not normalized_source:
        return terms
    source_parts = [part for part in normalized_source.split("/") if part]
    if not source_parts:
        return terms
    file_name = source_parts[-1]
    file_stem = os.path.splitext(file_name)[0]
    add(file_name)
    add(file_stem)
    package_parts = source_parts[:-1]
    if len(package_parts) >= 2:
        tail_parts = package_parts[-3:]
        for size in range(2, len(tail_parts) + 1):
            suffix = tail_parts[-size:]
            add("".join(suffix))
            add("".join(reversed(suffix)))
    return terms


def resolve_suite_diagnosis_target(config: dict[str, Any], *, test_type: str = "", suite_version: str = "", module: str = "", test_name: str = "", class_names: list[str] | None = None, suite_path: str = "", source_path: str = "") -> dict[str, Any]:
    available_suites = get_available_test_suites(config)
    class_names = [c for c in (class_names or []) if c]
    source_path = (source_path or "").strip() or _build_apk_source_path_guess(test_name, class_names).get("source_path", "")
    search_terms = _extract_suite_artifact_terms(source_path, module, test_name, class_names)
    normalized_type = (test_type or "").strip().lower()
    normalized_version = (suite_version or "").strip()
    normalized_suite_path = (suite_path or "").strip()
    canonical_types = _canonical_suite_types(normalized_type) if normalized_type else set()

    def suite_matches(suite: dict[str, Any]) -> bool:
        if normalized_suite_path and suite.get("tools_path") != normalized_suite_path:
            return False
        suite_type = (suite.get("test_type") or "").strip().lower()
        if normalized_type and suite_type not in canonical_types:
            return False
        return not (normalized_version and _score_suite_version_match(suite.get("version", ""), normalized_version) <= 0 and normalized_suite_path)

    filtered_suites = [suite for suite in available_suites if suite_matches(suite)]
    if not filtered_suites:
        filtered_suites = available_suites[:20]

    suite_ranked = []
    for suite in filtered_suites:
        score = 0
        reasons = []
        suite_type = (suite.get("test_type") or "").strip().lower()
        version_score = _score_suite_version_match(suite.get("version", ""), normalized_version)
        if normalized_type and suite_type == normalized_type:
            score += 60
            reasons.append(f"type:{normalized_type}")
        elif normalized_type and suite_type in canonical_types:
            score += 50
            reasons.append(f"type:{suite_type}")
        if version_score:
            score += version_score
            reasons.append(f"version:{suite.get('version', '')}")
        suite_text = " ".join([suite.get("full_path", ""), suite.get("tools_path", ""), suite.get("binary", "")])
        if module and _normalize_suite_match_text(module) in _normalize_suite_match_text(suite_text):
            score += 45
            reasons.append(f"module:{module}")
        if normalized_suite_path and suite.get("tools_path") == normalized_suite_path:
            score += 100
            reasons.append("suite_path")
        suite_ranked.append((score, reasons, suite))

    suite_ranked.sort(key=lambda item: (item[0], item[2].get("version", ""), item[2].get("full_path", "")), reverse=True)
    best_suite = suite_ranked[0][2] if suite_ranked else None
    best_tools_path = best_suite.get("tools_path", "") if best_suite else ""
    best_suite_root = ""
    if best_tools_path:
        best_suite_root = best_tools_path[:-len("/tools")] if best_tools_path.endswith("/tools") else best_tools_path

    target = make_empty_suite_target(
        test_type=normalized_type, suite_version=normalized_version,
        suite_path=best_tools_path, suite_root=best_suite_root,
        suite_name=(best_suite.get("version") or best_suite.get("binary") or best_tools_path) if best_suite else "",
        test_name=test_name, class_names=class_names,
    )
    target["suite_candidates"] = [item[2] for item in suite_ranked[:5]]
    if not best_suite:
        target["match_notes"].append("No matching test suite found")
        return target

    target["match_notes"].append(f"Selected suite: {best_tools_path}")
    if best_suite.get("version"):
        target["match_notes"].append(f"Suite version: {best_suite.get('version')}")

    preferred_artifact_candidates = []
    is_local = is_config_host_local(config)
    ssh = None if is_local else runtime.ssh_manager.get_connection(config)
    try:
        if module:
            if is_local:
                preferred_artifact_candidates = _collect_preferred_suite_artifact_candidates_local(best_suite_root, module)
            elif ssh:
                preferred_artifact_candidates = _collect_preferred_suite_artifact_candidates_remote(ssh, best_suite_root, module)
        if is_local:
            artifact_candidates = _collect_suite_artifact_candidates_local(best_suite_root)
        elif ssh:
            artifact_candidates = _collect_suite_artifact_candidates_remote(ssh, best_suite_root)
        else:
            raise RuntimeError("SSH connection failed")
    except Exception as e:
        logger.warning(f"[TestSuites] Artifact search failed: {e}")
        target["match_notes"].append(f"Artifact search failed: {e}")
        artifact_candidates = []
        preferred_artifact_candidates = []
    finally:
        if ssh:
            runtime.ssh_manager.return_connection(ssh)

    if preferred_artifact_candidates:
        preferred_map = {item.get("full_path", ""): item for item in preferred_artifact_candidates if item.get("full_path")}
        merged_candidates = list(preferred_artifact_candidates)
        for candidate in artifact_candidates:
            full_path = candidate.get("full_path", "")
            if full_path and full_path in preferred_map:
                continue
            merged_candidates.append(candidate)
        artifact_candidates = merged_candidates

    ranked_candidates = [
        _score_suite_artifact_candidate(candidate, search_terms, suite_version=best_suite.get("version", ""), test_type=best_suite.get("test_type", ""), module=module, source_path=source_path)
        for candidate in artifact_candidates
    ]
    ranked_candidates.sort(key=lambda item: (item.get("score", 0), item.get("size", 0)), reverse=True)
    target["artifact_candidates"] = ranked_candidates[:10]
    if ranked_candidates:
        target["artifact_confidence"] = int(ranked_candidates[0].get("score", 0))
        if target["artifact_confidence"] >= 50:
            target["artifact"] = ranked_candidates[0]
            target["match_notes"].append(f"Artifact: {ranked_candidates[0].get('path', '')}")
        else:
            target["match_notes"].append("No high-confidence APK/JAR artifact found, please locate manually in suite directory")
    else:
        target["match_notes"].append("No APK/JAR artifact candidates found")

    return target
