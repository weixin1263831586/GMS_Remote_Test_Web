"""users_api（方案 2）权限与语义回归：全局组织架构写仅限管理员人工会话，
个人 overlay（自我绑定/别名）per-owner。"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException

from features.auth import CurrentUser
from features.redmine import org_chart, users_api
from features.redmine.org_chart import load_org_payload, load_user_overlay


def _request(owner: str, role: str = "user", auth_method: str = "session"):
    class State:
        pass

    class Req:
        state = State()

    request = Req()
    request.state.current_user = CurrentUser(
        id=owner, username=owner, role=role, resource_owner_id=owner)
    request.state.auth_method = auth_method
    request.state.credentials_rejected = False

    async def json_body():
        return {}

    request.json = json_body
    return request


class UsersApiPermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fake = type("S", (), {"project_root": self.root, "data_root": self.root})()
        # org_chart 与 users（owner_user_map_path 经 from-import 绑定在
        # org_chart 命名空间）两侧 settings 都指向临时目录。
        from features.redmine import users as redmine_users

        for target in (org_chart, redmine_users):
            patch.object(target, "settings", fake).start()
            self.addCleanup(patch.stopall)

    def test_org_chart_write_requires_human_admin(self):
        from features.auth import require_human_principal

        # 普通用户（人工会话）→ 放行 human 检查，admin 由 Depends 层卡。
        self.assertIsNotNone(require_human_principal(_request("alice")))
        # Agent token → 403。
        with self.assertRaises(HTTPException) as ctx:
            require_human_principal(_request("agent-1", auth_method="agent_token"))
        self.assertEqual(ctx.exception.status_code, 403)
        # 机器能力 token → 403。
        with self.assertRaises(HTTPException) as ctx:
            require_human_principal(
                _request("worker-1", auth_method="machine_authority"))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_put_org_chart_persists_shared_payload(self):
        request = _request("root", role="admin")

        async def body():
            return {"payload": {"departments": [
                {"department_id": "qa", "department": "QA",
                 "members": [{"id": 1, "name": "A"}]},
            ]}}

        request.json = body
        result = asyncio.run(users_api.put_org_chart(request))
        self.assertTrue(result["success"])
        saved = load_org_payload()
        self.assertEqual(saved["departments"][0]["department_id"], "qa")

    def test_self_binding_is_per_owner(self):
        path = org_chart.org_chart_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"departments": [
            {"department_id": "d", "department": "部",
             "members": [{"id": 7, "name": "张三"}]},
        ]}), encoding="utf-8")

        async def body():
            return {"member_id": 7}

        request = _request("alice")
        request.json = body
        result = asyncio.run(users_api.put_my_binding(request))
        self.assertTrue(result["success"])
        # 只写 alice 的 overlay，不影响他人/全局文件。
        self.assertEqual(load_user_overlay("alice").get("me"), 7)
        self.assertEqual(
            load_org_payload()["departments"][0]["members"][0]["name"], "张三")

        # 绑定不存在成员 → 业务失败而非异常。
        async def bad_body():
            return {"member_id": 999}

        request.json = bad_body
        fail = asyncio.run(users_api.put_my_binding(request))
        self.assertFalse(fail["success"])


if __name__ == "__main__":
    unittest.main()
