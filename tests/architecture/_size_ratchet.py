"""Shared size-ratchet plumbing for the architecture size gates.

「历史债务只减不增」此前只是约定：ceiling 和代码写在同一个测试文件里，
开发者同时上调 ``MIGRATION_LINE_LIMITS`` 就能绕过门禁（评审 P2）。现在的
棘轮语义：

* ceilings 独立持久化在 ``baselines/*.json``（本目录随仓库提交）；
* 门禁把当前 baseline 与 **上一个 commit 的 baseline** 对比——任何
  ceiling 上调或新增登记都必须同时提供本文件 ``waivers`` 里带
  ``reason`` + ``expires`` 的豁免（过期即失效，必须重新评估）；
* 收紧（下调 / 删除条目 / 删除 waiver）永远放行；
* 无法读到 HEAD 版本（新仓库 / baseline 首次提交）时跳过对比：
  第一笔提交由评审人把关，CI 从第二笔起全量生效。

应用代码运行时（含插件生成树、发布脚本）只读 ``ceilings``；``waivers``
仅由 ratchet 门禁消费。
"""
from __future__ import annotations

import json
import subprocess
from datetime import date, datetime
from pathlib import Path


BASELINES_DIR = Path(__file__).resolve().parent / "baselines"


def load_baseline(name: str) -> dict:
    """读取 baselines/<name>；结构异常直接抛错（门禁配置错误必须翻红）。"""
    path = BASELINES_DIR / name
    data = json.loads(path.read_text(encoding="utf-8"))
    ceilings = data.get("ceilings")
    if not isinstance(ceilings, dict):
        raise ValueError(f"{path}: missing 'ceilings' mapping")
    waivers = data.get("waivers") or {}
    if not isinstance(waivers, dict):
        raise ValueError(f"{path}: 'waivers' must be a mapping")
    return data


def _waiver_valid(waiver: dict, *, today: date) -> str:
    """返回空串表示豁免有效；否则返回不通过的原因。"""
    reason = str(waiver.get("reason") or "").strip()
    if not reason:
        return "missing reason"
    expires_raw = str(waiver.get("expires") or "").strip()
    try:
        expires = date.fromisoformat(expires_raw[:10])
    except ValueError:
        return f"unparseable expires {expires_raw!r}"
    if expires < today:
        return f"expired {expires.isoformat()}"
    return ""


def assert_no_ceiling_raises(name: str, *, today: date | None = None) -> None:
    """Ratchet 门禁：当前 baseline 相对 HEAD 不允许上调或新增登记。

    ``git show HEAD:tests/architecture/baselines/<name>`` 不可读（新仓库、
    baseline 尚未提交）时跳过——没有"上一个承诺"就没有"上调"可言，
    首次登记由评审把关，CI 从 baseline 入库后的第二笔提交起全量生效。
    """
    baseline_name = f"baselines/{name}"
    current = load_baseline(name)
    today = today or date.today()

    proc = subprocess.run(
        ["git", "show", f"HEAD:tests/architecture/{baseline_name}"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return  # baseline 首次提交 / 无 git 历史门禁无法对比
    try:
        head = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return  # HEAD 里的 baseline 损坏：按首次提交处理，由评审把关

    head_ceilings = head.get("ceilings") or {}
    current_ceilings = current["ceilings"]
    current_waivers = current.get("waivers") or {}

    violations: list[str] = []
    for key, value in sorted(current_ceilings.items()):
        head_value = head_ceilings.get(key)
        if head_value is not None and value <= head_value:
            continue  # 持平或收紧
        if head_value is None and key not in head_ceilings and value <= 0:
            continue  # 防御分支：非正 ceiling 无意义，不靠豁免
        waiver = current_waivers.get(key)
        if not isinstance(waiver, dict):
            detail = (
                f"raised {head_value} -> {value}" if head_value is not None
                else f"new registration at {value}"
            )
            violations.append(f"{key}: {detail} (no waiver)")
            continue
        problem = _waiver_valid(waiver, today=today)
        if problem:
            violations.append(f"{key}: {problem} (waiver invalid)")
    if violations:
        raise AssertionError(
            "size-ratchet violation: ceilings in "
            f"tests/architecture/{baseline_name} must only shrink. "
            "Raise/update requires a waiver entry (reason + future expiry) "
            f"in the same file. Violations: {violations}"
        )


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")
