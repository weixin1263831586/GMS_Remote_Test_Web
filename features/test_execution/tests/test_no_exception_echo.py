"""异常字符串不得回显给 API 客户端(全局深度审核回归)。

foundation/errors.py 明文约定:意外异常的文本可能含文件路径、命令文本、
凭据,必须留在服务端日志,客户端只收通用消息。test_execution 曾在多处
``except Exception`` 里 ``error_response(str(e), 500)`` 直接回显。
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.test_execution import status_api


_MARKER = "/srv/secret/path/to/internal.py"


class NoInternalExceptionEchoTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_error_returns_generic_message(self):
        request = SimpleNamespace(
            query_params={"logs": "false"},
            app=SimpleNamespace(state=SimpleNamespace()),
        )
        with (
            patch.object(status_api.runtime, "generate_help_or_continue", return_value=None),
            patch.object(status_api.runtime, "get_client_id_from_request", return_value="alice"),
            patch.object(
                status_api, "get_or_create_user_state",
                side_effect=RuntimeError(f"db locked at {_MARKER}"),
            ),
        ):
            response = await status_api.get_status(request)

        payload = json.loads(response.body)
        self.assertFalse(payload.get("success", True))
        self.assertNotIn(_MARKER, json.dumps(payload))
        self.assertNotIn("db locked", json.dumps(payload))

    def test_source_never_echoes_unexpected_exception_text_in_500_handlers(self):
        """源码级回归:except Exception(意外异常)分支不得回显 str(e)。

        400 分支回显受控 ValueError(仓库自构造校验文案,如
        ``{label} must stay inside suites_path``)是设计内行为,不在
        本回归范围;500 分支必须回通用消息。
        """
        from pathlib import Path

        base = Path(status_api.__file__).parent
        offenders = []
        for path in base.glob("*.py"):
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "error_response(str(e)" not in line and 'error_response(f"{e!s}"' not in line:
                    continue
                # 只拦 500 回显(意外异常);str(exc)/400 分支是受控文案。
                context = "\n".join(lines[max(0, i - 3): i])
                if "status_code=500" in line or ", 500)" in line or (
                    "except Exception" in context and "500" in line
                ):
                    offenders.append(f"{path.name}:{i}")
        self.assertEqual(offenders, [], f"500 handler 回显异常文本: {offenders}")


if __name__ == "__main__":
    unittest.main()
