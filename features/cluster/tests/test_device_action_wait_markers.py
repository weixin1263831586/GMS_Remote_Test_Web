"""Worker 命令失败回执中的结构化标记必须透传到 502 detail。

工作台的"标定应用缺失 → 一键部署引导"依赖 `_wait_for_command` 把
``calibration_app_missing`` 从命令 result 原样带进 HTTPException(502, detail)；
前端 aoaApi 再从 payload.detail 读取该标记。这里锁住这一跳的契约。
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from features.cluster.device_actions_api import _wait_for_command


def _failed_repository(result: dict, error: str):
    return SimpleNamespace(get_command=lambda command_id: {
        "id": command_id, "status": "failed", "error": error, "result": result,
    })


def test_calibration_app_missing_marker_surfaces_in_502_detail():
    repository = _failed_repository(
        {"calibration_app_missing": True, "display": [720, 1280]}, "缺少 AOA 安全标定应用")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(_wait_for_command(repository, "calibrate", {"id": "cmd-1", "payload": {}}))
    exc = raised.value
    assert exc.status_code == 502
    assert exc.detail["message"] == "缺少 AOA 安全标定应用"
    assert exc.detail["calibration_app_missing"] is True
    assert "reconcile_required" not in exc.detail


def test_reconcile_required_marker_surfaces_in_502_detail():
    repository = _failed_repository({"reconcile_required": True}, "device state drifted")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(_wait_for_command(repository, "calibrate", {"id": "cmd-2", "payload": {}}))
    assert raised.value.status_code == 502
    assert raised.value.detail["reconcile_required"] is True
    assert "calibration_app_missing" not in raised.value.detail


def test_failed_command_without_marker_keeps_plain_detail():
    repository = _failed_repository({"serial": "A", "error": "probe failed"}, "tap failed")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(_wait_for_command(repository, "calibrate", {"id": "cmd-3", "payload": {}}))
    assert raised.value.status_code == 502
    assert raised.value.detail == {"message": "tap failed"}
