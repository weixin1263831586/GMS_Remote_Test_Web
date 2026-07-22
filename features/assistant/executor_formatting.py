"""Response decoding and browser-facing summaries for Agent tools."""

from __future__ import annotations

import json
import logging
from typing import Any

from .tools import AgentTool


logger = logging.getLogger(__name__)


def json_body(response) -> dict[str, Any]:
    """Extract JSON from a FastAPI response or model-like value."""
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if response is None:
        return {"success": False, "error": "Empty response"}
    try:
        body = getattr(response, "body", None)
        if body is None and hasattr(response, "render"):
            body = response.render(getattr(response, "content", None))
        if isinstance(body, memoryview):
            body = body.tobytes()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        if isinstance(body, str) and body.strip():
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": body.strip(),
                    "message": body.strip(),
                }
    except Exception as exc:
        logger.warning(
            "[Agent] Failed to parse JSON response from %s: %s",
            type(response).__name__,
            exc,
        )
    return {
        "success": False,
        "error": f"Invalid JSON response: {type(response).__name__}",
    }


def format_payload(tool: AgentTool, payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return str(payload["error"])
    if tool.name == "devices_info":
        return format_device_info_payload(payload)
    if payload.get("message"):
        return str(payload["message"])
    if "connected" in payload:
        state = "已连接/正常" if payload["connected"] else "未连接"
        return f"{tool.display_name}：{state}"
    data = payload.get("data", payload)
    if isinstance(data, dict):
        def first_list(*keys: str) -> list:
            for container in (data, payload):
                for key in keys:
                    value = container.get(key)
                    if isinstance(value, list):
                        return value
            return []

        results = first_list("results")
        if results:
            succeeded = sum(
                1
                for item in results
                if isinstance(item, dict) and item.get("success")
            )
            return f"{tool.display_name}完成：成功 {succeeded}/{len(results)}"
        reports = first_list("reports")
        if reports:
            return f"查询到 {len(reports)} 份报告。"
        devices = first_list("devices")
        if devices:
            return f"查询到 {len(devices)} 台设备。"
        items = first_list("items", "jobs", "workers", "docs")
        if items:
            return f"{tool.display_name}：查询到 {len(items)} 条记录。"
    return f"{tool.display_name}已完成。"


def format_device_info_payload(payload: dict[str, Any]) -> str:
    data = payload.get("data") or payload
    results = data.get("results") if isinstance(data, dict) else None
    if not results and isinstance(data, dict):
        properties = data.get("properties") or data.get("info")
        if isinstance(properties, dict):
            results = [
                {
                    "device": data.get("device")
                    or data.get("device_id")
                    or payload.get("device_id"),
                    "properties": properties,
                }
            ]
    if not isinstance(results, list) or not results:
        return payload.get("message") or "未返回设备详情。"
    preferred_fields = (
        "Model", "Android Version", "API Level", "SDK Version",
        "Serial Number", "Boot State", "Security Patch", "Build Type",
        "Build Tags", "Build Date", "Mali Version", "Total Memory",
        "Free Memory", "DATA Partition", "Timezone", "Language", "Fingerprint",
    )
    sections = []
    for item in results:
        if not isinstance(item, dict):
            continue
        device_id = (
            item.get("device")
            or item.get("device_id")
            or item.get("serial_no")
            or "Unknown"
        )
        properties = item.get("properties") or {}
        lines = [f"设备 {device_id} 详情："]
        for field_name in preferred_fields:
            value = properties.get(field_name)
            if value not in (None, ""):
                lines.append(f"- {field_name}: {value}")
        extra_fields = [
            (key, value)
            for key, value in properties.items()
            if key not in preferred_fields and value not in (None, "")
        ]
        lines.extend(f"- {key}: {value}" for key, value in extra_fields[:8])
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else "未返回设备详情。"
