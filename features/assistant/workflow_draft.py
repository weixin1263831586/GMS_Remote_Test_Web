"""WorkflowDraft — 多阶段工作流草案（烧写 → AOA → 测试）。

对话 Agent 目前只能生成单一 ``test_execution`` 计划，表达不了
「烧写固件 → AOA 恢复 → CTS 测试」这类完整链路。本模块把一句自然语言
解析为结构化草案（实体、阶段、阻塞原因），再由确定性代码做资源校验；
实际执行仍统一走 features/automation 的状态机，这里不引入新的编排器。

设计约束（对应对话链路复盘结论）：
- ``device.serial`` 缺失时进入显式阻塞，不静默替换成其他设备；
- ``firmware_share`` 是受控固件来源，凭分享 ID 换取可信文件，不放宽
  任意 HTTP URL；
- AOA 与烧写不同阶段执行：先烧写、后 AOA 恢复；
- "重新规划" 保留已解析实体并递增 ``plan_version``，而不是清空重猜。

本模块保持无跨 feature 依赖（便于单测）；feature 侧资源查询由调用方
注入 lookup 回调完成。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse


# ==================== 复合意图识别 ====================

_FLASH_HINT_RE = re.compile(
    r"烧写|烧录|刷机|刷入|flash|固件|firmware", re.IGNORECASE
)
_AOA_HINT_RE = re.compile(r"\baoa\b|AOA配置|AOA 恢复|aoa_profile|模拟点击|usb.?debug", re.IGNORECASE)
_FULL_SCOPE_RE = re.compile(
    r"完整\s*(?:跑|测试|执行|回归)|全量\s*(?:跑|测试|执行|回归)|整个\s*(?:suite|套件)"
    r"|full\s*(?:suite|run|regression)",
    re.IGNORECASE,
)
_SHARE_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SHARE_ID_RE = re.compile(r"/api/firmware-shares/([0-9a-f]{8,64})/download", re.IGNORECASE)
_SHARE_ID_BARE_RE = re.compile(
    r"(?:分享|share)[_\s-]?id\s*[:：=]\s*([0-9a-f]{8,64})", re.IGNORECASE
)


def detect_composite_intent(message: str) -> bool:
    """烧写/AOA/测试中至少两类意图复合出现时走 WorkflowDraft。"""
    text = message or ""
    hints = [
        bool(_FLASH_HINT_RE.search(text)),
        bool(_AOA_HINT_RE.search(text)),
        bool(re.search(r"CTS|GTS|VTS|STS|测试|test", text, re.IGNORECASE)),
    ]
    return sum(hints) >= 2


# ==================== 计划操作识别 ====================

_PLAN_ACTION_RE = re.compile(
    r"重新规划|重新生成计划|重新安排|再规划|replan|取消计划|取消执行|放弃计划"
    r"|计划状态|当前计划",
    re.IGNORECASE,
)


def detect_plan_action(text: str) -> str:
    """显式计划操作：replan / cancel / status；无匹配返回空串。"""
    value = text or ""
    if re.search(r"重新规划|重新生成计划|重新安排|再规划|replan", value, re.IGNORECASE):
        return "replan"
    if re.search(r"取消计划|取消执行|放弃计划", value, re.IGNORECASE):
        return "cancel"
    if re.search(r"计划状态|当前计划", value, re.IGNORECASE):
        return "status"
    return ""


# ==================== 实体提取 ====================

def extract_workflow_entities(message: str) -> dict[str, Any]:
    """从一句话里提取设备/固件/AOA/测试实体，互不污染。"""
    from features.assistant.intent import (
        _extract_device_ids,
        _extract_module_and_case,
        _extract_retry_count,
        _extract_test_type,
    )

    text = message or ""
    aoa_hint = bool(_AOA_HINT_RE.search(text))
    entities: dict[str, Any] = {
        "device_serials": _extract_device_ids(text),
        "firmware_share_id": _extract_share_id(text),
        "firmware_url": "",
        "aoa_requested": aoa_hint,
        "aoa_profile": _extract_aoa_profile(text),
        "test_type": _extract_test_type(text),
        "connect_wifi": bool(re.search(r"wifi|wi-fi|无线网络|连接网络", text, re.IGNORECASE)),
        "retry_count": _extract_retry_count(text),
        "analyze_on_failure": bool(
            re.search(r"报告分析|分析报告|失败分析", text, re.IGNORECASE)
        ),
        "flash_only": bool(re.search(r"只\s*烧写|仅\s*烧写|不(跑|执行|做)测试", text, re.IGNORECASE)),
        "test_scope_full": bool(_FULL_SCOPE_RE.search(text)),
    }
    url_match = _SHARE_URL_RE.search(text)
    if url_match:
        entities["firmware_url"] = url_match.group(0)
    module, case = _extract_module_and_case(text)
    entities["test_module"] = module
    entities["test_case"] = case
    return entities


def _extract_share_id(text: str) -> str:
    match = _SHARE_ID_RE.search(text or "")
    if match:
        return match.group(1)
    match = _SHARE_ID_BARE_RE.search(text or "")
    return match.group(1) if match else ""


def _extract_aoa_profile(text: str) -> str:
    """AOA 配置名：`执行AOA配置X` / `AOA profile X` / 引号包裹的配置名。"""
    text = text or ""
    patterns = (
        r"AOA配置\s*[:：]?\s*([A-Za-z0-9_.\-]+(?:[-_][A-Za-z0-9_.\-]+)*)",
        r"AOA\s+profile\s*[:：=]?\s*([A-Za-z0-9_.\-]+(?:[-_][A-Za-z0-9_.\-]+)*)",
        r"profile\s*[:：=]\s*[\"'“”]([^\"'“”\s，,。]+)[\"'“”]",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


# ==================== 原始用户约束冻结 ====================

def _stated_test_scope(entities: dict[str, Any]) -> str:
    """从显式实体推导冻结的测试范围：skip > module > full；未提及为空。

    优先级与 build_workflow_draft 的 scope 推导一致，保证冻结值与草案
    规范化值语义相同；只有用户显式表达过的范围才会被冻结。
    """
    if entities.get("flash_only"):
        return "skip"
    if entities.get("test_module"):
        return "module"
    return "full" if entities.get("test_scope_full") else ""


def freeze_user_constraints(
    entities: dict[str, Any], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """把本轮消息里**显式给出**的实体冻结为用户约束。

    只冻结 stated 字段（有值/为真才记录），未提及的字段保持开放，模型仍
    可补全；跨轮次时合并 previous（上一轮冻结）与本轮新约束，本轮显式
    修改覆盖旧值（如“改成 GTS”是用户主动变更，不是违约）。

    注意：本函数只会合并非空值，无法表达“用户撤回要求”（如“去掉 AOA”
    后旧的 aoa_requested/aoa_profile 仍会残留）。显式删除由
    update_draft 的规则级约束重置（constraint resets）处理，撤回后
    版本号仍然递增，保留变更历史供审计。
    """
    serials = [str(item) for item in entities.get("device_serials") or []]
    stated: dict[str, Any] = {
        "device_serial": serials[0] if serials else "",
        "firmware_share_id": str(entities.get("firmware_share_id") or ""),
        "aoa_profile": str(entities.get("aoa_profile") or ""),
        "aoa_requested": bool(entities.get("aoa_requested")),
        "test_type": str(entities.get("test_type") or "").upper(),
        "flash_only": bool(entities.get("flash_only")),
        "connect_wifi": bool(entities.get("connect_wifi")),
        "test_module": str(entities.get("test_module") or ""),
        "test_case": str(entities.get("test_case") or ""),
        "test_scope": _stated_test_scope(entities),
    }
    constraints = {
        key: value for key, value in (previous or {}).items()
        if key != "version" and value not in ("", False)
    }
    constraints.update({
        key: value for key, value in stated.items()
        if value not in ("", False)
    })
    constraints["version"] = int((previous or {}).get("version") or 0) + 1
    return constraints


def constraint_conflicts(
    entities: dict[str, Any], constraints: dict[str, Any]
) -> list[str]:
    """模型实体（规范化为草案后）与冻结约束的冲突清单（空列表 = 无冲突）。

    比较在 build_workflow_draft 的规范化结果上进行：被冻结的字段必须在
    草案上存在且相等——模型省略冻结字段同样构成冲突，不允许靠漏报绕过
    用户原始约束（如用户要求 CTS 而模型只输出烧写实体）。只有用户显式
    修改（重新规划规则触发约束重新冻结）才允许变更冻结值。
    """
    draft = build_workflow_draft(entities)
    conflicts: list[str] = []
    serial = str((draft.get("device") or {}).get("serial") or "")
    stated_serial = str(constraints.get("device_serial") or "")
    if stated_serial and serial != stated_serial:
        conflicts.append(f"设备 {stated_serial}（不得自动替换或遗漏）")
    share_id = str((draft.get("firmware") or {}).get("share_id") or "")
    stated_share = str(constraints.get("firmware_share_id") or "")
    if stated_share and share_id != stated_share:
        conflicts.append(f"固件分享 {stated_share}（不得换成其他来源或遗漏）")
    aoa = draft.get("aoa") or {}
    stated_profile = str(constraints.get("aoa_profile") or "")
    if stated_profile and str(aoa.get("profile") or "") != stated_profile:
        conflicts.append(f"AOA 配置 {stated_profile}（不得换成其他配置或遗漏）")
    if constraints.get("aoa_requested") and aoa.get("mode") != "user_build_recovery":
        conflicts.append("用户明确要求 AOA 恢复（不得静默关闭）")
    test = draft.get("test") or {}
    stated_type = str(constraints.get("test_type") or "")
    if stated_type and str(test.get("type") or "") != stated_type:
        conflicts.append(f"测试类型 {stated_type}（不得更换或遗漏）")
    if constraints.get("flash_only") and str(test.get("scope") or "") != "skip":
        conflicts.append("用户明确只烧写不测试（不得追加测试阶段）")
    stated_scope = str(constraints.get("test_scope") or "")
    if stated_scope and str(test.get("scope") or "") != stated_scope:
        conflicts.append(f"测试范围 {stated_scope}（不得更换或遗漏）")
    stated_module = str(constraints.get("test_module") or "")
    if stated_module and str(test.get("module") or "") != stated_module:
        conflicts.append(f"测试模块 {stated_module}（不得更换或遗漏）")
    stated_case = str(constraints.get("test_case") or "")
    if stated_case and str(test.get("case") or "") != stated_case:
        conflicts.append(f"测试用例 {stated_case}（不得更换或遗漏）")
    if constraints.get("connect_wifi") and not test.get("connect_wifi"):
        conflicts.append("用户明确要求连接 Wi-Fi（不得静默关闭）")
    return conflicts


# ==================== 草案构建与更新 ====================

_WORKFLOW_INTENT = "flash_aoa_test"

_TEST_TYPE_TO_SUITE_PREFIX = {"CTS": "android-cts", "GTS": "android-gts", "VTS": "android-vts", "STS": "android-sts"}


def build_workflow_draft(
    entities: dict[str, Any],
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把实体装配成带阻塞位的草案；worker_id 留待全局库存解析。"""
    workspace = workspace or {}
    serials = [str(item) for item in entities.get("device_serials") or []]
    test_type = str(entities.get("test_type") or "").upper()
    flash_only = bool(entities.get("flash_only"))
    share_id = str(entities.get("firmware_share_id") or "")
    # 平台分享链接是唯一受控固件来源：能从 URL 提取分享 ID 的直接换算，
    # 其余 URL（外部镜像等）不进入草案——不可暂存、不可校验、不可续传。
    url = str(entities.get("firmware_url") or "")
    if not share_id and url:
        match = _SHARE_ID_RE.search(urlparse(url).path or "")
        share_id = match.group(1) if match else ""
    return {
        "intent": _WORKFLOW_INTENT,
        "device": {
            "serial": serials[0] if serials else "",
            "serials": serials,
            "worker_id": None,
        },
        "firmware": {
            "source": "firmware_share",
            "share_id": share_id,
            "url": url,
        },
        "aoa": {
            # 用户提到 AOA 即启用恢复：缺配置名时由 draft_blocks 显式阻塞，
            # 绝不静默降级成 off（静默关闭会直接违背原始指令）。
            "mode": (
                "user_build_recovery"
                if (entities.get("aoa_profile") or entities.get("aoa_requested"))
                else "off"
            ),
            "profile": str(entities.get("aoa_profile") or ""),
        },
        "test": {
            "type": test_type,
            "module": str(entities.get("test_module") or ""),
            "case": str(entities.get("test_case") or ""),
            "scope": "skip" if flash_only else ("module" if entities.get("test_module") else "full"),
            "connect_wifi": bool(entities.get("connect_wifi")),
            "retry_count": int(entities.get("retry_count") or 0),
            "analyze_on_failure": bool(entities.get("analyze_on_failure")),
        },
        "workspace": {
            "worker_id": workspace.get("worker_id") or "",
            "scope_mode": workspace.get("scope_mode") or "single",
        },
        "status": "draft",
        "plan_version": 1,
    }


