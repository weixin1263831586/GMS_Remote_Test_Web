"""Wiki 命中 → codesearch 自动验证（ADR 0014 串联验证管线）。

报告诊断阶段 ②-④ 串联：

    ② Android Internals Wiki 命中（机制 + source_anchors）
    ③ 用 anchor 的 path/symbol 去本地 codesearch（OpenGrok）验证
    ④ 命中即标注 ``verified=True`` —— Wiki 结论在当前 SDK 源码树中
       找到了对应实体；未命中不降级删除，仅如实标注。

设计边界（ADR 0014）：

- 输入只有 ``system_background_results`` 里已结构化的 ``source_anchors``
  （`features/knowledge/external/ranking.parse_aosp_url` 投影），Wiki 正文
  永远是 background，验证只追加溯源信息，不改变任何 hit 的 evidence
  语义，也不直接生成 root cause。
- 验证器失败（codesearch 不可用/超时）静默降级为 ``verified=None``
  （未知），绝不影响诊断主流程。
- 每个诊断请求最多验证 ``MAX_VERIFIED_ANCHORS`` 个 anchor：codesearch
  是子进程调用，防止长尾膨胀。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .archive import codesearch_script_location, run_codesearch_process


logger = logging.getLogger(__name__)

#: 单次诊断最多验证的 anchor 数（codesearch 子进程有成本）。
MAX_VERIFIED_ANCHORS = 4
#: anchor repo/path 风暴防护：超长输入直接跳过（untrusted 数据）。
_MAX_ANCHOR_FIELD_LEN = 300


def _normalize_path(value: Any) -> str:
    return "/".join(
        part for part in str(value or "").replace("\\", "/").split("/") if part
    )


def _anchor_search_term(anchor: dict[str, Any]) -> str:
    """从 anchor 提取 codesearch 检索词：使用完整相对路径避免同名误报。"""
    return _normalize_path(anchor.get("path"))


def _path_matches_anchor(found_path: str, anchor: dict[str, Any]) -> bool:
    """要求命中路径与 anchor 路径（或 repo+path）后缀一致。"""
    found = _normalize_path(found_path).casefold()
    anchor_path = _normalize_path(anchor.get("path")).casefold()
    repo = _normalize_path(anchor.get("repo")).casefold()
    repo_without_platform = repo.removeprefix("platform/")
    candidates = {anchor_path}
    if repo_without_platform and anchor_path:
        candidates.add(f"{repo_without_platform}/{anchor_path}")
    return any(
        candidate and (found == candidate or found.endswith(f"/{candidate}"))
        for candidate in candidates
    )


def verify_anchor_in_codesearch(
    anchor: dict[str, Any],
    *,
    android_version: str = "",
) -> dict[str, Any]:
    """用本地 codesearch 验证单个 anchor，保留 true/false/unknown 三态。"""
    term = _anchor_search_term(anchor)
    if not term or len(term) > _MAX_ANCHOR_FIELD_LEN or "/" not in str(anchor.get("path") or ""):
        # 没有路径级 anchor（如 url-only）无从验证。
        return {"verified": None, "verification": None}
    from .archive import get_opengrok_project_for_android_version

    codesearch_script, codesearch_dir = codesearch_script_location()
    opengrok_config: dict[str, Any] = {}
    try:
        from foundation.config import ConfigManager

        opengrok_config = ConfigManager().load_config().get("opengrok", {}) or {}
    except Exception:  # 配置缺失时退回默认项目，不阻塞验证
        pass
    version_project = get_opengrok_project_for_android_version(
        android_version, opengrok_config
    )
    project_args = ["--project", version_project] if version_project else []
    # 用完整相对路径检索，并在结果侧再次做路径后缀校验。只按文件名会让
    # Android.bp / Utils.java 等常见名称把完全无关的文件误标为已验证。
    result = run_codesearch_process(
        [
            "python3", codesearch_script, "search",
            "--keywords", term, "--search-field", "path",
            "--limit", "10", *project_args,
        ],
        codesearch_dir,
    )
    if result is None:
        return {"verified": None, "verification": None}
    if not result.stdout.strip():
        return {"verified": False, "verification": None}
    for line in result.stdout.splitlines():
        line = line.strip()
        match = re.match(r"^\[[^\]]+\]\s+(.+)$", line)
        if match is None:
            continue
        found_path = match.group(1).strip()
        if not found_path:
            continue
        if _path_matches_anchor(found_path, anchor):
            verification = {
                "repo": str(anchor.get("repo") or ""),
                "revision": str(anchor.get("revision") or ""),
                "wiki_path": str(anchor.get("path") or ""),
                "local_path": found_path,
                "local_project": version_project,
                "evidence_type": str(anchor.get("evidence_type") or "aosp"),
            }
            return {"verified": True, "verification": verification}
    return {"verified": False, "verification": None}


def verify_background_anchors(
    background_results: list[dict[str, Any]],
    *,
    android_version: str = "",
) -> list[dict[str, Any]]:
    """批量验证 background 命中的 source_anchors（独立失败降级）。

    返回验证结论列表（每项含 ``hit_title``/``anchor``/``verified``/
    ``verification``），直接挂到诊断 payload 的
    ``anchor_verification_results``；任何单点失败只影响自己的条目。
    """
    verifications: list[dict[str, Any]] = []
    budget = MAX_VERIFIED_ANCHORS
    seen: set[tuple[str, str]] = set()
    for hit in background_results or []:
        if budget <= 0:
            break
        anchors = hit.get("source_anchors") if isinstance(hit, dict) else None
        if not isinstance(anchors, list):
            continue
        for anchor in anchors:
            if budget <= 0:
                break
            if not isinstance(anchor, dict):
                continue
            key = (str(anchor.get("repo") or ""), str(anchor.get("path") or ""))
            if key in seen or not key[1]:
                # 无 path 的 anchor（url-only）无从做文件级验证：跳过，
                # 不产生 verified=False 的误导条目。
                continue
            seen.add(key)
            budget -= 1
            try:
                outcome = verify_anchor_in_codesearch(
                    anchor, android_version=android_version
                )
            except Exception as exc:
                logger.warning(
                    "anchor verification failed for %s: %s", key[1], exc
                )
                outcome = {"verified": None, "verification": None}
            verifications.append({
                "hit_title": str(hit.get("title") or ""),
                "anchor": {
                    "repo": str(anchor.get("repo") or ""),
                    "revision": str(anchor.get("revision") or ""),
                    "path": str(anchor.get("path") or ""),
                    "evidence_type": str(anchor.get("evidence_type") or ""),
                },
                # verified: True=本地源码树找到对应文件实体；
                # False=codesearch 可用但未找到；None=验证器不可用。
                "verified": outcome["verified"],
                "verification": outcome["verification"],
            })
    return verifications
