"""Bounded OCR text derivation for Redmine image attachments.

截图/图片附件在快照下载时派生成可检索文本，让晨报/单号分析的读取链路
（``gms_rt_redmine_artifact_read``，evidence_api 按 ``derived_text_path``
服务）不再依赖 MCP 图片载荷——2026-10 的 #656295/#655644 复盘里
"截图不可读 → missing_information" 是取证未闭环的主要来源，而平台早已
内置 bundled tesseract（tools/tesseract，含 chi_sim），只缺这一段管道。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from .evidence import _DERIVED_TEXT_MAX_BYTES
from .evidence_store import EvidenceStore


logger = logging.getLogger(__name__)

OCR_TIMEOUT_SECONDS = 20
OCR_MAX_IMAGE_PIXELS = 16_000_000


def extract_image_ocr_text(
    store: EvidenceStore,
    atomic_write: Callable[[Path, bytes], None],
    artifact_id: str,
    snapshot_rel: str,
    image_path: Path,
) -> tuple[str, str]:
    """对图片附件做 best-effort OCR，产出可检索派生文本。

    返回 ``(derived_rel, error)``，签名与 zip/archive 派生函数一致。
    语义分两档：OCR 正常执行但图片本身无文字（空结果）返回
    ``("", "")``——附件保持 ready、只是没有可检索文本，这不是证据缺口
    （示意图/照片类截图是常态，翻 partial 会让快照噪声化）；OCR 依赖
    缺失、像素超限、识别超时或抛错返回 error，转 partial 并写进快照 errors，让
    missing_information 显性化（2026-10 #656295/#655644 复盘）。
    识别语言 chi_sim+eng（中文工单截图为主）；超限时按字节截断到
    _DERIVED_TEXT_MAX_BYTES。
    """
    try:
        import pytesseract
        from PIL import Image
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
            if image.width * image.height > OCR_MAX_IMAGE_PIXELS:
                return "", f"OCR 图片像素超过上限 {OCR_MAX_IMAGE_PIXELS}"
            text = str(pytesseract.image_to_string(
                image, lang="chi_sim+eng", timeout=OCR_TIMEOUT_SECONDS,
            ) or "")
    except Exception as exc:
        logger.debug("OCR failed for %s: %s", image_path, exc)
        detail = str(exc).strip().splitlines()
        summary = detail[-1][:200] if detail else type(exc).__name__
        return "", f"OCR 失败: {type(exc).__name__}: {summary}"
    if not text.strip():
        return "", ""
    # 按字节截断必须落在 UTF-8 字符边界上：直接切片可能把多字节汉字
    # 劈成非法序列，下游严格解码会失败。
    payload = (
        text.strip()
        .encode("utf-8")[:_DERIVED_TEXT_MAX_BYTES]
        .decode("utf-8", errors="ignore")
        .encode("utf-8")
    )
    derived_rel = f"{snapshot_rel}/derived/{artifact_id}.txt"
    try:
        atomic_write(store.resolve_internal(derived_rel), payload)
    except OSError as exc:
        return "", f"派生文本写入失败: {type(exc).__name__}"
    return derived_rel, ""