def _apply_test_type(draft: dict[str, Any], match: re.Match[str]) -> None:
    value = match.group(1) or match.group(2) or ""
    draft["test"]["type"] = value.upper()
    draft["test"]["scope"] = "skip" if draft["test"]["scope"] == "skip" else draft["test"]["scope"]


def _apply_flash_only(draft: dict[str, Any], _match: re.Match[str]) -> None:
    draft["test"]["scope"] = "skip"


def _apply_drop_aoa(draft: dict[str, Any], _match: re.Match[str]) -> None:
    draft["aoa"]["mode"] = "off"
    draft["aoa"]["profile"] = ""


def _apply_worker(draft: dict[str, Any], match: re.Match[str]) -> None:
    draft["device"]["worker_id"] = match.group(1)


# 规则元组第 4 项是约束重置键：显式删除类规则命中时，这些键必须从冻结
# 约束中移除——freeze_user_constraints 只合并非空值，旧的冻结要求（如
# aoa_requested）若不清除，后续重新规划会误认为用户仍要求 AOA（审核 P1）。
_DRAFT_UPDATE_RULES: list[tuple[str, str, Callable[[dict[str, Any], re.Match[str]], None], tuple[str, ...]]] = [
    # (说明, 触发正则, 应用函数, 需重置的冻结约束键)
    ("改成 GTS/VTS/STS/CTS", r"改成\s*(CTS|GTS|VTS|STS)|换成\s*(CTS|GTS|VTS|STS)", _apply_test_type, ()),
    ("只烧写不测试", r"只\s*烧写|仅\s*烧写|不(跑|执行|做)测试|先\s*只?\s*烧写", _apply_flash_only, ()),
    ("去掉 AOA", r"(去掉|删除|移除|取消)\s*AOA", _apply_drop_aoa, ("aoa_profile", "aoa_requested")),
    ("换 Worker", r"(?:换|切换到|使用)\s*worker\s*[:：=]?\s*([A-Za-z0-9_.\-]+)", _apply_worker, ()),
]


