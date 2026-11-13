"""SDK 子仓检索优先级排序（强相关先搜）。

全量发现模式下一个 SDK 有上千个子仓，全库 grep 受并行分片与 100s 超时
约束；本模块把有限的检索预算集中在强相关子仓上：

1. **知识锚点信号**（注入，可选）：android-internals wiki（ADR 0014）的
   ``KnowledgeHit.source_anchors`` 自带上游 repo/path 锚点，机制章节
   （chapter）给出机制名——bootstrap 经 :func:`configure_knowledge_hints`
   注入 ``query -> [(path_prefix, weight)]`` 提取器；wiki 是 background
   级知识，**只用于排序提示，不参与证据判定**（证据语义不变）。
2. **路径词法信号**（内置）：query 分词与子仓相对路径的目录/文件名
   token 匹配（如 ``surfaceflinger`` → ``frameworks/native/services/
   surfaceflinger``）。
3. **Android 子系统映射**（内置静态领域知识，非服务器绑定）：binder/
   lmkd/audio/camera/wifi 等机制关键词 → 典型子仓路径前缀。

输出按得分降序的 ``(repo_rel, score, reason)`` 列表；搜索侧据此排序
分片（强相关仓集中在首批分片）并在预算内提前收敛。
"""

from __future__ import annotations

import re


# 机制/子系统关键词 → 典型子仓路径前缀（大小写不敏感，子串匹配 query）。
# 领域静态知识；命中给固定权重，多关键词命中叠加。
_SUBSYSTEM_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("binder", "hwbinder", "binderfs", "parcel"),
        ("frameworks/native/libs/binder", "frameworks/native/libs", "drivers/", "frameworks/av/media"),
    ),
    (
        ("lmkd", "lowmemorykiller", "oom", "psi", "memcg", "activitymanager", "am_kill", "kill"),
        ("system/memory/lmkd", "frameworks/base/services/core/java/com/android/server/am"),
    ),
    (
        ("surfaceflinger", "composer", "vsync", "gralloc", "hwcomposer", "display", "graphics"),
        ("frameworks/native/services/surfaceflinger", "frameworks/native/libs/ui",
         "frameworks/native/libs/gui", "hardware/rockchip", "vendor/rockchip"),
    ),
    (
        ("audio", "alsa", "tinyalsa", "audiopolicy", "audioflinger", "sound"),
        ("frameworks/av/audio", "frameworks/av/media", "frameworks/av/services/audioflinger",
         "vendor/rockchip", "hardware/rockchip"),
    ),
    (
        ("camera", "hal3", "isp"),
        ("frameworks/av/camera", "frameworks/av/services/camera", "vendor/rockchip",
         "hardware/rockchip", "packages/apps/camera"),
    ),
    (
        ("wifi", "wlan", "wpa", "p2p"),
        ("frameworks/opt/net/wifi", "external/wpa_supplicant_8", "vendor/rockchip", "kernel"),
    ),
    (
        ("bluetooth", "hci", "bt_"),
        ("packages/apps/bluetooth", "system/bt", "external/", "vendor/rockchip", "kernel"),
    ),
    (
        ("power", "suspend", "wakeup", "battery"),
        ("frameworks/base/services/core/java/com/android/server/power", "system/power",
         "hardware/interfaces/power", "kernel"),
    ),
    (
        ("thermal", "temperature"),
        ("frameworks/native/services/thermalservice", "hardware/interfaces/thermal", "kernel"),
    ),
    (
        ("usb", "gadget", "adb", "aoa"),
        ("frameworks/base/services/usb", "system/core/adb", "kernel", "packages/apps/usb"),
    ),
    (
        ("media", "codec", "decoder", "encoder", "omx", "c2", "codec2", "mpeg"),
        ("frameworks/av/media", "frameworks/av/services", "vendor/rockchip", "hardware/rockchip"),
    ),
    (
        ("keymaster", "keystore", "attestation"),
        ("system/security", "system/keymaster", "external/", "hardware/interfaces/keymaster"),
    ),
    (
        ("sepolicy", "selinux", "avc", "denied"),
        ("system/sepolicy", "device/rockchip", "vendor/rockchip", "system/core/init"),
    ),
    (
        ("recovery", "ota", "update_engine", "bootloader"),
        ("bootable/recovery", "system/update_engine", "frameworks/base/services/core/java/com/android/server/updater"),
    ),
    (
        ("input", "touch", "keyevent", "inputreader", "inputdispatcher"),
        ("frameworks/native/services/inputflinger", "frameworks/base/core/java/android/view", "kernel"),
    ),
    (
        ("storage", "vold", "f2fs", "ext4", "mount", "mmc"),
        ("system/vold", "system/core/fs_mgr", "kernel", "system/core/init"),
    ),
    (
        ("netd", "ethernet", "network", "connectivity", "dhcp"),
        ("system/netd", "frameworks/opt/net/ethernet", "frameworks/base/services/core/java/com/android/server/connectivity", "kernel"),
    ),
    (
        ("gnss", "gps", "location"),
        ("frameworks/base/location", "hardware/interfaces/gnss", "vendor/rockchip"),
    ),
    (
        ("cts", "vts", "gtest", "instrumentation"),
        ("cts", "test/vts", "frameworks/base", "kernel"),
    ),
    (
        ("dvr", "tv", "tuner", "hdmi"),
        ("frameworks/base/media/java/android/media/tv", "frameworks/av", "vendor/rockchip"),
    ),
)

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9_]+")
# 排除常见噪声 token（语言关键字/泛词，对仓库定位无意义）。
_TOKEN_STOPWORDS = frozenset({
    "test", "tests", "java", "cpp", "c", "h", "py", "android", "androidx",
    "the", "and", "for", "with", "this", "that", "error", "fail", "failed",
    "failure", "assert", "expect", "return", "void", "null", "true", "false",
    "public", "private", "static", "class", "method", "function", "line",
    "service", "manager", "system", "device", "framework", "core", "main",
})

