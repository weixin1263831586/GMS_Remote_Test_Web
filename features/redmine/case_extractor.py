"""Structured case-fact extraction for the Redmine knowledge base.

The knowledge base reuses the structured fields already produced during issue
analysis (see :mod:`features.redmine.analysis_resolution`) rather than calling
the AI model again. This module adds rule-based classification on top:
chip platform, Android version, certification type, module, error signature,
region — plus maps the issue's analysis text into the case-fact vocabulary
defined by ``Redmine.txt`` §5.4.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .cert_rules import detect_certification_errors, detect_certification_type

# Placeholder texts the rule-based fallbacks emit when no real analysis is
# available. They carry no knowledge and must never win over the signature KB.
# Shared by the mature-case builder, reply drafter and knowledge service so the
# list stays in one place (Redmine.txt §5.4).
MEANINGLESS_PLACEHOLDERS = (
    "暂无分析结果",
    "暂无",
    "需要进一步分析",
    "待进一步分析确认",
    "未提取到描述",
    "当前证据中未找到明确已验证解决方案",
    "未从现有证据中提取到明确补丁",
    "当前缺少可定位补丁",
)


def is_meaningful(value: Any) -> bool:
    """True when *value* is a non-empty string that is not a placeholder."""
    text = str(value or "").strip()
    if not text:
        return False
    return not any(ph in text for ph in MEANINGLESS_PLACEHOLDERS)


def first_meaningful(values) -> str:
    """Return the first meaningful value in *values*, stripped; else ''."""
    for value in values or []:
        if is_meaningful(value):
            return str(value).strip()
    return ""


def meaningful_text(value: Any) -> str:
    """Return the stripped *value* when it is meaningful, else ''.

    Like :func:`is_meaningful` but returns the text. Also rejects the
    auto-generated journal summary line (``历史回复: … 条有内容回复可参考``),
    which reply drafting must never treat as real content.
    """
    text = str(value or "").strip()
    if not is_meaningful(text):
        return ""
    if text.startswith("历史回复:") and "条有内容回复可参考" in text:
        return ""
    return text


def decode_json_list(value, default=None):
    """Decode a ``*_json`` column that should hold a list.

    ``_decode_row`` already decodes JSON columns on read, but callers also
    receive these fields via other paths, so the ``isinstance(str)`` guard is
    kept as a defensive fallback. Returns *default* (``[]`` if omitted) on a
    non-list / unparseable value.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return default if default is not None else []
    if isinstance(value, list):
        return value
    return default if default is not None else []


