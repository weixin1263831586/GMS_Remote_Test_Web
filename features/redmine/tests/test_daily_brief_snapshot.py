"""Daily Brief 快照身份解析测试：严格单人视角。

验证 owner 被解析为**单个** Redmine 用户，且不再把 owner 保存的部门
用户表整体当作 owner_names、也不在缺身份时回退 None（后者会让 repository
展开为全部 assignee，造成跨成员数据暴露）。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import features.redmine.daily_brief_snapshot as snapshot_mod
from features.redmine.daily_brief_snapshot import (
    DailyBriefIdentityError,
    build_daily_triage_snapshot,
    resolve_daily_brief_owner_identity,
)


class _FakeClient:
    def __init__(self, user):
        self._user = user

    async def get_current_user(self):
        return self._user

    async def close(self):
        pass


class _FakeAgent:
    def __init__(self, user, config):
        self._user = user
        self.config_manager = SimpleNamespace(load_config=lambda: config)

    def _make_client(self):
        return _FakeClient(self._user)


def _service(user, config=None):
    return SimpleNamespace(agent=_FakeAgent(user, config or {}))


class IdentityResolutionTests(unittest.TestCase):
    def test_user_map_username_maps_to_single_user(self):
        # user map 有多个部门成员；只应返回 username 命中的那一个。
        user_map = [
            {"id": 1, "name": "张三", "aliases": ["zhangsan"], "email": "z@x.com"},
            {"id": 2, "name": "李四", "aliases": ["lisi"], "email": "l@x.com"},
        ]
        service = _service(None, {"redmine_auth": {"username": "lisi"}})
        with patch.object(snapshot_mod, "load_redmine_user_map_for_owner",
                          lambda owner: user_map):
            identity = asyncio.run(
                resolve_daily_brief_owner_identity("owner-a", service)
            )
        self.assertEqual(identity["user_id"], 2)
        self.assertIn("李四", identity["names"])
        self.assertNotIn("张三", identity["names"])

    def test_live_current_user_when_no_map_hit(self):
        user = SimpleNamespace(id=9, firstname="San", lastname="Zhang",
                               login="zhangsan", mail="z@x.com")
        service = _service(user, {"redmine_auth": {"username": "unknown"}})
        with patch.object(snapshot_mod, "load_redmine_user_map_for_owner",
                          lambda owner: []):
            identity = asyncio.run(
                resolve_daily_brief_owner_identity("owner-a", service)
            )
        self.assertEqual(identity["user_id"], 9)
        self.assertIn("Zhang San", identity["names"])

    def test_fail_closed_when_identity_unresolvable(self):
        service = _service(None, {})
        with patch.object(snapshot_mod, "load_redmine_user_map_for_owner",
                          lambda owner: []), self.assertRaises(DailyBriefIdentityError):
            asyncio.run(
                resolve_daily_brief_owner_identity("owner-a", service)
            )


class SnapshotScopingTests(unittest.TestCase):
    def test_snapshot_uses_single_owner_names_never_none(self):
        captured: dict = {}

        class _Repo:
            def get_workload_statistics(self, **kwargs):
                captured.update(kwargs)
                return {"lists": {"waiting_my_reply": [], "no_reply_3_days": []}}

        user = SimpleNamespace(id=7, firstname="San", lastname="Zhang",
                               login="zhangsan", mail="z@x.com")
        service = SimpleNamespace(
            agent=_FakeAgent(user, {}),
            repository=_Repo(),
        )
        with patch.object(snapshot_mod, "get_redmine_service_for_owner",
                          lambda owner: service), \
                patch.object(snapshot_mod, "load_redmine_user_map_for_owner",
                             lambda owner: []), \
                patch.object(snapshot_mod, "_sync_owner_issue_snapshots",
                             _async(False)):
            snapshot = asyncio.run(
                build_daily_triage_snapshot("owner-a", refresh=True)
            )
        # owner_names 必须是该用户的显示名集合，绝不是 None。
        self.assertIsNotNone(captured["owner_names"])
        self.assertIn("Zhang San", captured["owner_names"])
        self.assertEqual(snapshot["owner"]["user_id"], 7)

    def test_source_sync_status_reflects_pre_sync_outcome(self):
        """pre-sync 失败不静默：快照必须带 source_sync_status。"""

        class _Repo:
            def get_workload_statistics(self, **kwargs):
                return {"lists": {"waiting_my_reply": [], "no_reply_3_days": []}}

        user = SimpleNamespace(id=7, firstname="San", lastname="Zhang",
                               login="zhangsan", mail="z@x.com")
        service = SimpleNamespace(
            agent=_FakeAgent(user, {}),
            repository=_Repo(),
        )
        with patch.object(snapshot_mod, "get_redmine_service_for_owner",
                          lambda owner: service), \
                patch.object(snapshot_mod, "load_redmine_user_map_for_owner",
                             lambda owner: []):

            async def _raise(*args, **kwargs):
                raise RuntimeError("redmine down")

            # pre-sync 抛异常 → sync_failed
            with patch.object(snapshot_mod, "_sync_owner_issue_snapshots", _raise):
                failed = asyncio.run(
                    build_daily_triage_snapshot("owner-a", refresh=True)
                )
            self.assertEqual(failed["source_sync_status"], "sync_failed")
            self.assertFalse(failed["synced"])

            # pre-sync 成功 → synced
            with patch.object(snapshot_mod, "_sync_owner_issue_snapshots", _async(True)):
                ok = asyncio.run(
                    build_daily_triage_snapshot("owner-a", refresh=True)
                )
            self.assertEqual(ok["source_sync_status"], "synced")
            self.assertTrue(ok["synced"])

            # refresh=False 显式跳过 → skipped
            from unittest.mock import AsyncMock

            with patch.object(snapshot_mod, "_sync_owner_issue_snapshots", AsyncMock(return_value=True)) as sync:
                skipped = asyncio.run(
                    build_daily_triage_snapshot("owner-a", refresh=False)
                )
            self.assertEqual(skipped["source_sync_status"], "skipped")
            sync.assert_not_called()


def _async(value):
    async def coro(*args, **kwargs):
        return value
    return coro


if __name__ == "__main__":
    unittest.main()
