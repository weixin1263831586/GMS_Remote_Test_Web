"""Bounded clipboard image validation and read-only visual analysis."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import re

from PIL import Image, ImageOps

from features.kkagent import UniversalAIAnalyzer
from features.users import runtime as users_runtime
from foundation.responses import error_response, success_response

from .session_log import _append_message, _append_step, _settle_step, session_payload
from .session_store import save_session_images


MAX_IMAGES = 3
MAX_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 16_000_000
DATA_URL = re.compile(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", re.ASCII)
_formats = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}
# Bound concurrent decoders and inference calls; never queue unbounded image bodies.
_slots = asyncio.Semaphore(2)


def prepare_images(values: list[str]) -> list[str]:
    """Decode actual image bytes, reject oversized/animated files, strip metadata."""
    if not 1 <= len(values) <= MAX_IMAGES:
        raise ValueError("每次最多发送 3 张图片")
    if sum(len(value) for value in values) > ((MAX_BYTES + 2) // 3) * 4 + 128:
        raise ValueError("图片总大小不能超过 4 MB")
    result = []
    total = 0
    encoded_total = 0
    for value in values:
        match = DATA_URL.fullmatch(value)
        if not match:
            raise ValueError("仅支持 PNG、JPEG、WebP 图片")
        kind, payload = match.groups()
        try:
            raw = base64.b64decode(payload, validate=True)
            total += len(raw)
            if total > MAX_BYTES:
                raise ValueError("图片总大小不能超过 4 MB")
            with Image.open(io.BytesIO(raw)) as source:
                if source.format != _formats[kind] or source.width * source.height > MAX_PIXELS:
                    raise ValueError("图片格式或分辨率不符合要求")
                if getattr(source, "n_frames", 1) != 1:
                    raise ValueError("不支持动态图片")
                source.load()
                pixels = ImageOps.exif_transpose(source)
                # Preserve palette transparency when normalizing screenshots.
                transparent = "A" in pixels.getbands() or "transparency" in pixels.info
                if kind == "jpeg":
                    pixels = pixels.convert("RGB")
                    mime = "jpeg"
                    fmt = "JPEG"
                else:
                    pixels = pixels.convert("RGBA" if transparent else "RGB")
                    mime = "png"
                    fmt = "PNG"
                pixels.info.clear()
                buffer = io.BytesIO()
                pixels.save(buffer, format=fmt, **({"quality": 92} if fmt == "JPEG" else {}))
                normalized = buffer.getvalue()
                encoded_total += len(normalized)
                if encoded_total > MAX_BYTES:
                    raise ValueError("图片处理后超过 4 MB，请裁剪或缩小图片")
        except (OSError, binascii.Error, Image.DecompressionBombError, OverflowError) as exc:
            raise ValueError("图片内容无效或无法解码") from exc
        result.append(f"data:image/{mime};base64," + base64.b64encode(normalized).decode("ascii"))
    return result


async def analyze_image_turn(session: dict, message: str, values: list[str]):
    """Analyze only this turn's images; never run operational tools."""
    if session.get("agent_image_busy") or session.get("status") in {"thinking", "running", "monitoring"} or session.get("pending_plan"):
        return error_response("当前会话有未完成的任务或计划，请完成后再发送图片", 409)
    if _slots.locked():
        return error_response("图片分析繁忙，请稍后重试", 409)
    prior_status = session.get("status") or "idle"
    session["agent_image_busy"] = True
    session["status"] = "thinking"
    step_id = None
    try:
        async with _slots:
            try:
                images = await asyncio.to_thread(prepare_images, values)
            except ValueError as exc:
                return error_response(str(exc), 400)
            if session.get("status") != "thinking":
                return error_response("图片分析已取消", 409)
            analyzer = UniversalAIAnalyzer(users_runtime.config_manager.get_ai_config())
            if not analyzer.image_provider():
                return error_response("未配置支持图片输入的本地模型（capabilities: image_in 或 vision），图片尚未发送", 503)
            _append_step(session, "图片分析", "running", f"正在分析 {len(images)} 张图片")
            step_id = session["steps"][-1]["id"]
            retrieval_context = []
            if message.strip():
                try:
                    from features.knowledge import retrieve_public_cdd_context

                    retrieval_context = await asyncio.to_thread(retrieve_public_cdd_context, message)
                except Exception:
                    # Public background recall must not prevent the local image analysis.
                    retrieval_context = []
            if session.get("status") != "thinking":
                return error_response("图片分析已取消", 409)
            options = {"retrieval_context": retrieval_context} if retrieval_context else {}
            result = await asyncio.to_thread(analyzer.generate_image_analysis, message, images, **options)
            step = next((item for item in session.get("steps", []) if item["id"] == step_id), None)
            if session.get("status") != "thinking":
                if step:
                    step.update(status="warning", detail="图片分析已取消")
                    _settle_step(step)
                return error_response("图片分析已取消", 409)
            if not result.get("success"):
                if step:
                    step.update(status="error", detail="视觉模型调用失败")
                    _settle_step(step)
                return error_response("视觉模型调用失败，请检查模型配置或重试；图片未转发到其他模型", 502)
            try:
                attachments = save_session_images(session, images)
            except (OSError, ValueError):
                if step:
                    step.update(status="error", detail="图片保存失败")
                    _settle_step(step)
                return error_response("图片保存失败或会话存储已达上限，请检查存储或新建会话", 503)
            if step:
                step.update(status="done", detail=f"已分析 {len(images)} 张图片")
                _settle_step(step)
            _append_message(session, "user", message,
                            data={"image_count": len(images), "image_attachments": attachments})
            _append_message(session, "assistant", result["content"], data={"image_analysis": True, "retrieval_context": {"scope": "public_cdd_only",
                             "coverage_complete": False,
                             "references": [item["reference"] for item in retrieval_context]}})
            session["status"] = prior_status
            session.pop("agent_image_busy", None)
            return success_response({"session": session_payload(session)}, "Agent updated")
    finally:
        if step_id:
            unfinished = next((item for item in session.get("steps", []) if item["id"] == step_id), None)
            if unfinished and unfinished.get("status") == "running":
                unfinished.update(status="error", detail="图片分析未完成")
                _settle_step(unfinished)
        session.pop("agent_image_busy", None)
        if session.get("status") == "thinking":
            session["status"] = prior_status
