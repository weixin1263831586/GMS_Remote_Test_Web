"""Failure Identity / Cluster：确定性失败身份层（全局审查：失败身份与关系分层）。

现有 ``analysis_similarity`` 回答的是"值不值得拿出来比较"（retrieval
score：同用例/同模块/关键词/Jaccard + AI 语义分）。全局审查指出的长期
问题是 retrieval 高分 ≠ 同一失败：**同一个 CTS testcase 完全可能
设备 A = overlay 错、设备 B = framework API 行为变化、设备 C = 环境
问题**，不能因为 testcase 相同就合并 Cluster。

本模块把两层明确分开：

1. **Identity（本模块，确定性）**：suite/module/testcase/assertion class +
   归一化错误签名 + Android API + 设备类 → 稳定指纹（sha256）。
   归一化会剥掉地址、十六进制、时间戳、计数等易变部分，同一根因的两次
   失败得到同一指纹。
2. **Relation（先确定性，后 AI）**：``relation_class`` 先给出纯确定性
   关系（SAME_FAILURE / SAME_TEST_DIFFERENT_CAUSE / SIMILAR_SYMPTOM /
   UNRELATED）。AIHOT 风格的 LLM relation judge（SAME_ROOT_CAUSE、
   REGRESSION_OF、低置信合并的二次确认）是后续 AI 层，**不得**越过
   指纹层直接合并。

纯函数、无 IO：identity 在诊断/入库时计算一次即可长期聚合，``build_
failure_clusters`` 按 exact fingerprint 分组——只有 exact match 才进同一
Cluster，lexical/embedding 召回结果只能作为"候选"交给人或 relation judge。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


#: 归一化时整体剔除的噪声片段（地址/ID/时间/计数）。
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_ADDR = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_NUM = re.compile(r"\b\d+\b")
_SEP = re.compile(r"[^A-Za-z0-9_.]+")
_COLLAPSE_DOT = re.compile(r"\.{2,}")

#: assertion class：从异常行提取的"异常类型"，剥掉消息文本。
_ASSERTION_LINE = re.compile(
    r"\b((?:junit\.framework|org\.junit|java\.lang\.(?:Runtime|IllegalArgument|"
    r"IllegalState|NullPointer|AssertionError)|androidx?\.test)\.[A-Za-z0-9_.]*"
    r"(?:Exception|Error)|AssertionError)\b"
)

VALID_RELATIONS = (
    "SAME_FAILURE",
    "SAME_TEST_DIFFERENT_CAUSE",
    "SIMILAR_SYMPTOM",
    "UNRELATED",
)


def normalize_error_signature(reason: str) -> str:
    """归一化错误文本：剥掉地址/数字/十六进制/多余分隔符。

    同一根因、不同现场（时间戳、内存地址、计数、主机名）归一后得到同一
    签名；不同根因（异常类型/调用点不同）不会被归一掉。
    """
    text = str(reason or "")
    text = _HEX.sub(" ", text)
    text = _ADDR.sub(" ", text)
    text = _NUM.sub(" ", text)
    text = _SEP.sub(" ", text)
    text = _COLLAPSE_DOT.sub(" ", text)
    return " ".join(text.lower().split())[:400]


def assertion_class(reason: str) -> str:
    """提取 assertion class（异常类型 FQN）；无匹配返回空串。"""
    match = _ASSERTION_LINE.search(str(reason or ""))
    return match.group(1) if match else ""


def _first_failure_value(failures: list[dict[str, Any]], *keys: str) -> str:
    for failure in failures[:5]:
        for key in keys:
            value = str(failure.get(key) or "").strip()
            if value:
                return value
    return ""


def failure_identity(
    failures: list[dict[str, Any]],
    *,
    suite: str = "",
    android_version: str = "",
    device_class: str = "",
) -> dict[str, Any]:
    """从失败列表构造确定性身份。

    ``failures`` 行形状与 ``analysis_similarity`` 一致：
    ``{"module": ..., "name": ..., "reason": ...}``。

    返回::

        {
          "suite", "module", "testcase", "assertion_class",
          "error_signature", "android_version", "device_class",
          "fingerprint",   # sha256(canonical fields)
        }
    """
    failure_list = [f for f in (failures or []) if isinstance(f, dict)]
    module = _first_failure_value(failure_list, "module")
    testcase = _first_failure_value(failure_list, "name", "testcase")
    reason = _first_failure_value(failure_list, "reason", "message")
    aclass = assertion_class(reason)
    signature = normalize_error_signature(reason)
    identity = {
        "suite": str(suite or "").strip(),
        "module": module,
        "testcase": testcase,
        "assertion_class": aclass,
        "error_signature": signature,
        "android_version": str(android_version or "").strip(),
        "device_class": str(device_class or "").strip(),
    }
    identity["fingerprint"] = _fingerprint(identity)
    return identity


def _fingerprint(identity: dict[str, Any]) -> str:
    canonical = "\x00".join(
        str(identity.get(key) or "")
        for key in (
            "suite", "module", "testcase", "assertion_class",
            "error_signature", "android_version", "device_class",
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def relation_class(first: dict[str, Any], second: dict[str, Any]) -> str:
    """确定性关系判定（exact fingerprint 才是 SAME_FAILURE）。

    - ``SAME_FAILURE``：指纹完全一致（同用例 + 同签名 + 同上下文）。
    - ``SAME_TEST_DIFFERENT_CAUSE``：同 module+testcase，签名/异常类不同
      ——钉子案例：同名用例可能各自有不同根因，禁止合 Cluster。
    - ``SIMILAR_SYMPTOM``：签名一致但用例不同（同一机制在不同用例显形），
      只能作为候选，合并需 relation judge 二次确认。
    - ``UNRELATED``：以上皆不满足。
    """
    if first.get("fingerprint") == second.get("fingerprint"):
        return "SAME_FAILURE"
    same_test = (
        str(first.get("module") or "") == str(second.get("module") or "")
        and str(first.get("testcase") or "") == str(second.get("testcase") or "")
        and bool(first.get("testcase"))
    )
    if same_test:
        return "SAME_TEST_DIFFERENT_CAUSE"
    same_signature = (
        bool(first.get("error_signature"))
        and first.get("error_signature") == second.get("error_signature")
    )
    if same_signature:
        return "SIMILAR_SYMPTOM"
    return "UNRELATED"


def build_failure_clusters(
    identities: list[dict[str, Any]],
    *,
    subject_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """按 exact fingerprint 聚合成 Cluster。

    ``identities`` 与 ``subject_ids``（issue id / run id 等主体）一一对应；
    未提供主体时仅返回成员计数。SIMILAR_SYMPTOM 级别的关联**不进** Cluster
    ——那是 retrieval 的职责，合并前必须过 relation judge。
    """
    groups: dict[str, dict[str, Any]] = {}
    for index, identity in enumerate(identities):
        fingerprint = str(identity.get("fingerprint") or "")
        if not fingerprint:
            continue
        group = groups.setdefault(
            fingerprint,
            {"fingerprint": fingerprint, "members": [], "identity": {
                key: identity.get(key)
                for key in (
                    "suite", "module", "testcase", "assertion_class",
                    "android_version", "device_class",
                )
            }},
        )
        subject = subject_ids[index] if subject_ids and index < len(subject_ids) else index
        group["members"].append(subject)
    clusters = sorted(groups.values(), key=lambda g: -len(g["members"]))
    return [
        {**group, "size": len(group["members"]), "members": group["members"]}
        for group in clusters
    ]
