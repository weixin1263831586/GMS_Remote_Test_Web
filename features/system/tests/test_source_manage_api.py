"""source_manage_api（「添加SDK」管理面）的守卫与流程测试。

TestClient + 中间件打桩认证（同 test_source_provider 模式）：
- human-only：agent_token 403、elevation 403（写路径）、dev/anonymous 语义；
- probe/注册/删除流程：SSH 与编译服务器配置打桩，验证命令构造、
  校验拒绝（路径不存在 / 无 git）、热重载被调用；
- list 接口的 live 校验与 mark_verified 落盘。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.system.source_user_store import (
    UserSourceStore,
    UserSourceStoreError,
)


_CLEANUPS: list = []


def _stub_secrets() -> None:
    def fake_encrypt(value: str) -> str:
        return "enc::" + value[::-1]

    def fake_decrypt(value: str) -> str:
        if not value.startswith("enc::"):
            raise ValueError("bad token")
        return value[len("enc::"):][::-1]

    patcher = mock.patch.multiple(
        "features.system.source_user_store",
        encrypt_secret=fake_encrypt,
        decrypt_secret=fake_decrypt,
    )
    patcher.start()
    _CLEANUPS.append(patcher.stop)


def tearDownModule():
    while _CLEANUPS:
        _CLEANUPS.pop()()


class _ManageApiHarness(unittest.TestCase):
    """公共脚手架：打桩 store / build_servers / settings。"""

    def setUp(self):
        _stub_secrets()
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store_path = self.root / "user_sources.json"
        self.store = UserSourceStore(self.store_path)
        # 编译服务器清单（env_password 口令来自测试环境变量）
        servers_path = self.root / "build_servers.json"
        servers_path.write_text(json.dumps({
            "servers": [{
                "id": "build-206", "name": "206", "host": "10.10.10.206",
                "port": 22, "username": "hcj", "backend": "ssh",
                "auth": {"type": "env_password", "env": "TEST_SDK_SSH_PW"},
                "workspace_root": "/home/hcj",
            }]
        }), encoding="utf-8")
        import features.system.source_manage_api as manage
        self.manage = manage
        fake_settings = mock.Mock()
        fake_settings.project_root = str(self.root)
        patchers = [
            mock.patch.object(manage, "build_servers_path", return_value=servers_path),
            mock.patch.object(manage, "settings", fake_settings),
            mock.patch.object(manage, "user_source_store", return_value=self.store),
        ]
        for patcher in patchers:
            patcher.start()
            _CLEANUPS.append(patcher.stop)

        self.app = FastAPI()

        @self.app.middleware("http")
        async def authenticate(request, call_next):
            role = request.headers.get("x-test-role", "human")
            method = request.headers.get("x-test-method", "session")
            elevated = request.headers.get("x-test-elevated", "1") == "1"
            user = CurrentUser(
                id="owner-1",
                username="hcj",
                role=role if role != "agent" else "agent_service",
                resource_owner_id="owner-1",
            )
            request.state.current_user = user
            request.state.auth_method = method
            request.state.is_elevated = elevated
            return await call_next(request)

        self.app.include_router(manage.router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def _make_temp_git_repo(self, name: str = "repo") -> Path:
        """真实临时 git 仓（git init + 1 commit），供 local_git 源探测。"""
        import subprocess

        repo = self.root / "repos" / name
        repo.mkdir(parents=True)
        (repo / "README.md").write_text("temp repo\n", encoding="utf-8")
        for command in (
            ["git", "init", "-q"],
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "add", "README.md"],
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "commit", "-qm", "init"],
        ):
            subprocess.run(command, cwd=repo, check=True, timeout=60)
        return repo


class ManageApiGuardTests(_ManageApiHarness):
    def test_agent_token_cannot_read_servers(self):
        response = self.client.get(
            "/api/sdk/manage/servers",
            headers={"x-test-method": "agent_token"},
        )
        self.assertEqual(response.status_code, 403)

    def test_agent_token_cannot_register(self):
        response = self.client.post(
            "/api/sdk/manage/sources",
            json={"server_id": "build-206", "repo_root": "/sdk"},
            headers={"x-test-method": "agent_token"},
        )
        self.assertEqual(response.status_code, 403)

    def test_unelevated_human_cannot_register_or_delete(self):
        headers = {"x-test-elevated": "0"}
        response = self.client.post(
            "/api/sdk/manage/sources",
            json={"server_id": "build-206", "repo_root": "/sdk"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.delete(
            "/api/sdk/manage/sources/whatever", headers=headers
        )
        self.assertEqual(response.status_code, 403)

    def test_unknown_server_404(self):
        response = self.client.post(
            "/api/sdk/manage/probe",
            json={"server_id": "nope", "repo_root": "/sdk"},
        )
        self.assertEqual(response.status_code, 404)


class ManageApiFlowTests(_ManageApiHarness):
    def _patch_ssh(self, stdout="", code=0):
        """打桩 features.system.ssh.SSHManager（manage API 经延迟 import 取用）。"""
        from features.system import ssh as ssh_module

        result = mock.Mock(stdout=stdout, stderr="", code=code, timed_out=False)
        instance = mock.MagicMock()
        ssh_client = mock.MagicMock()
        instance.create_connection.return_value = ssh_client
        instance.execute_command.return_value = result
        return mock.patch.object(ssh_module, "SSHManager", return_value=instance)

    def test_probe_reports_git_workspace(self):
        with self._patch_ssh(stdout="DIR_OK\nREPO_OK\nSUB_GIT_OK\n"):
            response = self.client.post(
                "/api/sdk/manage/probe",
                json={"server_id": "build-206", "repo_root": "/home/hcj/6_A16"},
            )
        self.assertEqual(response.status_code, 200)
        probe = response.json()["data"]["probe"]
        self.assertTrue(probe["path_exists"])
        self.assertTrue(probe["has_sub_git_repo"])
        self.assertIn("repo 工作区", probe["summary"])

    def test_probe_timeout_is_502_not_missing_path(self):
        """SSH 超时（code=-1/timed_out）必须 502，不能误报「路径不存在」。"""
        result = mock.Mock(stdout="", stderr="", code=-1, timed_out=True)
        instance = mock.MagicMock()
        instance.create_connection.return_value = mock.MagicMock()
        instance.execute_command.return_value = result
        from features.system import ssh as ssh_module

        with mock.patch.object(ssh_module, "SSHManager", return_value=instance), \
                mock.patch.object(
                    self.manage, "_resolve_password_by_target",
                    return_value="pw",
                ):
            response = self.client.post(
                "/api/sdk/manage/probe",
                json={"provider": "ssh_git", "target": "hcq@10.0.0.9",
                      "repo_root": "/home/hcq/6_A16"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("已自动重试仍超时", response.json()["error"])
        # 超时自动重试一次：execute_command 被调用 1+重试 次。
        self.assertEqual(instance.execute_command.call_count, 2)

    def test_probe_timeout_retry_recovers(self):
        """偶发超时自动重试后成功：用户不感知，探测正常返回。"""
        from features.system import ssh as ssh_module

        timeout_result = mock.Mock(stdout="", stderr="", code=-1, timed_out=True)
        ok_result = mock.Mock(
            stdout="DIR_OK\nREPO_OK\nSUB_GIT_OK\n", stderr="", code=0, timed_out=False,
        )
        instance = mock.MagicMock()
        instance.create_connection.return_value = mock.MagicMock()
        instance.execute_command.side_effect = [timeout_result, ok_result]
        with mock.patch.object(ssh_module, "SSHManager", return_value=instance), \
                mock.patch.object(
                    self.manage, "_resolve_password_by_target",
                    return_value="pw",
                ):
            response = self.client.post(
                "/api/sdk/manage/probe",
                json={"provider": "ssh_git", "target": "hcq@10.0.0.9",
                      "repo_root": "/home/hcq/6_A16"},
            )
        self.assertEqual(response.status_code, 200)
        probe = response.json()["data"]["probe"]
        self.assertTrue(probe["path_exists"])
        self.assertTrue(probe["has_sub_git_repo"])

    def test_probe_rejects_missing_path(self):
        with self._patch_ssh(stdout="SUB_GIT_OK\n"):
            response = self.client.post(
                "/api/sdk/manage/probe",
                json={"server_id": "build-206", "repo_root": "/home/hcj/gone"},
            )
        self.assertEqual(response.status_code, 200)
        probe = response.json()["data"]["probe"]
        self.assertFalse(probe["path_exists"])

    def test_probe_rejects_traversal(self):
        response = self.client.post(
            "/api/sdk/manage/probe",
            json={"server_id": "build-206", "repo_root": "/a/../etc"},
        )
        self.assertEqual(response.status_code, 400)

    def test_derive_source_id_preserves_case(self):
        """目录名派生 source_id 必须保留原始大小写（7_Android17_1017）。"""
        derive = self.manage._derive_source_id
        self.assertEqual(derive("/home/hcj/7_Android17_1017"), "7_Android17_1017")
        self.assertEqual(derive("/home/hcj/6_SDK-AOSP_v2.1/"), "6_SDK-AOSP_v2-1")
        # 非法字符仍被清洗，空结果回退 "sdk"。
        self.assertEqual(derive("/home/hcj/###"), "sdk")

    def test_guess_android_version(self):
        guess = self.manage.guess_android_version
        self.assertEqual(guess("/home/hcj/7_Android17_1017"), "Android17")
        self.assertEqual(guess("/home/hcj/android-16-bp2a"), "Android16")
        self.assertEqual(guess("/home/hcj/AOSP_15_workspace"), "Android15")
        self.assertEqual(guess("/home/hcj/aosp_android_14.0.0_r1"), "Android14")
        # 无 Android/aosp 字样不猜：0623 等日期/编号不得误判成版本。
        self.assertEqual(guess("/home/hcj/6_Android16_0623"), "Android16")
        self.assertEqual(guess("/home/hcj/my_project_0623"), "")
        self.assertEqual(guess("/home/hcj/backup_2024"), "")

    def test_register_rejects_path_without_git(self):
        with self._patch_ssh(stdout="DIR_OK\n0\n"):
            response = self.client.post(
                "/api/sdk/manage/sources",
                json={"server_id": "build-206", "repo_root": "/home/hcj/empty"},
            )
        self.assertEqual(response.status_code, 422)
        self.assertIn("git", response.json()["error"])

    def test_register_adds_source_and_reloads(self):
        with self._patch_ssh(stdout="DIR_OK\nREPO_OK\nSUB_GIT_OK\n"), \
                mock.patch.object(self.manage, "reload_source_runtime") as reload:
            reload.return_value = "AVAILABLE"
            response = self.client.post(
                "/api/sdk/manage/sources",
                json={
                    "server_id": "build-206",
                    "repo_root": "/home/hcj/6_Android16_0623",
                    "password": "one-shot-pw",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()["data"]["source"]
        self.assertEqual(payload["source_id"], "6_Android16_0623")
        self.assertEqual(payload["registry_state"], "AVAILABLE")
        self.assertNotIn("ssh_password_enc", payload)
        reload.assert_called_once()
        # 口令加密落盘，明文不出现
        raw = self.store_path.read_text(encoding="utf-8")
        self.assertNotIn("one-shot-pw", raw)
        self.assertIn("ssh_password_enc", raw)
        self.assertEqual(self.store.get_password("6_Android16_0623"), "one-shot-pw")

    def test_delete_removes_and_reloads(self):
        self.store.add(
            source_id="a16",
            repo_root="/sdk",
            ssh_host="10.10.10.206",
            ssh_port=22,
            ssh_username="hcj",
            ssh_password="p",
        )
        with mock.patch.object(self.manage, "reload_source_runtime") as reload:
            reload.return_value = "AVAILABLE"
            response = self.client.delete("/api/sdk/manage/sources/a16")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["registry_state"], "AVAILABLE")
        with self.assertRaises(UserSourceStoreError):
            self.store.get("a16")

    def test_delete_unknown_404(self):
        response = self.client.delete("/api/sdk/manage/sources/ghost")
        self.assertEqual(response.status_code, 404)


class DiscoverTests(_ManageApiHarness):
    def test_parse_discover_drops_git_and_sub_lines(self):
        """2026-11-15 收紧：裸 .git 仓（GIT/SUB）不再列为 SDK 候选。

        gms_tools 这类 CI 工具仓曾被误列成 SDK；SDK 检索对它们跑全量子仓
        枚举纯属浪费。旧命令的 GIT/SUB 输出（老格式兼容）同样丢弃。
        """
        candidates = self.manage._parse_discover(
            "GIT|/home/builder/sdk\nSUB|/home/builder/work/sdk\n"
        )
        self.assertEqual(candidates, [])
        # repo 工作区与 Android 源码树仍然保留。
        candidates = self.manage._parse_discover(
            "HOME|/home/builder\n"
            "REPO|/home/builder/aosp\n"
            "ANDROID|/home/builder/single-aosp\n"
            "GIT|/home/builder/gms_tools\n"
        )
        self.assertEqual(
            [(item["kind"], item["path"]) for item in candidates],
            [("REPO", "/home/builder/aosp"),
             ("ANDROID", "/home/builder/single-aosp")],
        )

    def test_parse_target_formats(self):
        parse = self.manage._parse_target
        self.assertEqual(
            parse("hcq@10.10.10.206"),
            {"host": "10.10.10.206", "port": 22, "username": "hcq"},
        )
        self.assertEqual(
            parse("hcq@10.10.10.206:2222"),
            {"host": "10.10.10.206", "port": 2222, "username": "hcq"},
        )
        for bad in ("", "10.10.10.206", "a@b@c", "user@ho st", "user@host:99999"):
            with self.assertRaises(UserSourceStoreError):
                parse(bad)

    def test_discover_lists_repo_workspaces(self):
        stdout = (
            "HOME|/home/hcj\n"
            "REPO|/home/hcj/6_Android16_0623\n"
            "ANDROID|/home/hcj/single-tree\n"
            "SUB|/home/hcj/single-project\n"  # 裸 .git 仓：2026-11-15 起丢弃
            "junk\n"
        )
        with mock.patch.object(
            self.manage, "_run_remote_probe", return_value=(stdout, 0)
        ), mock.patch.object(
            self.manage, "_resolve_password_by_target", return_value="pw"
        ):
            response = self.client.post(
                "/api/sdk/manage/discover", json={"target": "hcq@10.0.0.9"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        candidates = response.json()["data"]["candidates"]
        self.assertEqual(candidates[0]["kind"], "REPO")
        self.assertEqual(candidates[0]["path"], "/home/hcj/6_Android16_0623")
        self.assertIn("repo 工作区", candidates[0]["kind_label"])
        # ANDROID 是 Android 源码树（单仓，含 build/envsetup.sh）。
        android = next(item for item in candidates if item["kind"] == "ANDROID")
        self.assertEqual(android["path"], "/home/hcj/single-tree")
        self.assertIn("Android 源码树", android["kind_label"])
        # SUB/GIT 不再出现。
        self.assertEqual({item["kind"] for item in candidates}, {"REPO", "ANDROID"})

    def test_discover_keeps_nested_home_and_drops_git(self):
        """嵌套 home（/data/users/x）的一级目录有 4 个斜杠，不能误剪；
        裸 git 仓即使一层深也不再列为候选（2026-11-15 收紧）。"""
        stdout = (
            "HOME|/data/users/hcq\n"
            "GIT|/data/users/hcq/my-project\n"       # 裸 .git：丢弃
            "REPO|/data/users/hcq/aosp\n"            # repo 工作区：保留
            "ANDROID|/data/users/hcq/kernel\n"       # Android 树：保留
            "SUB|/data/users/hcq/aosp/deep/extra\n"  # 深层 + 非候选类型：剪掉
        )
        with mock.patch.object(
            self.manage, "_run_remote_probe", return_value=(stdout, 0)
        ), mock.patch.object(
            self.manage, "_resolve_password_by_target", return_value="pw"
        ):
            response = self.client.post(
                "/api/sdk/manage/discover", json={"target": "hcq@10.0.0.9"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        candidates = response.json()["data"]["candidates"]
        kinds = {(item["kind"], item["path"]) for item in candidates}
        self.assertIn(("REPO", "/data/users/hcq/aosp"), kinds)
        self.assertIn(("ANDROID", "/data/users/hcq/kernel"), kinds)
        self.assertNotIn(("GIT", "/data/users/hcq/my-project"), kinds)
        self.assertNotIn(("SUB", "/data/users/hcq/aosp/deep/extra"), kinds)

    def test_discover_command_executes_and_marks_candidates(self):
        """真实执行 _discover_command：repo 工作区与 Android 源码树必须被
        识别（含空格路径），裸 .git 仓不再产出候选行（2026-11-15 收紧）。

        这是评审要求的 shell 集成测试——只检查命令字符串挡不住
        转义回归（历史上 GIT 行的 \" 转义让候选类型静默丢失）。"""
        import subprocess

        home = self.root / "remote-home"
        git_repo = home / "my repo"
        (git_repo / ".git").mkdir(parents=True)
        repo_ws = home / "6_A16"
        (repo_ws / ".repo").mkdir(parents=True)
        (repo_ws / ".repo" / "manifest.xml").write_text("<manifest/>")
        android_tree = home / "single aosp"
        (android_tree / "build" / "make").mkdir(parents=True)
        (android_tree / "build" / "make" / "envsetup.sh").write_text("#!/bin/sh\n")
        stdout = subprocess.run(
            ["bash", "-c", self.manage._discover_command()],
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        candidates = {item["path"]: item["kind"]
                      for item in self.manage._parse_discover(stdout)}
        self.assertEqual(candidates.get(str(repo_ws)), "REPO")
        self.assertEqual(candidates.get(str(android_tree)), "ANDROID")
        # 裸 .git 仓不再被列为候选。
        self.assertNotIn(str(git_repo), candidates)

    def test_discover_requires_target(self):
        response = self.client.post("/api/sdk/manage/discover", json={})
        self.assertEqual(response.status_code, 400)

    def test_discover_empty_result_404(self):
        with mock.patch.object(
            self.manage, "_run_remote_probe", return_value=("", 0)
        ), mock.patch.object(
            self.manage, "_resolve_password_by_target", return_value="pw"
        ):
            response = self.client.post(
                "/api/sdk/manage/discover", json={"target": "hcq@10.0.0.9"}
            )
        self.assertEqual(response.status_code, 404)

    def test_probe_via_manual_target(self):
        with mock.patch.object(
            self.manage, "_run_remote_probe",
            return_value=("DIR_OK\nREPO_OK\nSUB_GIT_OK\n", 0),
        ), mock.patch.object(
            self.manage, "_resolve_password_by_target", return_value="pw"
        ) as resolve:
            response = self.client.post(
                "/api/sdk/manage/probe",
                json={"provider": "ssh_git", "target": "hcq@10.0.0.9",
                      "repo_root": "/home/hcq/6_A16"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        probe = response.json()["data"]["probe"]
        self.assertTrue(probe["has_sub_git_repo"])
        resolve.assert_called_once_with("10.0.0.9", "hcq", "", source_id="")

    def test_register_via_manual_target(self):
        with mock.patch.object(
            self.manage, "_run_remote_probe",
            return_value=("DIR_OK\nREPO_OK\nSUB_GIT_OK\n", 0),
        ), mock.patch.object(
            self.manage, "_resolve_password_by_target", return_value="env-pw"
        ), mock.patch.object(self.manage, "reload_source_runtime") as reload:
            reload.return_value = "AVAILABLE"
            response = self.client.post(
                "/api/sdk/manage/sources",
                json={"provider": "ssh_git", "target": "hcq@10.0.0.9",
                      "repo_root": "/home/hcq/6_Android16_0623"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        source = response.json()["data"]["source"]
        self.assertEqual(source["ssh_host"], "10.0.0.9")
        self.assertEqual(source["ssh_username"], "hcq")
        self.assertEqual(source["source_id"], "6_Android16_0623")
        # env 解析出的口令被加密存储
        self.assertEqual(
            self.store.get_password("6_Android16_0623"), "env-pw"
        )


class ManageListLiveTests(_ManageApiHarness):
    def test_local_git_list_verifies_locally(self):
        """local_git 源在 list 刷新时本地探测，不 SSH。"""
        repo = self._make_temp_git_repo("local-fb")
        self.store.add(
            source_id="local-fb",
            provider="local_git",
            repo_root=str(repo),
            default_revision="main",
        )
        with mock.patch.object(self.manage, "_run_remote_probe") as remote:
            response = self.client.get("/api/sdk/manage/sources?verify=sync")
        self.assertEqual(response.status_code, 200)
        remote.assert_not_called()  # 本机路径绝不发起 SSH
        sources = response.json()["data"]["sources"]
        self.assertTrue(sources[0]["live"]["verified"])
        self.assertTrue(self.store.get("local-fb")["last_verified_ok"])

    def test_local_git_missing_path_marked(self):

        missing_root = str(Path(self.directory.name) / "gone-repo")
        self.store.add(
            source_id="local-gone",
            provider="local_git",
            repo_root=missing_root,
        )
        response = self.client.get("/api/sdk/manage/sources?verify=sync")
        sources = response.json()["data"]["sources"]
        self.assertFalse(sources[0]["live"]["verified"])
        self.assertIn("不存在", sources[0]["live"]["reason"])
        self.assertFalse(self.store.get("local-gone")["last_verified_ok"])

    def test_register_local_git_source(self):
        repo = self._make_temp_git_repo("registered")
        with mock.patch.object(self.manage, "reload_source_runtime") as reload:
            reload.return_value = "AVAILABLE"
            response = self.client.post(
                "/api/sdk/manage/sources",
                json={
                    "provider": "local_git",
                    "repo_root": str(repo),
                    "default_revision": "main",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()["data"]["source"]
        self.assertEqual(payload["provider"], "local_git")
        self.assertEqual(payload["default_revision"], "main")
        self.assertEqual(payload["registry_state"], "AVAILABLE")
        reload.assert_called_once()

    def test_list_sources_runs_live_verification(self):
        self.store.add(
            source_id="a16",
            repo_root="/home/hcj/6_A16",
            ssh_host="10.10.10.206",
            ssh_port=22,
            ssh_username="hcj",
            ssh_password="p",
        )
        from features.system import ssh as ssh_module

        result_ok = mock.Mock(stdout="", stderr="", code=0, timed_out=False)
        instance = mock.MagicMock()
        instance.create_connection.return_value = mock.MagicMock()
        instance.execute_command.return_value = result_ok
        with mock.patch.object(ssh_module, "SSHManager", return_value=instance):
            response = self.client.get("/api/sdk/manage/sources?verify=sync")
        self.assertEqual(response.status_code, 200)
        sources = response.json()["data"]["sources"]
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0]["live"]["verified"])
        # 校验状态已落盘
        record = self.store.get("a16")
        self.assertTrue(record["last_verified_ok"])
        # 加密口令不外泄
        self.assertNotIn("ssh_password_enc", sources[0])

    def test_list_sources_marks_missing_path(self):
        self.store.add(
            source_id="a16",
            repo_root="/home/hcj/gone",
            ssh_host="10.10.10.206",
            ssh_port=22,
            ssh_username="hcj",
            ssh_password="p",
        )
        from features.system import ssh as ssh_module

        result_fail = mock.Mock(stdout="", stderr="", code=1, timed_out=False)
        instance = mock.MagicMock()
        instance.create_connection.return_value = mock.MagicMock()
        instance.execute_command.return_value = result_fail
        with mock.patch.object(ssh_module, "SSHManager", return_value=instance):
            response = self.client.get("/api/sdk/manage/sources?verify=sync")
        sources = response.json()["data"]["sources"]
        self.assertFalse(sources[0]["live"]["verified"])
        self.assertFalse(self.store.get("a16")["last_verified_ok"])

    def test_auto_mode_returns_cached_and_defers_background_verify(self):
        """auto 模式：新鲜缓存直接返回；过期源转后台校验，响应不等待。

        2026-11-14 审核意见：多台编译服务器失联时同步校验卡住列表——
        auto 模式必须立即返回（stale 源带缓存结果 + verify_state），
        校验完成后 last_verified_* 落盘，下次刷新可见。
        """
        import json as json_mod
        import time as time_mod
        from datetime import datetime, timedelta, timezone

        from features.system import source_manage_api as manage

        ok_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        stale_at = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        self.store.add(
            source_id="fresh-src", repo_root="/home/hcj/6_A16",
            ssh_host="10.10.10.206", ssh_port=22, ssh_username="hcj",
            ssh_password="p",
        )
        self.store.add(
            source_id="stale-src", repo_root="/home/hcj/7_A17",
            ssh_host="10.10.10.207", ssh_port=22, ssh_username="hcj",
            ssh_password="p",
        )
        # 直接写存储时间戳：fresh 源窗口内（last_verified_ok=True），
        # stale 源 2h 前（last_verified_ok=False → 缓存不可用但已过期）。
        payload = json_mod.loads(self.store.path.read_text(encoding="utf-8"))
        payload["sources"]["fresh-src"]["last_verified_at"] = ok_at
        payload["sources"]["fresh-src"]["last_verified_ok"] = True
        payload["sources"]["stale-src"]["last_verified_at"] = stale_at
        payload["sources"]["stale-src"]["last_verified_ok"] = False
        self.store.path.write_text(
            json_mod.dumps(payload), encoding="utf-8")
        self.addCleanup(manage._VERIFY_IN_FLIGHT.clear)
        # 后台校验路径打桩：确定性地返回"路径不存在"，等待线程收尾后
        # 断言落盘。绝不真连 SSH。
        with mock.patch.object(
            manage, "_probe_via_server",
            return_value={"verified": False, "reason": "路径不存在"},
        ) as probe:
            response = self.client.get("/api/sdk/manage/sources")
        # 响应立即返回，带 verify_state 与缓存结果。
        sources = {s["source_id"]: s for s in response.json()["data"]["sources"]}
        self.assertEqual(sources["fresh-src"]["verify_state"], "fresh")
        self.assertTrue(sources["fresh-src"]["live"]["verified"])
        self.assertEqual(sources["stale-src"]["verify_state"], "stale")
        self.assertIsNone(sources["stale-src"]["live"])  # 后台校验中
        # 等待后台线程收尾（打桩立即返回，秒级完成）。
        deadline = time_mod.time() + 5
        while manage._VERIFY_IN_FLIGHT and time_mod.time() < deadline:
            time_mod.sleep(0.05)
        self.assertFalse(manage._VERIFY_IN_FLIGHT)
        # stale 源确实在后台校验过（一次，不重复），结果已落盘。
        self.assertEqual(probe.call_count, 1)
        self.assertFalse(self.store.get("stale-src")["last_verified_ok"])
        self.assertIsNotNone(self.store.get("stale-src")["last_verified_at"])
        # fresh 源不重复校验。
        probed_roots = [call.args[1] for call in probe.call_args_list]
        self.assertNotIn("/home/hcj/6_A16", probed_roots)


class BatchRegisterTests(_ManageApiHarness):
    """POST /api/sdk/manage/sources/batch（弹框多选批量添加）。"""

    def test_batch_register_requires_paths(self):
        response = self.client.post("/api/sdk/manage/sources/batch", json={})
        self.assertEqual(response.status_code, 400)

    def test_batch_register_rejects_duplicate_paths(self):
        response = self.client.post(
            "/api/sdk/manage/sources/batch",
            json={"provider": "ssh_git", "target": "hcq@10.0.0.9",
                  "repo_roots": ["/home/hcj/6_A16", "/home/hcj/6_A16"]},
        )
        self.assertEqual(response.status_code, 422)

    def test_batch_register_rejects_oversized_batch(self):
        roots = [f"/home/hcj/sdk_{i:02d}" for i in range(17)]
        response = self.client.post(
            "/api/sdk/manage/sources/batch",
            json={"provider": "ssh_git", "target": "hcq@10.0.0.9",
                  "repo_roots": roots},
        )
        self.assertEqual(response.status_code, 422)

    def test_batch_register_via_single_ssh_connection(self):
        """一次 SSH 连接探测全部路径（206 认证 60s+ 只付一次成本）。"""
        from features.system import ssh as ssh_module

        result = mock.Mock(
            stdout="BPR|0|DIR REPO\nBPR|1|DIR GIT\n",
            stderr="", code=0, timed_out=False,
        )
        instance = mock.MagicMock()
        instance.create_connection.return_value = mock.MagicMock()
        instance.execute_command.return_value = result
        with mock.patch.object(
            ssh_module, "SSHManager", return_value=instance
        ) as ssh_cls, mock.patch.object(self.manage, "reload_source_runtime") as reload:
            reload.return_value = "AVAILABLE"
            response = self.client.post(
                "/api/sdk/manage/sources/batch",
                json={"provider": "ssh_git", "target": "hcq@10.0.0.9",
                      "repo_roots": ["/home/hcj/6_A16", "/home/hcj/7_A17"]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["failed"], [])
        self.assertEqual(
            [item["source_id"] for item in data["created"]],
            ["6_A16", "7_A17"],  # 目录名生成，大小写保留
        )
        # 只建立一次连接、执行一条命令（N 条路径不放大认证成本）。
        self.assertEqual(ssh_cls.call_count, 1)
        self.assertEqual(instance.execute_command.call_count, 1)
        reload.assert_called_once()

    def test_batch_register_partial_failure_keeps_rest(self):
        """单条路径非 SDK 树（无 git 证据）→ failed，不阻断其余条目。"""
        from features.system import ssh as ssh_module

        result = mock.Mock(
            stdout="BPR|0|DIR REPO\nBPR|1|DIR\n",  # 第 2 条：只有空目录
            stderr="", code=0, timed_out=False,
        )
        instance = mock.MagicMock()
        instance.create_connection.return_value = mock.MagicMock()
        instance.execute_command.return_value = result
        with mock.patch.object(
            ssh_module, "SSHManager", return_value=instance
        ), mock.patch.object(self.manage, "reload_source_runtime") as reload:
            reload.return_value = "AVAILABLE"
            response = self.client.post(
                "/api/sdk/manage/sources/batch",
                json={"provider": "ssh_git", "target": "hcq@10.0.0.9",
                      "repo_roots": ["/home/hcj/6_A16", "/home/hcj/not-sdk"]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual([item["source_id"] for item in data["created"]], ["6_A16"])
        self.assertEqual(len(data["failed"]), 1)
        self.assertEqual(data["failed"][0]["repo_root"], "/home/hcj/not-sdk")
        self.assertIn("Android 源码树", data["failed"][0]["error"])

    def test_batch_register_local_git_sources(self):
        """local_git 批量：本地探测（绝不 SSH），缺失路径进 failed。"""
        repo = self._make_temp_git_repo("batch-local")
        missing = str(self.root / "gone-batch")
        with mock.patch.object(self.manage, "_run_remote_probe") as remote, \
                mock.patch.object(self.manage, "reload_source_runtime") as reload:
            reload.return_value = "AVAILABLE"
            response = self.client.post(
                "/api/sdk/manage/sources/batch",
                json={"provider": "local_git",
                      "repo_roots": [str(repo), missing]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        remote.assert_not_called()
        data = response.json()["data"]
        self.assertEqual(len(data["created"]), 1)
        self.assertTrue(data["created"][0]["source_id"].startswith("batch-local"))
        self.assertEqual(len(data["failed"]), 1)
        self.assertIn("不存在", data["failed"][0]["error"])

    def test_batch_probe_command_and_parser(self):
        """BPR 标记协议：路径含空格安全，缺行可由数量校验兜底。"""
        import subprocess

        existing = self.root / "real-sdk"
        existing.mkdir()
        spaced = self.root / "my sdk"
        command = self.manage._batch_probe_command(
            [str(spaced), str(existing)]
        )
        stdout = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        probes = self.manage._parse_batch_probe(stdout)
        self.assertEqual(len(probes), 2)
        self.assertFalse(probes[0]["path_exists"])  # 不存在（含空格路径不炸）
        self.assertTrue(probes[1]["path_exists"])   # 存在
        # 缺行 → 数量不符 → _batch_probe_via_target 报 502（解析端兜底）。
        self.assertEqual(len(self.manage._parse_batch_probe("BPR|0|DIR REPO\n")), 1)


if __name__ == "__main__":
    unittest.main()
