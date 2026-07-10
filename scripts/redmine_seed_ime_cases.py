#!/usr/bin/env python3
"""Seed the per-user Redmine knowledge base with verified IME/CTS cases.

Background: the diagnosis "知识库" panel only reported "未命中知识库" because the
curated ``redmine_case_facts`` table was empty even though the synced Redmine
issue store held verified tickets. This script promotes two high-value issues
into ``case_facts`` (with FTS) so the mature-case / reply-drafting pipeline can
recall the real, verified root cause:

  #618660 — closed, verified. Root cause: CTS run with a secondary display
            attached routes showSoftInput/hideSoftInput to the wrong Display,
            causing the ImeEventStream timeout (ImeEventStreamTestUtils.java:138).
            Fix: disconnect the secondary display, single-display rerun.

  #637450 — confirmed, open. The exact testTapThenSetQuery failure; reuses the
            #618660 root cause and points at the verified reference tickets.

Idempotent: re-running upserts the same rows. Targets the per-user knowledge
store that already holds synced data (the one ``_resolve_redmine_knowledge_service``
falls back to when no request owner is present).
"""

from __future__ import annotations

import sys

from features.redmine.case_extractor import RedmineCaseExtractor

# Resolve the active per-user knowledge service (largest populated store).
from features.reports.api_helpers import _resolve_redmine_knowledge_service


# Verified root cause + solution for the multi-display IME timeout family.
# Sourced from the closed/verified Redmine ticket #618660.
MULTI_DISPLAY_ROOT_CAUSE = (
    "测试环境问题：CTS 测试时设备连接了副屏。多屏环境下 WindowManager 与 "
    "InputMethodManagerService(IMMS) 会把 showSoftInput / hideSoftInput 事件"
    "路由到错误的 Display，主屏事件流(ImeEventStream)抓不到预期的 "
    "showSoftInput 事件，在 ImeEventStreamTestUtils.expectEvent "
    "(ImeEventStreamTestUtils.java:138) 处超时抛 TimeoutException。"
    "这是 Google CTS 对多屏显示设备的已知限制，并非固件 Bug。"
)

MULTI_DISPLAY_SOLUTION = (
    "1. 物理断开副屏(HDMI/DP)，确保设备仅在单屏(主屏)环境运行：\n"
    "   adb shell dumpsys display | grep \"Display Id\"   # 应只剩一个 Display Id\n"
    "2. 重置 CTS 测试环境后重跑：\n"
    "   adb uninstall android.view.inputmethod.cts\n"
    "   adb shell pm clear com.android.cts.mockime\n"
    "   ./cts-tradefed run cts -m CtsInputMethodTestCases\n"
    "3. 单独复现失败用例：\n"
    "   ./cts-tradefed run cts -m CtsInputMethodTestCases "
    "-t android.view.inputmethod.cts.SearchViewTest#testTapThenSetQuery"
)

MULTI_DISPLAY_VERIFICATION = (
    "断开副屏后单屏重跑 CtsInputMethodTestCases，showSoftInput 事件在主屏"
    "ImeEventStream 中正常捕获，testTapThenSetQuery 等用例通过。"
    "已验证于历史单 #618660(朱珂汉/黄超群)。"
)

# issue_id -> override fields applied on top of the auto-extracted fact.
_OVERRIDES: dict[int, dict] = {
    618660: {
        "root_cause": MULTI_DISPLAY_ROOT_CAUSE,
        "solution": MULTI_DISPLAY_SOLUTION,
        "verification": MULTI_DISPLAY_VERIFICATION,
        "reply_template": MULTI_DISPLAY_SOLUTION,
        "confidence": 90.0,
        "source_quality": "verified_closed",
    },
    637450: {
        "root_cause": MULTI_DISPLAY_ROOT_CAUSE,
        "solution": (
            "本单为 testTapThenSetQuery 失败，根因与已验证历史单 #618660 一致"
            "(多屏环境导致 showSoftInput 事件路由错误)。处理方式：断开副屏，"
            "单屏重跑 CtsInputMethodTestCases。\n\n" + MULTI_DISPLAY_SOLUTION
        ),
        "verification": "参考已验证历史单 #618660、#633809。",
        "confidence": 75.0,
        "source_quality": "confirmed_open",
    },
}


def seed(target_issue_ids: list[int] | None = None) -> dict:
    service = _resolve_redmine_knowledge_service(None)
    if service is None:
        print("No populated per-user knowledge store found; nothing to seed.", file=sys.stderr)
        return {"seeded": [], "skipped": target_issue_ids or list(_OVERRIDES)}

    db = service.knowledge_db
    repo = service.issue_repository
    issue_ids = target_issue_ids or list(_OVERRIDES)
    seeded: list[int] = []

    for issue_id in issue_ids:
        issue = repo.get_issue(issue_id)
        if not issue:
            print(f"  #{issue_id}: not in synced issue store, skip", file=sys.stderr)
            continue
        fact = RedmineCaseExtractor.extract(issue)
        fact.update(_OVERRIDES.get(issue_id, {}))
        # Stamp keywords so FTS reliably matches IME/showSoftInput/SearchView probes.
        kws = set(fact.get("keywords") or [])
        kws.update({
            "showSoftInput", "TimeoutException", "ImeEventStreamTestUtils",
            "ImeEventStream", "SearchView", "testTapThenSetQuery",
            "CtsInputMethodTestCases", "多屏", "副屏", "secondary display",
        })
        fact["keywords"] = sorted(kws)
        db.upsert_case_fact(fact)
        seeded.append(issue_id)
        print(f"  #{issue_id}: seeded ({fact.get('error_signature') or 'no-signature'})")

    return {"seeded": seeded, "skipped": [i for i in issue_ids if i not in seeded]}


if __name__ == "__main__":
    result = seed()
    print(f"\nDone. Seeded {len(result['seeded'])} case fact(s): {result['seeded']}")
