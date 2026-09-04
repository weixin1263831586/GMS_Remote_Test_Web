"""Tradefed 模块名校验与失败原因识别（fail-fast）。

工单 gms-rt-improvement-tickets P0-1 / P0-2 / P2-5：
- P0-2：接单后、执行前校验模块名在 ``testcases/`` 下存在（精确或大小写
  不敏感匹配），不匹配则拒绝任务并附模糊候选（避免无效任务占用设备租约
  与队列，走完 tradefed 初始化约 4 秒才失败）。
- P0-1/P2-5：tradefed ``No matched tradefed modules`` 时结构化透传错误，
  而不是让平台把根因掩盖成 ``process exited with N``。
"""

from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path


logger = logging.getLogger("gms-worker")

# tradefed stdout 中 0 模块匹配的特征行。
_NO_MATCHED_MODULES_RE = re.compile(
    r"No matched tradefed modules from the given modules:\s*\[(.+?)\]"
)


def list_suite_modules(suite_tools_path: str | Path) -> list[str]:
    """列出套件的可用模块名（testcases/ 下的模块目录与 *.config 文件）。"""
    testcases = Path(suite_tools_path).parent / "testcases"
    if not testcases.is_dir():
        return []
    modules: set[str] = set()
    try:
        for entry in testcases.iterdir():
            if entry.is_dir():
                modules.add(entry.name)
            elif entry.name.endswith(".config"):
                modules.add(entry.name[: -len(".config")])
    except OSError:
        return []
    return sorted(modules)


def fuzzy_match_candidates(
    module: str,
    modules: list[str],
    max_candidates: int = 8,
) -> list[str]:
    """模糊候选：子串匹配优先，其次编辑距离最近邻（P1-3 也复用）。"""
    lowered = module.lower()
    substring = [name for name in modules if lowered in name.lower()]
    if substring:
        return substring[:max_candidates]
    return difflib.get_close_matches(module, modules, n=max_candidates, cutoff=0.4)


def validate_module_name(
    module: str,
    suite_tools_path: str | Path,
    *,
    max_candidates: int = 8,
) -> str | None:
    """校验模块名；不匹配时返回含候选的错误信息，匹配返回 None。

    匹配规则：精确 → 大小写不敏感。套件无 testcases 目录（布局不同或
    未解压完整）时跳过校验，让 tradefed 自己给出结论，避免误杀合法任务。
    """
    module = str(module or "").strip()
    if not module:
        return None
    testcases = Path(suite_tools_path).parent / "testcases"
    if not testcases.is_dir():
        logger.debug(
            "testcases dir not found for %s; skip module validation",
            suite_tools_path,
        )
        return None
    modules = list_suite_modules(testcases)
    if module in modules:
        return None
    lowered = {name.lower(): name for name in modules}
    case_match = lowered.get(module.lower())
    if case_match:
        return (
            f"module not found in suite: {module}"
            f"（大小写不敏感匹配到 '{case_match}'，请使用正确大小写）"
        )
    candidates = fuzzy_match_candidates(module, modules, max_candidates)
    hint = ", ".join(candidates) if candidates else "(无相近模块)"
    return f"module not found in suite: {module}. 相近候选模块: {hint}"


def detect_no_matched_modules(stdout: str) -> str | None:
    """从 tradefed stdout 识别 0 模块匹配错误，返回结构化错误信息。

    未命中返回 None。工单 P0-1/P2-5：识别 ``No matched tradefed modules``
    并结构化透传，让任务失败原因直达 "module not found in suite"。
    """
    match = _NO_MATCHED_MODULES_RE.search(str(stdout or ""))
    if not match:
        return None
    requested = match.group(1).strip()
    return f"module not found in suite: tradefed matched 0 modules ({requested})"
