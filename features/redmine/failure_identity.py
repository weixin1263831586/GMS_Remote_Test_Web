"""Failure Identity / Cluster：确定性失败身份层（全局审查：失败身份与关系分层）。

现有 ``analysis_similarity`` 回答的是"值不值得拿出来比较"（retrieval
score：同用例/同模块/关键词/Jaccard + AI 语义分）。全局审查指出的长期
问题是 retrieval 高分 ≠ 同一失败：**同一个 CTS testcase 完全可能
设备 A = overlay 错、设备 B = framework API 行为变化、设备 C = 环境
问题**，不能因为 testcase 相同就合并 Cluster。

本模块把两层明确分开：

1. **Identity（本模块，确定性）**：suite/module/testcase/assertion class +
   归一化错误签名 + Android API + 设备类 → 稳定指纹（sha256）。
   归一化只剥**易变噪声**（内存地址、UUID、时间戳、PID/TID、时长、
   计数器、Redmine/Gerrit 引用号），同一根因的两次失败得到同一指纹；
   **领域数字必须保留**（expected/actual、size、API level、errno、
   status code、版本号）——``expected <3> but was <5>`` 与
   ``expected <9> but was <2>`` 是不同失败，``vbkey size 0`` 与
   ``vbkey size 32``、``API 36`` 与 ``API 37`` 同理：数字本身常常就是
   最重要的 failure signature（全局审查 4.3）。
2. **Relation（先确定性，后 AI）**：``relation_class`` 先给出纯确定性
   关系（SAME_FAILURE / SAME_TEST_DIFFERENT_CAUSE / SIMILAR_SYMPTOM /
   UNRELATED）。AIHOT 风格的 LLM relation judge（SAME_ROOT_CAUSE、
   REGRESSION_OF、低置信合并的二次确认）是后续 AI 层，**不得**越过
   指纹层直接合并。

设备语义（全局审查 4.2）：``device_class`` 必须是 SoC / product /
形态这类**设备类**；``device_serial`` 是观察元数据，随身份落库但
**不进指纹**——同一失败在两台同型号设备上必须得到同一指纹，否则
跨设备聚合被 serial 阻断。

纯函数、无 IO：identity 在诊断/入库时计算一次即可长期聚合，``build_
failure_clusters`` 按 exact fingerprint 分组——只有 exact match 才进同一
Cluster，lexical/embedding 召回结果只能作为"候选"交给人或 relation judge。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


#: 易变噪声片段（地址/ID/时间/进程号/时长/计数器）。只剥这些；
#: 未匹配任何模式的数字原样保留。
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
#: 无 0x 前缀的长十六进制块（地址/meminfo 列/hash）。
_HEX_BLOB = re.compile(r"\b[a-fA-F0-9]{16,}\b")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_DATE_TIME = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_TIME_OF_DAY = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\b")
#: Redmine issue / Gerrit change 引用号（纯数字；不能用 [0-9A-Za-z]，
#: 否则会误伤 Class#method 形态的用例名，如 BarTest#testBaz）。
_REF_ID = re.compile(r"#\d{3,}\b")
_PID_TID = re.compile(r"\b(?:pid|tid|uid)\b[=:：]?\s*\d+", re.IGNORECASE)
_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|us|μs|ns)\b")
_COUNTER = re.compile(
    r"\b(?:count|attempt|attempts|retry|retries|elapsed|took|iteration|"
    r"seq|line(?:no)?)\b[=:：]?\s*\d+",
    re.IGNORECASE,
)
#: 裸的 epoch 秒/毫秒（10 或 13 位、1 开头）。
_EPOCH = re.compile(r"\b1\d{9}\b|\b1\d{12}\b")
_SEP = re.compile(r"[^A-Za-z0-9_.]+")
_COLLAPSE_DOT = re.compile(r"\.{2,}")

#: assertion class：从异常行提取的"异常类型"，剥掉消息文本。
_ASSERTION_LINE = re.compile(
    r"\b((?:junit\.framework|org\.junit|java\.lang\.(?:Runtime|IllegalArgument|"
    r"IllegalState|NullPointer|AssertionError)|androidx?\.test)\.[A-Za-z0-9_.]*"
    r"(?:Exception|Error)|AssertionError)\b"
)

#: xTS 模块名：驼峰 + TestCases/Tests/TestClass 后缀（CtsCameraTestCases）。
_MODULE_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]*Test(?:Cases?|Class|s)?)\b"
)
#: Class#method（android.foo.BarTest#testBaz）。
_TESTCASE_HASH_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.$]*)#([A-Za-z_][A-Za-z0-9_]*)\b")
#: 点分方法名（android.foo.BarTest.testBaz）：末段以 test 开头才算用例。
_TESTCASE_DOTTED_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_.$]*\.[A-Za-z_][A-Za-z0-9_]*)\b"
)
_SUITE_TOKENS = ("cts-verifier", "cts-v", "cts", "gts", "vts", "sts", "mts")

VALID_RELATIONS = (
    "SAME_FAILURE",
    "SAME_TEST_DIFFERENT_CAUSE",
    "SIMILAR_SYMPTOM",
    "UNRELATED",
)


def normalize_error_signature(reason: str) -> str:
    """归一化错误文本：只剥易变噪声，保留领域数字。

    同一根因、不同现场（时间戳、内存地址、PID、UUID、计数、主机名、
    引用号）归一后得到同一签名；不同根因（异常类型不同、expected/actual
    不同、size/API level 不同）不会被归一掉。
    """
    text = str(reason or "")
    for pattern in (
        _UUID, _HEX, _HEX_BLOB, _IPV4, _DATE_TIME, _TIME_OF_DAY,
        _REF_ID, _PID_TID, _DURATION, _COUNTER, _EPOCH,
    ):
        text = pattern.sub(" ", text)
    text = _SEP.sub(" ", text)
    text = _COLLAPSE_DOT.sub(" ", text)
    return " ".join(text.lower().split())[:400]


def assertion_class(reason: str) -> str:
    """提取 assertion class（异常类型 FQN）；无匹配返回空串。"""
    match = _ASSERTION_LINE.search(str(reason or ""))
    return match.group(1) if match else ""


def _camel_humped(name: str) -> bool:
    """驼峰判定：首字符之后还有大写字母（排除英文普通词 Tests）。"""
    return any(ch.isupper() for ch in name[1:])


def extract_test_identity(text: str) -> dict[str, str]:
    """从自由文本（Redmine subject / 报告标题）提取 suite/module/testcase。

    只识别高置信 xTS 形状，不猜自由文本：

    - suite：cts/gts/vts/sts/mts 词（含 cts-v、cts-verifier），归一为大写；
    - module：驼峰 + ``TestCases``/``Tests``/``TestClass`` 后缀
      （``CtsCameraTestCases``）；纯英文词（"Tests"）不算；
    - testcase：``Class#method``（优先）或末段 ``test*`` 的点分方法名
      （``android.foo.BarTest.testBaz``）。

    返回 ``{"suite", "module", "testcase"}``，提不到的键为空串。
    """
    source = str(text or "")
    lowered = source.lower()
    suite = ""
    for token in _SUITE_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            suite = token.upper()
            break
    module = ""
    for match in _MODULE_RE.finditer(source):
        candidate = match.group(1)
        if _camel_humped(candidate):
            module = candidate
            break
    testcase = ""
    hash_match = _TESTCASE_HASH_RE.search(source)
    if hash_match:
        testcase = f"{hash_match.group(1)}#{hash_match.group(2)}"
    else:
        for match in _TESTCASE_DOTTED_RE.finditer(source):
            dotted = match.group(1)
            last = dotted.rsplit(".", 1)[-1]
            if last.startswith("test") and any(c.isupper() for c in dotted):
                testcase = dotted
                break
    return {"suite": suite, "module": module, "testcase": testcase}


def failure_identity(
    failures: list[dict[str, Any]],
    *,
    suite: str = "",
    android_version: str = "",
    device_class: str = "",
    device_serial: str = "",
) -> dict[str, Any]:
    """从失败列表构造确定性身份。

    ``failures`` 行形状与 ``analysis_similarity`` 一致：
    ``{"module": ..., "name": ..., "reason": ...}``。

    ``device_class`` 必须是设备**类**（SoC/product/形态）；``device_serial``
    是观察元数据，随身份落库但**不参与指纹**（全局审查 4.2）。

    返回::

        {
          "suite", "module", "testcase", "assertion_class",
          "error_signature", "android_version", "device_class",
          "device_serial",  # 观察元数据，不进指纹
          "fingerprint",    # sha256(canonical fields, 不含 device_serial)
          "clusterable",    # testcase + signature 或 module + strong signature
        }
    """
    failure_list = [f for f in (failures or []) if isinstance(f, dict)]
    # Keep the testcase and reason paired; never join facts from different rows.
    row = next((f for f in failure_list if (f.get("name") or f.get("testcase"))
                and (f.get("reason") or f.get("message"))),
               next(iter(failure_list), {}))
    module = str(row.get("module") or "").strip()
    testcase = str(row.get("name") or row.get("testcase") or "").strip()
    reason = str(row.get("reason") or row.get("message") or "").strip()
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
        "device_serial": str(device_serial or "").strip(),
    }
    identity["fingerprint"] = _fingerprint(identity)
    identity["clusterable"] = _clusterable(identity)
    return identity


def _clusterable(identity: dict[str, Any]) -> bool:
    signature = str(identity.get("error_signature") or "").strip()
    informative = signature.lower() not in {"", "fail", "failed", "failure", "error", "unknown"}
    strong = bool(identity.get("assertion_class")) or (len(signature) >= 12 and len(signature.split()) >= 2)
    return bool(informative and (identity.get("testcase") or (identity.get("module") and strong)))


#: 参与指纹的字段（``device_serial`` 是观察元数据，刻意不在其中）。
_FINGERPRINT_FIELDS = (
    "suite", "module", "testcase", "assertion_class",
    "error_signature", "android_version", "device_class",
)


def _fingerprint(identity: dict[str, Any]) -> str:
    canonical = "\x00".join(
        str(identity.get(key) or "")
        for key in _FINGERPRINT_FIELDS
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
    if (not _clusterable(first) or not _clusterable(second)
            or first.get("clusterable") is False or second.get("clusterable") is False):
        return "UNRELATED"
    if first.get("fingerprint") and first.get("fingerprint") == second.get("fingerprint"):
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
        if not fingerprint or not _clusterable(identity) or identity.get("clusterable") is False:
            continue
        group = groups.setdefault(
            fingerprint,
            {"fingerprint": fingerprint, "members": [], "identity": {
                key: identity.get(key)
                for key in (*_FINGERPRINT_FIELDS, "device_serial")
            }},
        )
        subject = subject_ids[index] if subject_ids and index < len(subject_ids) else index
        group["members"].append(subject)
    clusters = sorted(groups.values(), key=lambda g: -len(g["members"]))
    return [
        {**group, "size": len(group["members"]), "members": group["members"]}
        for group in clusters
    ]