_KNOWLEDGE_HINT_PROVIDER = None
_CODESEARCH_HINT_PROVIDER = None

# 巨型预构建仓：全库 grep 实测秒级~90s+（clang 工具链），对机制/根因分析
# 价值极低；排序时压到最低优先（仍可被 path_filter 显式选中检索）。
_LOW_VALUE_REPO_PREFIXES = ("prebuilts/",)


def _low_value_penalty(repo_rel: str) -> float:
    return -2.0 if repo_rel.lower().startswith(_LOW_VALUE_REPO_PREFIXES) else 0.0


def configure_knowledge_hints(
    provider=None,
) -> None:
    """注入 knowledge → 仓前缀提示提取器：``(query) -> [(prefix, weight)]``。

    ``None`` 撤销注入（回到纯内置排序）。提取器内部异常由调用方吞掉，
    绝不能让排序失败阻塞检索。
    """
    global _KNOWLEDGE_HINT_PROVIDER
    _KNOWLEDGE_HINT_PROVIDER = provider


def configure_codesearch_hints(provider=None) -> None:
    """注入 codesearch（OpenGrok）→ 仓前缀提示提取器。

    提取器签名同为 ``(query) -> [(prefix, weight)]``；bootstrap 侧用
    ``ReportAnalyzer().rk_codesearch`` 实现：符号在代码索引中的真实落点
    是最强的"哪个模块"先验，权重高于静态映射。异常纪律同 knowledge。
    """
    global _CODESEARCH_HINT_PROVIDER
    _CODESEARCH_HINT_PROVIDER = provider


def _path_tokens(path: str) -> set[str]:
    return {token for token in _TOKEN_SPLIT.split(path.lower()) if token}


def _repo_prefix_score(repo_rel: str, prefix: str) -> float:
    """路径前缀匹配得分：整段前缀 3.0，逐段命中按深度衰减。"""
    repo_rel = repo_rel.lower()
    prefix = prefix.lower().rstrip("/")
    if not prefix or prefix == "/":
        return 0.0
    if repo_rel == prefix:
        return 3.0
    if repo_rel.startswith(prefix + "/"):
        return 2.5
    # 前缀部分重合（如 knowledge 给 frameworks/base 而仓是
    # frameworks/base/services/core）：按前缀段数给分。
    segs = prefix.split("/")
    for depth in range(len(segs), 0, -1):
        partial = "/".join(segs[:depth])
        if repo_rel.startswith(partial + "/") or repo_rel == partial:
            return 1.0 + 0.25 * depth
    return 0.0


