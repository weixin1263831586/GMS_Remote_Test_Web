"""Rule-based certification error detection (Redmine.txt §5.3).

Shared by attachment analysis (OCR'd screenshots of BTS/EDLA reports) and the
case extractor. Returns the detected error patterns, the partitions mentioned,
and a best-guess certification type. Pure rules — no model call.
"""

from __future__ import annotations

import re
from typing import Any


# BTS/EDLA error patterns. Each: (signature, [regex patterns]).
CERT_ERROR_PATTERNS: list[tuple[str, list[re.Pattern[str]]]] = [
    (
        "VBMeta test key",
        [
            re.compile(r"VBMeta\s*test\s*key", re.I),
            re.compile(r"publicly\s*known\s*VBMeta\s*test\s*key", re.I),
            re.compile(r"signed\s*with.*VBMeta.*test\s*key", re.I),
        ],
    ),
    (
        "publicly known key",
        [re.compile(r"publicly\s*known\s+(?:rsa|ec|avb|aes)?\s*key", re.I)],
    ),
    (
        "APEX signature",
        [re.compile(r"apex.*(?:sig|签名|hash\s*mismatch)", re.I)],
    ),
    (
        "attestation key",
        [re.compile(r"attestation.*(?:key|cert).*(?:test|invalid|missing)", re.I)],
    ),
    (
        "rollback protection",
        [re.compile(r"rollback\s*(?:protection|index)", re.I)],
    ),
]

_PARTITION_RE = re.compile(r"\b(vbmeta|boot|system|vendor|odm|dtbo|product|system_ext|vbmeta_system|recovery)\b", re.I)

# Certification/test type keywords in priority order (first match wins).
# Single source of truth for both attachment analysis and the case extractor —
# note CTS-Verifier/MCTS are matched *before* the bare CTS/GMS so a more
# specific type wins (e.g. "CTS-Verifier" beats "CTS").
_CERT_TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("BTS", re.compile(r"\bBTS\b", re.I)),
    ("EDLA", re.compile(r"\bEDLA\b", re.I)),
    ("CTS-Verifier", re.compile(r"\bCTS[-\s]?Verif", re.I)),
    ("CTS", re.compile(r"\bCTS\b", re.I)),
    ("VTS", re.compile(r"\bVTS\b", re.I)),
    ("GTS", re.compile(r"\bGTS\b", re.I)),
    ("GMS", re.compile(r"\bGMS\b", re.I)),
    ("MCTS", re.compile(r"\bMCTS\b", re.I)),
]


def detect_certification_type(text: str) -> str:
    """Best-guess certification/test type for *text* (BTS/EDLA/CTS-Verifier/...).

    Shared by attachment analysis and the case extractor so both paths agree.
    """
    text = str(text or "")
    for name, pattern in _CERT_TYPE_PATTERNS:
        if pattern.search(text):
            return name
    return ""


def detect_certification_errors(text: str) -> dict[str, Any]:
    """Scan ``text`` (OCR / log / description) for certification error signals.

    Returns::

        {
            "errors": ["VBMeta test key", ...],   # matched signatures
            "partitions": ["system", "vbmeta"],   # partitions referenced
            "certification_type": "BTS",          # best-guess test type
            "failures": [                          # ready-to-consume failure dicts
                {"name": "...", "module": "BTS", "reason": "..."},
            ],
        }
    """
    text = str(text or "")
    errors: list[str] = []
    seen: set[str] = set()
    failures: list[dict[str, str]] = []
    for signature, patterns in CERT_ERROR_PATTERNS:
        for pattern in patterns:
            match = pattern.search(text)
            if match and signature not in seen:
                seen.add(signature)
                errors.append(signature)
                # Capture the surrounding line as the failure reason.
                start = max(0, match.start() - 60)
                end = min(len(text), match.end() + 120)
                reason = text[start:end].strip().replace("\n", " ")
                failures.append({
                    "name": signature,
                    "module": "AVB/VBMeta" if "VBMeta" in signature or "key" in signature else "cert",
                    "reason": reason[:300],
                })
                break

    partitions = list(dict.fromkeys(p.lower() for p in _PARTITION_RE.findall(text)))
    cert_type = detect_certification_type(text)

    return {
        "errors": errors,
        "partitions": partitions,
        "certification_type": cert_type,
        "failures": failures,
    }
