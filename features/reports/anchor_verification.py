"""Wiki 命中 → codesearch 自动溯源（ADR 0014 串联验证管线）。

报告诊断阶段 ②-④ 串联：

    ② Android Internals Wiki 命中（机制 + source_anchors）
    ③ 用 anchor 的 path/symbol 去本地 codesearch（OpenGrok）查询
    ④ 命中如实标注 —— 只证明「当前选中的 OpenGrok project 里存在同
       路径文件」，绝不标注为 ``verified``。

证据语义（严格控制“验证”一词的强度）：

    path_present / ``path_matched``
        本地源码树存在对应路径。Android 14/15/16/17 大量文件路径长期
        不变而内部逻辑早已改变，路径命中不能证明 Wiki 描述的机制在该
        revision 成立，更不能证明任何根因结论。
    ``path_missing``
        codesearch 可用且应答，但目标路径不存在于该 project。
    ``unknown`` / ``path_present=None``
        codesearch 不可用/超时/崩溃，验证器无法给出结论。

只有 revision + symbol/content 真正匹配（未来 SourceAnchor 携带
symbol/line/commit 并逐项复核）才能升级到 ``revision_verified`` /
``claim_verified`` 等级；本模块刻意不产出这些等级。

设计边界（ADR 0014）：

- 输入只有 ``system_background_results`` 里已结构化的 ``source_anchors``
  （`features/knowledge/external/ranking.parse_aosp_url` 投影），Wiki 正文
  永远是 background，溯源只追加信息，不改变任何 hit 的 evidence 语义，
  也不直接生成 root cause。
- 溯源器失败（codesearch 不可用/超时）静默降级为 ``unknown``，绝不影响
  诊断主流程。
- 每个诊断请求最多溯源 ``MAX_VERIFIED_ANCHORS`` 个 anchor：codesearch
  是子进程调用，防止长尾膨胀。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from .archive import codesearch_script_location, run_codesearch_process


logger = logging.getLogger(__name__)

#: 单次诊断最多溯源的 anchor 数（codesearch 子进程有成本）。
MAX_VERIFIED_ANCHORS = 4
#: anchor repo/path 风暴防护：超长输入直接跳过（untrusted 数据）。
_MAX_ANCHOR_FIELD_LEN = 300

#: 证据等级：仅此三个取值。更高等级（revision_verified / claim_verified）
#: 需要 SourceAnchor 携带 symbol/line/commit 并逐项复核后才允许引入。
EVIDENCE_PATH_MATCHED = "path_matched"
EVIDENCE_PATH_MISSING = "path_missing"
EVIDENCE_UNKNOWN = "unknown"

#: 锚点溯源的总时间预算（秒）：所有 anchor 的 codesearch 调用共享，
#: 每笔调用受剩余预算约束，不是每项固定 30s。
CODESEARCH_ANCHOR_TIMEOUT_SECONDS = 30.0


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
    timeout: float = CODESEARCH_ANCHOR_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """用本地 codesearch 溯源单个 anchor 的路径存在性（三态）。

    ``timeout`` 是本次调用的剩余预算；调用方用总预算扣减后传入。
    """
    term = _anchor_search_term(anchor)
    if not term or len(term) > _MAX_ANCHOR_FIELD_LEN or "/" not in str(anchor.get("path") or ""):
        # 没有路径级 anchor（如 url-only）无从溯源。
        return {"path_present": None, "evidence_level": EVIDENCE_UNKNOWN, "verification": None}
    from .archive import get_opengrok_project_for_android_version

    codesearch_script, codesearch_dir = codesearch_script_location()
    opengrok_config: dict[str, Any] = {}
    try:
        from foundation.config import ConfigManager

        opengrok_config = ConfigManager().load_config().get("opengrok", {}) or {}
    except Exception:  # 配置缺失时退回默认项目，不阻塞溯源
        pass
    version_project = get_opengrok_project_for_android_version(
        android_version, opengrok_config
    )
    project_args = ["--project", version_project] if version_project else []
    # 用完整相对路径检索，并在结果侧再次做路径后缀校验。只按文件名会让
    # Android.bp / Utils.java 等常见名称把完全无关的文件误标为路径命中。
    result = run_codesearch_process(
        [
            "python3", codesearch_script, "search",
            "--keywords", term, "--search-field", "path",
            "--limit", "10", *project_args,
        ],
        codesearch_dir,
        timeout=timeout,
    )
    if result is None:
        return {"path_present": None, "evidence_level": EVIDENCE_UNKNOWN, "verification": None}
    if not result.stdout.strip():
        return {"path_present": False, "evidence_level": EVIDENCE_PATH_MISSING, "verification": None}
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
            return {
                "path_present": True,
                "evidence_level": EVIDENCE_PATH_MATCHED,
                "verification": verification,
            }
    return {"path_present": False, "evidence_level": EVIDENCE_PATH_MISSING, "verification": None}


def verify_background_anchors(
    background_results: list[dict[str, Any]],
    *,
    android_version: str = "",
) -> list[dict[str, Any]]:
    """批量溯源 background 命中的 source_anchors（独立失败降级）。

    返回溯源结论列表（每项含 ``hit_title``/``hit_id``/``anchor``/
    ``path_present``/``evidence_level``/``verification``），直接挂到诊断
    payload 的 ``anchor_verification_results``；任何单点失败只影响自己的
    条目。

    绑定契约：``hit_id`` 是召回层生成的结构化标识，消费方
    ``_attach_anchor_verifications`` 必须用它回挂，不得用 ``hit_title``
    ——两个知识条目同 title 时 title 回挂会交叉绑定。
    全部 anchor 的 codesearch 调用共享 ``CODESEARCH_ANCHOR_TIMEOUT_SECONDS``
    总时间预算，每笔调用只拿到剩余预算。
    """
    verifications: list[dict[str, Any]] = []
    budget = MAX_VERIFIED_ANCHORS
    # 时间预算按剩余额度分配：锚点数量上限 4，单笔上限 30s，总预算
    # 30s——单笔长尾吃满预算后，后续锚点直接记为 unknown，不再等待。
    time_budget = CODESEARCH_ANCHOR_TIMEOUT_SECONDS
    # 相同 anchor 只跑一次子进程，但结论必须复用到每个
    # hit；仅用 seen 跳过会让后续 hit 无法按 hit_id 回挂。
    outcome_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for hit in background_results or []:
        anchors = hit.get("source_anchors") if isinstance(hit, dict) else None
        if not isinstance(anchors, list):
            continue
        hit_id = str(hit.get("hit_id") or "")
        for anchor in anchors:
            if not isinstance(anchor, dict):
                continue
            key = (str(anchor.get("repo") or ""), str(anchor.get("path") or ""))
            if not key[1]:
                # 无 path 的 anchor（url-only）无从做文件级溯源：跳过，
                # 不产生 path_missing 的误导条目。
                continue
            outcome = outcome_cache.get(key)
            if outcome is None:
                if budget <= 0:
                    # 新 anchor 超出数量预算；继续遍历是为了让后面
                    # 可能出现的已缓存 anchor 仍能回挂到对应 hit。
                    continue
                budget -= 1
                if time_budget <= 0:
                    outcome = {
                        "path_present": None,
                        "evidence_level": EVIDENCE_UNKNOWN,
                        "verification": None,
                    }
                else:
                    started = time.monotonic()
                    try:
                        outcome = verify_anchor_in_codesearch(
                            anchor,
                            android_version=android_version,
                            timeout=time_budget,
                        )
                    except Exception as exc:
                        logger.warning(
                            "anchor verification failed for %s: %s", key[1], exc
                        )
                        outcome = {
                            "path_present": None,
                            "evidence_level": EVIDENCE_UNKNOWN,
                            "verification": None,
                        }
                    time_budget = max(
                        0.0, time_budget - (time.monotonic() - started)
                    )
                outcome_cache[key] = outcome
            verifications.append({
                "hit_title": str(hit.get("title") or ""),
                # 结构化回挂 key：召回层无 id 时留空，消费方据此跳过该
                # 条目而不是按 title 误绑定。
                "hit_id": hit_id,
                "anchor": {
                    "repo": str(anchor.get("repo") or ""),
                    "revision": str(anchor.get("revision") or ""),
                    "path": str(anchor.get("path") or ""),
                    "evidence_type": str(anchor.get("evidence_type") or ""),
                },
                # path_present: True=本地源码树存在同路径文件；
                # False=codesearch 可用但路径不存在；None=溯源器不可用
                # 或预算耗尽。
                # evidence_level 是 UI/报告应使用的唯一语义标签。
                "path_present": outcome["path_present"],
                "evidence_level": outcome["evidence_level"],
                "verification": outcome["verification"],
            })
    return verifications


__all__ = [
    "CODESEARCH_ANCHOR_TIMEOUT_SECONDS",
    "EVIDENCE_PATH_MATCHED",
    "EVIDENCE_PATH_MISSING",
    "EVIDENCE_UNKNOWN",
    "MAX_VERIFIED_ANCHORS",
    "verify_anchor_in_codesearch",
    "verify_background_anchors",
]
