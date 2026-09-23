"""adb-forward / burn-gsi 路由的语义状态码回归测试。

错误模型（根 AGENTS.md）：远端/设备侧失败映射 502，500 保留给未预期的
编程错误。回归背景（RK3588GMS7 GSI 烧写失败日志）：

* ``/api/burn/gsi`` 部分设备烧写失败（SSH 远端 fastboot 失败）→ 502；
* ``/api/adb-forward/start`` adb-hub 5037 初始化失败（RuntimeError）→ 502。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from starlette.requests import Request

from features.devices import adb_forward_api
from features.firmware import gsi_sn_burn


class _JSONBody:
    def __init__(self, payload: dict, status_code: int):
        self._payload = payload
        self.status_code = status_code

    @property
    def body(self) -> dict:
        import json

        return json.loads(json.dumps(self._payload))


class AdbForwardStartStatusTests(unittest.TestCase):
    def _start(self, exc: Exception | None = None, manager_result: dict | None = None):
        body = adb_forward_api.ADBForwardStartRequest(
            device_host="127.0.0.1",
        )
        request = Request({"type": "http", "method": "POST", "headers": []})
        with (
            patch.object(
                adb_forward_api.adb_forward_manager,
                "config_manager",
            ) as fake_config,
            patch.object(
                adb_forward_api.adb_forward_manager, "start_forward"
            ) as fake_start,
        ):
            fake_config.load_config.return_value = {"device_host": "127.0.0.1"}
            if exc is not None:
                fake_start.side_effect = exc
            else:
                fake_start.return_value = manager_result or {}
            response = asyncio.run(adb_forward_api.start_adb_forward(request, body))

        if isinstance(response, _JSONBody):
            return response.status_code
        return getattr(response, "status_code", None)

    def test_runtime_error_maps_to_502(self):
        # adb-hub 未能在5037端口完成ADB协议初始化 → RuntimeError 冒泡。
        status = self._start(
            exc=RuntimeError(
                "adb-hub 未能在5037端口完成ADB协议初始化: protocol fault"
            )
        )
        self.assertEqual(status, 502)

    def test_manager_failure_maps_to_502(self):
        status = self._start(
            manager_result={"success": False, "error": "SSH 隧道启动失败"}
        )
        self.assertEqual(status, 502)

    def test_unexpected_error_stays_500(self):
        status = self._start(exc=TypeError("'NoneType' object is not subscriptable"))
        self.assertEqual(status, 500)


class GsiBurnPartialFailureStatusTests(unittest.TestCase):
    def test_error_response_partial_failure_uses_502(self):
        """烧写经 SSH 在远端执行：部分失败必须返回 502 而非 500。

        直接断言 gsi_sn_burn 源码中的 status_code（路由全链路依赖大量
        runtime/ssh 状态，mock 成本高；这里锁定关键映射不被回退）。
        """
        import inspect

        source = inspect.getsource(gsi_sn_burn)
        self.assertIn("部分设备烧写失败", source)
        # error_response 调用携带 status_code=502
        self.assertRegex(
            source,
            r"部分设备烧写失败[\s\S]{0,300}status_code=502",
        )

    def test_unexpected_gsi_error_is_500(self):
        """外层兜底仍保留 500：str(e) 的裸 error_response 默认 500。"""
        import inspect

        source = inspect.getsource(gsi_sn_burn)
        self.assertIn("return error_response(str(e), 500)", source)


if __name__ == "__main__":
    unittest.main()