def update_draft(draft: dict[str, Any], message: str) -> tuple[dict[str, Any], list[str]]:
    """把「重新规划」的修改意见套用到现有草案，保留已解析实体。"""
    applied: list[str] = []
    constraint_resets: set[str] = set()
    text = message or ""
    for _desc, pattern, apply, resets in _DRAFT_UPDATE_RULES:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        apply(draft, match)
        applied.append(_desc)
        constraint_resets.update(resets)
    if applied:
        draft["plan_version"] = int(draft.get("plan_version") or 1) + 1
        draft["status"] = "draft"
        # 用户显式修改是约束变更本身：同步冻结约束（新版本），后续轮次
        # 以新约束为准，模型不得再回退到旧值。未触及约束字段的规则
        # （如换 Worker）不 bump 约束版本。显式删除（去掉 AOA）同时重置
        # 冻结键：撤回的要求不再约束重新规划；版本号随 merged 递增，
        # 变更历史仍然可审计。
        merged = freeze_user_constraints(
            _entities_from_draft(draft), draft.get("user_constraints")
        )
        for key in constraint_resets:
            merged.pop(key, None)
        previous = {
            key: value for key, value in (draft.get("user_constraints") or {}).items()
            if key != "version"
        }
        current = {key: value for key, value in merged.items() if key != "version"}
        if current != previous:
            draft["user_constraints"] = merged
    return draft, applied


