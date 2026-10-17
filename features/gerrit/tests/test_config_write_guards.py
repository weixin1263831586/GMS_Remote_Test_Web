"""Gerrit 配置安全回归：

- 未配置分支的 /changes 也必须走 _public_config，rest_password 永不明文回显；
- ssh_identity_file / rest_ca_cert 属于主机路径字段，变更必须提权管理员。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException


def _request(body: dict | None = None):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/gerrit-dashboard/changes",
        "headers": [],
    }
    request = SimpleNamespace(
        json=AsyncMock(return_value=dict(body or {})),
        state=SimpleNamespace(),
    )
    request.scope = scope
    return request


class FakeManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.saved = None

    def get_gerrit_dashboard_config(self):
        return dict(self.cfg)

    def save_gerrit_dashboard_config(self, merged):
        self.saved = dict(merged)
        return True


class GerritPublicConfigTests(unittest.TestCase):
    def test_unconfigured_changes_branch_still_masks_rest_password(self):
        import features.gerrit.api as api

        cfg = {
            "base_url": "",
            "ssh_host": "",
            "rest_password": "secret-rest-pw",
            "query_limit": 100,
        }
        manager = FakeManager({})
        with patch.object(
            api, "_dashboard_config_for_request", return_value=cfg
        ), patch.object(api, "_config_for_request", return_value=manager), patch.object(
            api, "_select_profile", return_value={"query": ""}
        ):
            result = asyncio.run(api.list_gerrit_changes(_request(), profile_id="", query=""))

        payload = json.dumps(result, default=str)
        self.assertEqual(result["data"]["config"]["rest_password"], "***")
        self.assertNotIn("secret-rest-pw", payload)


class GerritHostPathFieldGuardTests(unittest.TestCase):
    def _update(self, body, current=None):
        import features.gerrit.api as api

        current = current or {}
        manager = FakeManager(current)
        request = _request(body)
        with patch.object(
            api, "_config_for_request", return_value=manager
        ), patch.object(
            api, "_dashboard_config_for_request", return_value=dict(current)
        ):
            result = asyncio.run(api.update_gerrit_dashboard_config(request))
        return manager, result

    def test_ssh_identity_file_change_requires_elevation(self):
        import features.gerrit.api as api

        def deny(request):
            raise HTTPException(status_code=403, detail="Elevation required")

        with patch.object(api, "require_elevated_admin", side_effect=deny), self.assertRaises(HTTPException) as ctx:
            self._update({"ssh_identity_file": "/root/.ssh/id_ed25519"})
        self.assertEqual(ctx.exception.status_code, 403)

    def test_rest_ca_cert_change_requires_elevation(self):
        import features.gerrit.api as api

        def deny(request):
            raise HTTPException(status_code=403, detail="Elevation required")

        with patch.object(api, "require_elevated_admin", side_effect=deny), self.assertRaises(HTTPException) as ctx:
            self._update({"rest_ca_cert": "/etc/ssl/certs/gerrit.crt"})
        self.assertEqual(ctx.exception.status_code, 403)

    def test_ordinary_fields_do_not_invoke_elevation(self):
        import features.gerrit.api as api

        def deny(request):
            raise AssertionError("ordinary fields must not require elevation")

        with patch.object(api, "require_elevated_admin", side_effect=deny):
            manager, result = self._update({"base_url": "https://gerrit.example"})
        self.assertTrue(result["success"])
        self.assertEqual(manager.saved["base_url"], "https://gerrit.example")

    def test_elevated_admin_may_set_identity_file(self):
        import features.gerrit.api as api

        with patch.object(api, "require_elevated_admin", return_value=None):
            manager, result = self._update(
                {"ssh_identity_file": "/var/lib/gms/gerrit/id_ed25519"}
            )
        self.assertTrue(result["success"])
        self.assertEqual(
            manager.saved["ssh_identity_file"], "/var/lib/gms/gerrit/id_ed25519"
        )


if __name__ == "__main__":
    unittest.main()
