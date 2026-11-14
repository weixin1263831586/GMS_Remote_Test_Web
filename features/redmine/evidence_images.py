"""Complete OCR text derivation for Redmine image attachments.

截图/图片附件在快照下载时派生成可检索文本，让晨报/单号分析的读取链路
（``gms_rt_redmine_artifact_read``，evidence_api 按 ``derived_text_path``
服务）不再依赖 MCP 图片载荷——2026-10 的 #656295/#655644 复盘里
"截图不可读 → missing_information" 是取证未闭环的主要来源，而平台早已
内置 bundled tesseract（tools/tesseract，含 chi_sim），只缺这一段管道。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .evidence import (
    OCR_MAX_IMAGE_PIXELS,
    OCR_TIMEOUT_SECONDS,
    OCR_TOTAL_TIMEOUT_SECONDS,
)
from .evidence_store import EvidenceStore


logger = logging.getLogger(__name__)


def ocr_image_text(image, pytesseract) -> str:
    """Return OCR text with an explicit warning when tile coverage is partial."""
    text, note = _ocr_image_text(image, pytesseract)
    return f"{text}\n[{note}]" if note else text


def _ocr_image_text(image, pytesseract) -> tuple[str, str]:
    """Cover large images with overlapping tiles instead of rejecting them.

    单次 tesseract 调用受 OCR_TIMEOUT_SECONDS 约束；分片场景另有
    OCR_TOTAL_TIMEOUT_SECONDS 总预算——大图切成几十个 tile 时，逐片
    各给 120s 的总耗时可能到小时级，必须整体截断（已识别 tile 的文本
    保留，截断显式标注）。
    """
    deadline = time.monotonic() + OCR_TOTAL_TIMEOUT_SECONDS
    note = ""

    def recognize(region) -> str:
        nonlocal note
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            note = f"OCR truncated: total {OCR_TOTAL_TIMEOUT_SECONDS}s budget exceeded"
            return ""
        return str(pytesseract.image_to_string(
            region, lang="chi_sim+eng",
            timeout=min(OCR_TIMEOUT_SECONDS, remaining),
        ) or "")

    width, height = image.size
    if width * height <= OCR_MAX_IMAGE_PIXELS:
        text = recognize(image)
        return text, note
    tile_width = min(width, 4096, OCR_MAX_IMAGE_PIXELS)
    tile_height = max(1, OCR_MAX_IMAGE_PIXELS // tile_width)
    step_x = max(1, tile_width - min(64, tile_width // 4))
    step_y = max(1, tile_height - min(64, tile_height // 4))
    texts = []
    for top in range(0, height, step_y):
        for left in range(0, width, step_x):
            with image.crop((left, top, min(width, left + tile_width),
                             min(height, top + tile_height))) as tile:
                try:
                    texts.append(recognize(tile))
                except RuntimeError as exc:
                    if "timeout" not in str(exc).lower():
                        raise
                    note = f"OCR truncated: {exc}"
            if note:
                break
        if note:
            break
    text = "\n".join(texts)
    return text, note


def _load_ocr_dependencies() -> tuple[Any, Any]:
    """加载 OCR 依赖 ``(pytesseract, PIL.Image)``；失败由调用方降级。

    独立函数而非内联 import：无 OCR 依赖部署的降级测试注入此入口，
    避免测试操纵 sys.modules / builtins.__import__ 这类进程级全局状态
    （OCR 在工作线程执行，全局注入在组合运行下会互相污染）。
    """
    import pytesseract
    from PIL import Image

    return pytesseract, Image


def extract_image_ocr_text(
    store: EvidenceStore,
    atomic_write: Callable[[Path, bytes], None],
    artifact_id: str,
    snapshot_rel: str,
    image_path: Path,
) -> tuple[str, str]:
    """对图片附件做完整 OCR，产出可检索派生文本。

    返回 ``(derived_rel, error)``，与 zip/archive 派生结果一致。
    语义分三档（原件状态与派生状态分离——status 保持"原件保存成功即
    ready"，派生结果只落在 derived_status）：
    - OCR 正常执行但图片本身无文字（空结果）：返回 ``("", "")``，附件
      ready、derived_status=empty，不是证据缺口（示意图/照片类截图是
      常态，降级原件只会让快照噪声化）；
    - OCR 依赖缺失或识别抛错：返回 error，调用方
      （evidence_fetch._run_derivation）把 derived_status 记为 missing
      并把 derive 阶段错误写进快照 errors（拉低 completeness 的
      derived_text 维度），让 missing_information 显性化，原件保持
      ready（2026-10 #656295/#655644 复盘）。
    - 分块识别超时：保留已识别的文本并返回 error，派生状态记 partial；
      尚无文本产出则记 missing，不把截断标注当成识别结果。
    识别语言 chi_sim+eng；OCR 文本完整保存。
    """
    try:
        pytesseract, Image = _load_ocr_dependencies()
    except Exception as exc:
        return "", f"OCR 依赖缺失: {type(exc).__name__}"
    try:
        # bundled_tesseract_cmd()/configure_bundled_tesseract() 首调后缓存，
        # 每图检查代价可忽略；与 analysis_attachments 共用同一捆绑二进制。
        from .tesseract_finder import bundled_tesseract_cmd, configure_bundled_tesseract

        cmd = bundled_tesseract_cmd()
        if cmd:
            configure_bundled_tesseract()
            import pytesseract.pytesseract as _pt

            _pt.tesseract_cmd = cmd
        with Image.open(image_path) as image:
            text, note = _ocr_image_text(image, pytesseract)
    except Exception as exc:
        logger.debug("OCR failed for %s: %s", image_path, exc)
        detail = str(exc).strip().splitlines()
        summary = detail[-1][:200] if detail else type(exc).__name__
        return "", f"OCR 失败: {type(exc).__name__}: {summary}"
    if not text.strip():
        return "", note
    if note:
        text += f"\n[{note}]"
    payload = text.strip().encode("utf-8")
    derived_rel = f"{snapshot_rel}/derived/{artifact_id}.txt"
    try:
        atomic_write(store.resolve_internal(derived_rel), payload)
    except OSError as exc:
        return "", f"派生文本写入失败: {type(exc).__name__}"
    return derived_rel, note