def _entities_from_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """从草案反向读出实体视图，供约束同步使用。"""
    device = draft.get("device") or {}
    firmware = draft.get("firmware") or {}
    aoa = draft.get("aoa") or {}
    test = draft.get("test") or {}
    serial = str(device.get("serial") or "")
    return {
        "device_serials": [serial] if serial else [],
        "firmware_share_id": str(firmware.get("share_id") or ""),
        "aoa_profile": str(aoa.get("profile") or ""),
        "aoa_requested": aoa.get("mode") == "user_build_recovery",
        "test_type": str(test.get("type") or ""),
        "flash_only": (test.get("scope") or "") == "skip",
        "connect_wifi": bool(test.get("connect_wifi")),
        "test_module": str(test.get("module") or ""),
        "test_case": str(test.get("case") or ""),
    }


# ==================== 阻塞与摘要 ====================

def draft_blocks(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """结构性缺失即阻塞：序列号、固件来源、AOA 配置名。"""
    blocks: list[dict[str, Any]] = []
    device = draft.get("device") or {}
    if not str(device.get("serial") or ""):
        blocks.append({
            "stage": "device",
            "reason": "未识别到设备序列号。请用「设备 <序列号>」明确指定，系统不会自动替换成其他设备。",
        })
    firmware = draft.get("firmware") or {}
    if not str(firmware.get("share_id") or "") and not str(firmware.get("url") or ""):
        blocks.append({
            "stage": "firmware",
            "reason": "未识别到固件来源。请提供固件分享链接（/api/firmware-shares/<id>/download）。",
        })
    elif not str(firmware.get("share_id") or "") and str(firmware.get("url") or ""):
        # 非 平台分享 链接的固件 URL 无法暂存与校验，明确拒绝而非静默通过预检。
        blocks.append({
            "stage": "firmware",
            "reason": "固件来源必须是平台固件分享链接（/api/firmware-shares/<id>/download）；"
                      "外部 URL 无法暂存与校验，请先在固件分享页上传。",
        })
    aoa = draft.get("aoa") or {}
    if aoa.get("mode") == "user_build_recovery" and not str(aoa.get("profile") or ""):
        blocks.append({
            "stage": "aoa",
            "reason": "用户要求 AOA 恢复但未指定配置名：请提供 AOA Profile 名称"
                      "（不会静默关闭 AOA）。",
        })
    test = draft.get("test") or {}
    if test.get("connect_wifi") and (test.get("scope") or "full") == "skip":
        blocks.append({
            "stage": "test",
            "reason": "「启用 Wi-Fi」依赖测试阶段执行（preparations）；纯烧写工作流"
                      "不执行测试，请去掉 Wi-Fi 要求或允许执行测试。",
        })
    return blocks


def summarize_draft(draft: dict[str, Any], preflight: dict[str, Any] | None = None) -> str:
    device = draft.get("device") or {}
    firmware = draft.get("firmware") or {}
    aoa = draft.get("aoa") or {}
    test = draft.get("test") or {}
    lines = [
        f"已解析为多阶段工作流（计划 v{draft.get('plan_version', 1)}）：",
        f"- Worker: {device.get('worker_id') or '待全局设备库存解析'}",
        f"- 设备: {device.get('serial') or '未识别（阻塞）'}",
        f"- 固件: firmware_share {firmware.get('share_id') or '未识别（阻塞）'}",
        f"- AOA: {aoa.get('mode') or 'off'}" + (f" / {aoa.get('profile')}" if aoa.get("profile") else ""),
        f"- 测试: {test.get('type') or '未指定'} / 范围 {test.get('scope') or 'full'}",
    ]
    if test.get("module"):
        lines.append(f"- 测试模块: {test['module']}")
    if int(test.get("retry_count") or 0):
        lines.append(f"- 失败 retry: {test['retry_count']} 次")
    if test.get("connect_wifi"):
        lines.append("- 测试前启用 Wi-Fi（仅启用，不保证连接指定网络）")
    if preflight:
        for item in preflight.get("checks") or []:
            status = "通过" if item.get("ok") else "阻塞"
            lines.append(f"- 预检[{item.get('stage')}]: {status}" + (f" — {item.get('reason')}" if item.get("reason") else ""))
    blocks = (preflight or {}).get("blocks") or draft_blocks(draft)
    if preflight and not preflight.get("ready") and not blocks:
        blocks = [{"reason": "资源预检尚未通过"}]
    if blocks:
        lines.append("存在阻塞项，暂不能执行：")
        for block in blocks:
            lines.append(f"  - {block['reason']}")
        lines.append("解决后点击“重新规划”刷新预检。")
    else:
        lines.append("输入“确认执行”或点击执行按钮后启动。")
    return "\n".join(lines)


# ==================== 确定性预检 ====================

def preflight_draft(
    draft: dict[str, Any],
    *,
    share_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    device_lookup: Callable[[str, str | None], dict[str, Any] | None] | None = None,
    aoa_lookup: Callable[[str, str], dict[str, Any] | None] | None = None,
    suite_lookup: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """对草案逐阶段做只读校验，返回 checks 与 can_execute。

    lookup 返回 ``None`` 视为该阶段不可满足；返回 dict 时可携带
    ``reason`` 覆盖默认提示。所有查询都是只读，不锁定资源。
    """
    checks: list[dict[str, Any]] = []
    device = draft.get("device") or {}
    firmware = draft.get("firmware") or {}
    aoa = draft.get("aoa") or {}
    test = draft.get("test") or {}

    # 1. 固件来源：只认平台固件分享（受控来源）；URL-only 一律不可预检，
    # 与 draft_blocks 同一语义（外部 URL 无法暂存与校验）。
    share_id = str(firmware.get("share_id") or "")
    if share_id and share_lookup:
        record = share_lookup(share_id) or {}
        if record.get("ok"):
            checks.append({"stage": "firmware", "ok": True, "detail": record.get("detail", "分享有效")})
        else:
            checks.append({
                "stage": "firmware", "ok": False,
                "reason": record.get("reason") or f"固件分享 {share_id} 不可用",
            })
    elif not share_id:
        checks.append({
            "stage": "firmware", "ok": False,
            "reason": (
                "缺少可执行的固件来源：仅识别平台固件分享链接"
                "（/api/firmware-shares/<id>/download）"
                if str(firmware.get("url") or "")
                else "缺少固件来源"
            ),
        })
    else:
        checks.append({"stage": "firmware", "ok": True, "detail": "待执行时暂存"})

    # 2. 设备定位（跨 Worker，仅确认存在与占用，不替换设备）
    serial = str(device.get("serial") or "")
    if serial and device_lookup:
        info = device_lookup(serial, device.get("worker_id")) or {}
        if info.get("ok"):
            resolved_worker = str(info.get("worker_id") or "")
            if resolved_worker:
                draft["device"]["worker_id"] = resolved_worker
            checks.append({"stage": "device", "ok": True, "detail": info.get("detail", "设备已定位")})
        else:
            checks.append({
                "stage": "device", "ok": False,
                "reason": info.get("reason") or f"设备 {serial} 未在集群中定位到",
            })
    elif not serial:
        checks.append({"stage": "device", "ok": False, "reason": "未指定设备序列号"})
    else:
        checks.append({"stage": "device", "ok": True, "detail": "待执行时定位"})

    # 3. AOA 预检（配置存在且属于当前账号）
    if aoa.get("mode") == "user_build_recovery":
        profile = str(aoa.get("profile") or "")
        if profile and aoa_lookup:
            found = aoa_lookup(profile, str(draft.get("workspace", {}).get("owner_id") or "")) or {}
            if found.get("ok"):
                checks.append({"stage": "aoa", "ok": True, "detail": found.get("detail", "AOA 配置可用")})
            else:
                checks.append({
                    "stage": "aoa", "ok": False,
                    "reason": found.get("reason") or f"AOA 配置 {profile} 不存在或不属于当前账号",
                })
        elif not profile:
            checks.append({"stage": "aoa", "ok": False, "reason": "用户要求 AOA 恢复但缺少配置名（不会静默关闭 AOA）"})
        else:
            checks.append({"stage": "aoa", "ok": True, "detail": "待执行时校验"})
    else:
        checks.append({"stage": "aoa", "ok": True, "detail": "未启用"})

    # 4. 测试预检（scope=skip 时跳过；suite 在同一 Worker 上匹配并冻结进
    #    草案，提交时以 test_suite 精确绑定，不再由执行期重新模糊选择）
    if (test.get("scope") or "full") != "skip":
        test_type = str(test.get("type") or "").upper()
        if test_type and suite_lookup:
            suite = suite_lookup(test_type, str(draft.get("device", {}).get("worker_id") or "")) or {}
            if suite.get("ok"):
                tools_path = str(suite.get("tools_path") or "")
                if tools_path:
                    test["test_suite"] = tools_path
                checks.append({"stage": "test", "ok": True, "detail": suite.get("detail", "套件可用")})
            else:
                checks.append({
                    "stage": "test", "ok": False,
                    "reason": suite.get("reason") or f"没有匹配 {test_type} 的可用测试套件",
                })
        elif not test_type:
            checks.append({"stage": "test", "ok": False, "reason": "未识别测试类型"})
        else:
            checks.append({"stage": "test", "ok": True, "detail": "待执行时匹配"})
    else:
        checks.append({"stage": "test", "ok": True, "detail": "本次只烧写，不执行测试"})

    structural_blocks = draft_blocks(draft)
    blocked_checks = [item for item in checks if not item.get("ok")]
    return {
        "ready": not structural_blocks and not blocked_checks,
        "checks": checks,
        "blocks": structural_blocks + [
            {"stage": item["stage"], "reason": item.get("reason") or ""}
            for item in blocked_checks
        ],
    }


def is_self_referential_url(url: str, own_origin: str) -> bool:
    """判断固件 URL 是否指向平台自身的 firmware-shares 下载端点。"""
    try:
        parsed = urlparse(url or "")
        own = urlparse(own_origin or "")
    except ValueError:
        return False
    return (
        bool(parsed.path)
        and _SHARE_ID_RE.search(parsed.path) is not None
        and (not own.netloc or parsed.netloc == own.netloc)
    )