def rank_repos(
    query: str,
    repo_rels: list[str] | tuple[str, ...],
    *,
    knowledge_hints: list[tuple[str, float]] | None = None,
    codesearch_hints: list[tuple[str, float]] | None = None,
) -> list[tuple[str, float, str]]:
    """对子仓清单按与 query 的强相关性排序，返回 (repo_rel, score, reason)。

    分值来源（可叠加）：
    - ``codesearch:`` OpenGrok 符号命中路径（真实落点，最强先验）；
    - ``anchor:`` knowledge 锚点前缀命中（调用方保证权重 >= 2）；
    - ``path:`` query token 与仓路径 token 重合（每 token +1.5）；
    - ``subsystem:`` 内置机制关键词映射命中（每关键词 +1.2）。

    得分为 0 的仓保持原顺序排在最后（仍会参与检索，只是优先级最低）。
    """
    query_tokens = [
        token for token in _TOKEN_SPLIT.split(str(query or "").lower())
        if len(token) >= 3 and token not in _TOKEN_STOPWORDS
    ]
    hint_scores: dict[str, float] = {}
    hint_sources: dict[str, str] = {}
    for hints, source_label in (
        (codesearch_hints, "codesearch"),
        (knowledge_hints, "anchor"),
    ):
        for prefix, weight in hints or ():
            if not prefix:
                continue
            if float(weight) >= hint_scores.get(prefix, 0.0):
                hint_scores[prefix] = float(weight)
                hint_sources[prefix] = source_label
    subsystem_hits: list[str] = []
    for keywords, prefixes in _SUBSYSTEM_HINTS:
        if any(keyword in str(query or "").lower() for keyword in keywords):
            subsystem_hits.extend(prefixes)

    scored: list[tuple[str, float, str]] = []
    for repo_rel in repo_rels:
        score = 0.0
        best_reason = ""
        best_contribution = 0.0
        for prefix, weight in hint_scores.items():
            hit = _repo_prefix_score(repo_rel, prefix)
            if hit > 0:
                contribution = hit * weight
                score += contribution
                if contribution > best_contribution:
                    best_contribution = contribution
                    best_reason = f"{hint_sources[prefix]}:{prefix}"
        repo_tokens = _path_tokens(repo_rel)
        for token in query_tokens:
            if token in repo_tokens:
                score += 1.5
                if best_contribution < 1.5:
                    best_contribution = 1.5
                    best_reason = f"path:{token}"
        for prefix in subsystem_hits:
            hit = _repo_prefix_score(repo_rel, prefix)
            if hit > 0:
                contribution = hit * 1.2
                score += contribution
                if contribution > best_contribution:
                    best_contribution = contribution
                    best_reason = f"subsystem:{prefix}"
        score += _low_value_penalty(repo_rel)
        if score > 0:
            scored.append((repo_rel, round(score, 3), best_reason))
        else:
            scored.append((repo_rel, 0.0, ""))
    # 稳定排序：得分降序，同分保持原清单顺序（原清单已按字典序）。
    ordered = sorted(enumerate(scored), key=lambda item: (-item[1][1], item[0]))
    return [
        (repo_rel, score, reason)
        for _, (repo_rel, score, reason) in ordered
    ]


def knowledge_hints_for_query(query: str) -> list[tuple[str, float]]:
    """经注入的提取器获取 knowledge 锚点提示；任何异常都降级为空。"""
    return _hints_via_provider(_KNOWLEDGE_HINT_PROVIDER, query)


def codesearch_hints_for_query(query: str) -> list[tuple[str, float]]:
    """经注入的提取器获取 codesearch 符号落点提示；任何异常都降级为空。"""
    return _hints_via_provider(_CODESEARCH_HINT_PROVIDER, query)


def _hints_via_provider(provider, query: str) -> list[tuple[str, float]]:
    if provider is None:
        return []
    try:
        hints = provider(str(query or ""))
    except Exception:
        return []
    if not isinstance(hints, list):
        return []
    cleaned: list[tuple[str, float]] = []
    for item in hints:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        prefix = str(item[0] or "").strip()
        try:
            weight = float(item[1])
        except (TypeError, ValueError):
            continue
        if prefix and weight > 0:
            cleaned.append((prefix, weight))
    return cleaned