def decode_json_obj(value, default=None):
    """Like :func:`decode_json_list` but for object/dict columns (default ``{}``)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return default if default is not None else {}
    if isinstance(value, dict):
        return value
    return default if default is not None else {}


# Certification / test types in priority order (first match wins).
# Certification-type detection is shared from cert_rules (single source of truth,
# keeps BTS/EDLA/CTS-Verifier/CTS/VTS/GTS/GMS/MCTS order consistent everywhere).

# Module detection rules: (module_name, compiled regex on the issue text).
_MODULE_RULES = [
    ("AVB/VBMeta", re.compile(r"vbmeta|avb\b|verified\s*boot", re.I)),
    ("Power HAL", re.compile(r"VtsHalPower|PowerAidl|power_ext|power\s*hal", re.I)),
    ("Bluetooth", re.compile(r"\bbluetooth\b|\b蓝牙\b|\bbt\b\s*test", re.I)),
    ("APEX签名", re.compile(r"apex.*sig|apex.*签名|com\.android\.apex", re.I)),
    ("AdServices", re.compile(r"CtsAdServices|AdServices", re.I)),
    ("GtsFeatures", re.compile(r"GtsFeatures|AdvancedProtection", re.I)),
    ("Battery", re.compile(r"\bbattery\b|\b电池\b", re.I)),
    ("IntentFirewall", re.compile(r"IntentFirewall|GtsIntentFirewall", re.I)),
    ("DeviceOwner", re.compile(r"Device\s*Owner|cts.?verifier.*disable", re.I)),
    ("Sensor", re.compile(r"sensor|传感器|加速度", re.I)),
    ("Boot/Wifi", re.compile(r"无法.*开机|boot.*fail|wifi", re.I)),
]

# Canonical error signatures. Each entry: (signature, list of patterns).
# When any pattern matches, that signature is recorded — it is what the
# similarity search uses to cluster duplicate problems (Redmine.txt §5.3).
_ERROR_SIGNATURES = [
    (
        "VBMeta test key",
        [
            re.compile(r"VBMeta\s*test\s*key", re.I),
            re.compile(r"publicly\s*known\s*VBMeta\s*test\s*key", re.I),
            re.compile(r"signed\s*with.*VBMeta.*test\s*key", re.I),
            re.compile(r"vbmeta.*test\s*key", re.I),
        ],
    ),
    (
        "APEX signature",
        [re.compile(r"apex.*sig|apex.*签名", re.I)],
    ),
    (
        "PowerAidl hasFixedPerformance unsupported",
        [
            re.compile(r"Power/PowerAidl[#.]hasFixedPerformance", re.I),
            re.compile(r"VtsHalPowerTargetTest.*hasFixedPerformance", re.I | re.S),
            re.compile(r"isModeSupported\s*\(\s*Mode::FIXED_PERFORMANCE", re.I),
        ],
    ),
    (
        "Device not booting",
        [re.compile(r"无法.*开机|boot.*fail|not.*boot", re.I)],
    ),
]

# Root-cause / solution templates keyed by canonical error signature.
_SIG_KNOWLEDGE: dict[str, dict[str, str]] = {
    "VBMeta test key": {
        "root_cause": "认证版本使用公开的 AVB/VBMeta 测试 key（test key），未切换为客户量产 production AVB key。",
        "solution": (
            "1. 确认客户量产 AVB key（test/verity key、avb_pkmd）。\n"
            "2. 使用 production AVB key 重新签名 system/vendor/vbmeta 等相关分区。\n"
            "3. 重新生成 vbmeta.img 并替换镜像。\n"
            "4. 重新烧写后再次运行 BTS/EDLA 扫描确认不再报 VBMeta test key。"
        ),
        "verification": "重新签名并烧写后，BTS 扫描 system/vbmeta 分区不再出现 'publicly known VBMeta test key'。",
        "verification_steps": [
            "用 production AVB key 重新签名相关分区",
            "重新生成 vbmeta.img",
            "烧写后重跑 BTS 扫描，确认无 VBMeta test key 报错",
        ],
    },
    "PowerAidl hasFixedPerformance unsupported": {
        "root_cause": (
            "Android16 VTS 要求 Power HAL AIDL 接口支持 FixedPerformance 模式；"
            "当前 RK 平台 Power HAL 扩展实现未对 Mode::FIXED_PERFORMANCE 返回支持，"
            "导致 IPower.isModeSupported(Mode.FIXED_PERFORMANCE) 返回 false。"
        ),
        "solution": (
            "1. 定位 RK SDK 中 Power HAL 扩展实现目录，例如 vendor/rockchip/hardware/modules/power_ext。\n"
            "2. 检查 Power.cpp / PowerExt.cpp 中 isModeSupported 的实现。\n"
            "3. 增加 Mode::FIXED_PERFORMANCE 分支，使其返回 true，并保持其他模式原有逻辑不变。\n"
            "4. 重新单编 power 模块并更新设备端 android.hardware.power-service-default。\n"
            "5. 重启 power service 或重启设备后，重新执行 run vts -m VtsHalPowerTargetTest 验证。"
        ),
        "verification": "重新执行 run vts -m VtsHalPowerTargetTest，确认 Power/PowerAidl#hasFixedPerformance 通过，不再出现 Actual: false / Expected: true。",
        "verification_steps": [
            "单编并替换 Power HAL service",
            "重启 power service 或设备",
            "重跑 VtsHalPowerTargetTest",
            "确认 hasFixedPerformance 返回 true 并通过 VTS",
        ],
    },
}

_REGION_RULES = [
    ("海外", re.compile(r"海外|export|overseas", re.I)),
    ("商显", re.compile(r"商显|digital\s*signage|kiosk", re.I)),
    ("车载", re.compile(r"车载|car|automotive", re.I)),
    ("平板", re.compile(r"tablet|平板", re.I)),
    ("盒子", re.compile(r"\bbox\b|盒子", re.I)),
]

# Stopwords stripped from the keyword list.
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "with",
    "fail", "failed", "test", "问题", "测试", "模块", "失败",
}


class RedmineCaseExtractor:
    """Extract a structured case fact from a stored issue row."""

    @classmethod
    def extract(cls, issue: dict[str, Any], *, failures: list | None = None, journals: list | None = None, attachments: list | None = None) -> dict[str, Any]:
        issue = issue or {}
        failures = failures or issue.get("failures_json") or []
        journals = journals or issue.get("journals_json") or []
        attachments = attachments or issue.get("attachments_json") or []

        subject = str(issue.get("subject") or "")
        description = str(issue.get("description") or "")
        fixed_version = str(issue.get("fixed_version") or "")
        error_info = str(issue.get("error_info") or "")
        error_analysis = str(issue.get("error_analysis") or "")
        solution_text = str(issue.get("solution") or "")
        patch_direction = str(issue.get("patch_direction") or "")
        summary = str(issue.get("summary") or "")
        doc_content = str(issue.get("doc_content") or "")

        # Attachment / journal failures contribute more text signal.
        attachment_failures_text = cls._collect_attachment_failure_text(attachments)
        journal_text = cls._collect_journal_text(journals)
        full_text = "\n".join(filter(None, [subject, description, summary, error_info, attachment_failures_text]))

        chip_platform = cls._detect_chip_platform(subject, description, fixed_version)
        android_version = cls._detect_android_version(subject, description, fixed_version)
        certification_type = cls._detect_certification_type(full_text)
        module = cls._detect_module(full_text, failures)
        error_signature = cls._detect_error_signature(full_text)
        # Fall back to the shared cert-rules detector (covers OCR'd attachment
        # text and multi-pattern signatures) when the basic regex missed.
        if not error_signature:
            cert_detected = detect_certification_errors(attachment_failures_text or full_text)
            if cert_detected["errors"]:
                error_signature = cert_detected["errors"][0]
            if not certification_type and cert_detected["certification_type"]:
                certification_type = cert_detected["certification_type"]
        region = cls._detect_region(full_text)

        sig_knowledge = _SIG_KNOWLEDGE.get(error_signature, {})
        # Prefer the issue's AI/rule analysis, but fall back to the signature
        # knowledge base when that text is a placeholder ("暂无分析结果" etc.).
        root_cause = cls._meaningful_or(error_analysis, sig_knowledge.get("root_cause", "")).strip()
        solution = cls._meaningful_or(solution_text, sig_knowledge.get("solution", "")).strip()
        verification = (sig_knowledge.get("verification") or "").strip()

        problem_summary = cls._build_problem_summary(subject, description, summary, failures)
        symptoms = cls._build_symptoms(failures, error_info, description, full_text)
        reply_template = cls._build_reply_template(issue, module, error_signature, root_cause, solution, verification)
        keywords = cls._build_keywords(subject, error_signature, module, chip_platform, android_version, full_text)
        confidence = cls._score_confidence(chip_platform, android_version, module, error_signature, solution)
        source_quality = cls._classify_quality(issue, solution)

        evidence = {
            "subject": subject,
            "redmine_description": description[:2000],
            "status_name": issue.get("status_name") or "",
            "error_info": error_info[:3000],
            "journals": cls._summarize_journals(journals),
            "journals_count": len(journals),
            "attachments": cls._summarize_attachments(attachments),
            "attachments_count": len(attachments),
        }

        return {
            "issue_id": int(issue.get("issue_id") or 0),
            "subject": subject,
            "status_name": issue.get("status_name") or "",
            "assigned_to_name": issue.get("assigned_to_name") or "",
            "project_name": issue.get("project_name") or "",
            "category": issue.get("category") or "",
            "chip_platform": chip_platform,
            "android_version": android_version,
            "certification_type": certification_type,
            "module": module,
            "product_form": cls._detect_product_form(full_text),
            "region": region,
            "error_signature": error_signature,
            "problem_summary": problem_summary,
            "symptoms": symptoms,
            "root_cause": root_cause,
            "solution": solution,
            "verification": verification,
            "reply_template": reply_template,
            "keywords": keywords,
            "evidence": evidence,
            "doc_excerpt": doc_content[:4000],
            "confidence": confidence,
            "source_quality": source_quality,
        }

    # ------------------------------------------------------------------
    # Detection rules
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_chip_platform(subject: str, description: str, fixed_version: str) -> str:
        match = re.search(r"(RK\d{4,})", f"{subject} {description} {fixed_version}", re.IGNORECASE)
        return match.group(1).upper() if match else ""

    @staticmethod
    def _detect_android_version(subject: str, description: str, fixed_version: str) -> str:
        match = re.search(r"android\s*(\d+(?:\.\d+)*)", f"{subject} {description} {fixed_version}", re.IGNORECASE)
        if not match:
            return ""
        # Normalise to the major version (Android16, not Android16.0) so the
        # similarity score aligns across "Android16" / "Android 16.0" forms.
        major = match.group(1).split(".")[0]
        return f"Android{major}"

    # Certification-type detection delegates to cert_rules (shared, no local list).
    _detect_certification_type = staticmethod(detect_certification_type)

    @staticmethod
    def _detect_module(text: str, failures: list) -> str:
        for name, pattern in _MODULE_RULES:
            if pattern.search(text):
                return name
        # Fallback: derive from failure module/name.
        for failure in failures or []:
            if isinstance(failure, dict):
                mod = str(failure.get("module") or "")
                if mod:
                    return mod[:60]
        return ""

    @staticmethod
    def _detect_error_signature(text: str) -> str:
        for signature, patterns in _ERROR_SIGNATURES:
            if any(p.search(text) for p in patterns):
                return signature
        return ""

    @staticmethod
    def _detect_region(text: str) -> str:
        for name, pattern in _REGION_RULES:
            if pattern.search(text):
                return name
        return ""

    # product_form currently derives from the same region rules.
    _detect_product_form = _detect_region

    # ------------------------------------------------------------------
    # Text composition
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_attachment_failure_text(attachments: list) -> str:
        chunks: list[str] = []
        for item in attachments or []:
            if not isinstance(item, dict):
                continue
            analysis = item.get("analysis_json") or {}
            for failure in analysis.get("failures") or []:
                if isinstance(failure, dict):
                    chunks.append(str(failure.get("reason") or ""))
                    chunks.append(str(failure.get("name") or ""))
            excerpt = analysis.get("text_excerpt") or (analysis.get("details") or {}).get("ocr_text") or ""
            if excerpt:
                chunks.append(str(excerpt))
        return "\n".join(chunk for chunk in chunks if chunk)

    @staticmethod
    def _collect_journal_text(journals: list) -> str:
        chunks: list[str] = []
        for item in journals or []:
            if isinstance(item, dict):
                chunks.append(str(item.get("notes") or ""))
        return "\n".join(chunk for chunk in chunks if chunk)

    @staticmethod
    def _summarize_journals(journals: list) -> list[dict[str, str]]:
        """Structured journal timeline for evidence (Redmine.txt §4 evidence.journals)."""
        summarized: list[dict[str, str]] = []
        for item in (journals or [])[:30]:
            if not isinstance(item, dict):
                continue
            notes = str(item.get("notes") or "").strip()
            if not notes:
                continue
            summarized.append({
                "user": str(item.get("user") or "")[:60],
                "created_on": str(item.get("created_on") or "")[:19],
                "notes": notes[:500],
            })
        return summarized

    @staticmethod
    def _summarize_attachments(attachments: list) -> list[dict[str, Any]]:
        """Structured attachment evidence (filename/status/parsed/failures/ocr excerpt)."""
        summarized: list[dict[str, Any]] = []
        for item in (attachments or [])[:20]:
            if not isinstance(item, dict):
                continue
            analysis = item.get("analysis_json") or {}
            details = analysis.get("details") or {}
            failures = analysis.get("failures") or []
            excerpt = (analysis.get("text_excerpt") or details.get("ocr_text") or "")[:400]
            detected_errors = details.get("detected_errors") or []
            summarized.append({
                "filename": str(item.get("filename") or ""),
                "status": str(item.get("status") or ""),
                "parsed": bool(analysis.get("parsed")),
                "type": str(details.get("type") or ""),
                "certification_type": str(details.get("certification_type") or ""),
                "failure_count": len(failures) if isinstance(failures, list) else 0,
                "detected_errors": detected_errors[:5] if isinstance(detected_errors, list) else [],
                "excerpt": excerpt,
            })
        return summarized

    @staticmethod
    def _build_problem_summary(subject: str, description: str, summary: str, failures: list) -> str:
        if failures and isinstance(failures[0], dict):
            first = failures[0]
            line = f"{first.get('module') or '未知模块'} / {first.get('name') or '未知用例'}"
            reason = str(first.get("reason") or "").split("\n")[0][:160]
            return f"{line}：{reason}" if reason else line
        return (summary or description or subject)[:240]

    @staticmethod
    def _build_symptoms(failures: list, error_info: str, description: str = "", full_text: str = "") -> list[str]:
        symptoms: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            value = str(value or "").strip()
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                symptoms.append(value)

        for failure in (failures or [])[:5]:
            if isinstance(failure, dict):
                name = f"{failure.get('module') or ''}/{failure.get('name') or ''}".strip("/")
                reason = str(failure.get("reason") or "").split("\n")[0][:200]
                if name and reason:
                    add(f"失败用例：{name} —— {reason}")
                elif name:
                    add(f"失败用例：{name}")
        text = "\n".join(filter(None, [description, error_info, full_text]))
        test_match = re.search(r"(Power/PowerAidl[#.]hasFixedPerformance(?:/[^\s\r\n]+)?)", text, re.I)
        if test_match:
            add(f"失败用例：{test_match.group(1)}")
        if re.search(r"Actual:\s*false", text, re.I) and re.search(r"Expected:\s*true", text, re.I):
            add("断言失败：supported=false，期望 true")
        if error_info:
            first_line = error_info.strip().split("\n")[0][:200]
            if first_line:
                add(f"关键报错：{first_line}")
        return symptoms

    @staticmethod
    def _build_reply_template(issue: dict, module: str, signature: str, root_cause: str, solution: str, verification: str) -> str:
        subject = issue.get("subject") or ""
        lines = [
            f"您好，关于 #{issue.get('issue_id')} {subject}，初步分析如下：",
            "",
            f"- 模块：{module or '-'}",
        ]
        if signature:
            lines.append(f"- 问题：{signature}")
        if root_cause:
            lines.append(f"- 根因：{root_cause}")
        if solution:
            lines.extend(["", "解决步骤：", solution])
        if verification:
            lines.extend(["", "验证方式：", verification])
        lines.extend(["", "如有进一步日志可继续协助确认根因，谢谢。"])
        return "\n".join(lines)

    @staticmethod
    def _build_keywords(subject: str, signature: str, module: str, chip: str, android: str, full_text: str) -> list[str]:
        keywords: list[str] = []
        for source in (signature, module, chip, android):
            value = (source or "").strip()
            if value:
                keywords.append(value)
        # Add tokens from subject/full_text (alphanumeric, length>=3).
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}|[一-龥]{2,}", f"{subject} {full_text}")
        seen = {k.lower() for k in keywords}
        for token in tokens:
            key = token.lower()
            if key in seen or key in _STOPWORDS:
                continue
            seen.add(key)
            keywords.append(token)
            if len(keywords) >= 20:
                break
        return keywords

    @staticmethod
    def _score_confidence(chip: str, android: str, module: str, signature: str, solution: str) -> float:
        score = 0.0
        if chip:
            score += 15
        if android:
            score += 15
        if module:
            score += 25
        if signature:
            score += 30
        if solution:
            score += 15
        return min(score, 100.0)

    # Placeholder texts produced by the rule-based fallbacks in analysis_resolution;
    # they carry no real knowledge and should yield to the signature knowledge base.
    _PLACEHOLDERS = MEANINGLESS_PLACEHOLDERS

    @classmethod
    def _meaningful_or(cls, primary: str, fallback: str) -> str:
        """Return primary if it is meaningful, else fallback (also checked)."""
        primary = str(primary or "").strip()
        if primary and not any(ph in primary for ph in cls._PLACEHOLDERS):
            return primary
        fallback = str(fallback or "").strip()
        if fallback and not any(ph in fallback for ph in cls._PLACEHOLDERS):
            return fallback
        return primary or fallback

    @staticmethod
    def _classify_quality(issue: dict, solution: str) -> str:
        status = str(issue.get("status_name") or "").lower()
        if solution and status in ("closed", "已关闭"):
            return "high"
        if solution:
            return "medium"
        return "low"
