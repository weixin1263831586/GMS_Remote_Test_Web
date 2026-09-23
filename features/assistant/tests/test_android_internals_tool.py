"""android_internals_search 工具链路回归（ADR 0014 version-aware 补全）。

覆盖两条评审发现的断裂：

1. tool schema 缺 ``android_api_level``（API/CLI/MCP 均支持而 assistant 缺）；
2. ``_build_call_kwargs`` 只绑定 ``req``/``body`` 形参，而
   ``external_api.search_external`` 的请求体形参名是 ``payload``，
   经 executor_ref 直调必现缺参。
"""

from __future__ import annotations

import unittest

from fastapi import Depends, HTTPException

from features.assistant.executor import ActionExecutor
from features.assistant.knowledge_tools import knowledge_agent_tools


def _tool():
    return next(t for t in knowledge_agent_tools() if t.name == "android_internals_search")


class AndroidInternalsToolSchemaTests(unittest.TestCase):
    def test_schema_exposes_android_api_level(self):
        params = {p["name"]: p for p in _tool().params}
        self.assertIn("android_api_level", params)
        self.assertEqual(params["android_api_level"]["type"], "integer")
        self.assertFalse(params["android_api_level"]["required"])


class BuildCallKwargsBindingTests(unittest.TestCase):
    """executor_ref 直调 search_external 时 payload 形参必须绑定为请求模型。"""

    def setUp(self):
        import importlib

        self.executor = ActionExecutor()
        self._func = importlib.import_module(
            "features.knowledge.external_api"
        ).search_external

    def test_payload_bound_with_android_api_level(self):
        kwargs = self.executor._build_call_kwargs(
            self._func, _tool(), None,
            {"query": "lmkd", "limit": 3, "android_api_level": 36},
        )
        payload = kwargs["payload"]
        self.assertEqual(payload.android_api_level, 36)
        self.assertEqual(payload.limit, 3)

    def test_payload_defaults_without_optional_params(self):
        kwargs = self.executor._build_call_kwargs(
            self._func, _tool(), None, {"query": "binder"},
        )
        payload = kwargs["payload"]
        self.assertIsNone(payload.android_api_level)
        self.assertEqual(payload.sources, [])

    def test_payload_rejects_out_of_range_api_level(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            self.executor._build_call_kwargs(
                self._func, _tool(), None,
                {"query": "anr", "android_api_level": 5000},
            )

    def test_caller_cannot_supply_dependency_parameter(self):
        def guarded(_admin=Depends(lambda _request: None)):
            return _admin

        with self.assertRaises(HTTPException) as exc:
            self.executor._build_call_kwargs(
                guarded, _tool(), object(), {"_admin": None},
            )
        self.assertEqual(exc.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
