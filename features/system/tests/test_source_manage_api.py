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
        with self._patch_ssh(stdout="DIR_OK\nREPO_OK\n1123\n"):
            response = self.client.post(
                "/api/sdk/manage/probe",
                json={"server_id": "build-206", "repo_root": "/home/hcj/6_A16"},
            )
        self.assertEqual(response.status_code, 200)
        probe = response.json()["data"]["probe"]
        self.assertTrue(probe["path_exists"])
        self.assertEqual(probe["git_repo_count"], 1123)
        self.assertIn("1123", probe["summary"])

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
            stdout="DIR_OK\nREPO_OK\n5\n", stderr="", code=0, timed_out=False,
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
        self.assertEqual(probe["git_repo_count"], 5)

    def test_probe_rejects_missing_path(self):
        with self._patch_ssh(stdout="1123\n"):
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
        with self._patch_ssh(stdout="DIR_OK\nREPO_OK\n1123\n"), \
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
    def test_parse_discover_keeps_legacy_output_without_home_marker(self):
        candidates = self.manage._parse_discover(
            "GIT|/home/builder/sdk\nSUB|/home/builder/work/sdk\n"
        )
        self.assertEqual([item["path"] for item in candidates], ["/home/builder/sdk"])

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
            "SUB|/home/hcj/single-project\n"
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
        # SUB 是普通目录下的独立 git 仓；标签不得与 SDK 检索链路
        # 里 "repo 工作区内部子仓"（discover_repos）术语撞车。
        sub = next(item for item in candidates if item["kind"] == "SUB")
        self.assertEqual(sub["kind_label"], "二级 git 仓")

    def test_discover_keeps_git_single_repo_in_nested_home(self):
        """嵌套 home（/data/users/x）的一级目录有 4 个斜杠，不能误剪。"""
        stdout = (
            "HOME|/data/users/hcq\n"
            "GIT|/data/users/hcq/my-project\n"
            "REPO|/data/users/hcq/aosp\n"
            "SUB|/data/users/hcq/aosp/deep/extra\n"  # 三层：剪掉
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
        self.assertIn(("GIT", "/data/users/hcq/my-project"), kinds)
        self.assertIn(("REPO", "/data/users/hcq/aosp"), kinds)
        self.assertNotIn(("SUB", "/data/users/hcq/aosp/deep/extra"), kinds)

    def test_discover_command_executes_and_marks_git_repos(self, ):
        """真实执行 _discover_command：GIT 单仓（含空格路径）必须被识别。

        这是评审要求的 shell 集成测试——只检查命令字符串挡不住
        转义回归（历史上 GIT 行的 \" 转义让 GIT 类型静默丢失）。"""
        import subprocess

        home = self.root / "remote-home"
        git_repo = home / "my repo"
        (git_repo / ".git").mkdir(parents=True)
        repo_ws = home / "6_A16"
        (repo_ws / ".repo").mkdir(parents=True)
        (repo_ws / ".repo" / "manifest.xml").write_text("<manifest/>")
        # SUB 只在「一级目录既非 repo 工作区也非 git 仓」时向二级探测。
        sub_repo = home / "workspaces" / "lib"
        (sub_repo / ".git").mkdir(parents=True)
        stdout = subprocess.run(
            ["bash", "-c", self.manage._discover_command()],
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        candidates = {item["path"]: item["kind"]
                      for item in self.manage._parse_discover(stdout)}
        self.assertEqual(candidates.get(str(git_repo)), "GIT")
        self.assertEqual(candidates.get(str(repo_ws)), "REPO")
        self.assertEqual(candidates.get(str(sub_repo)), "SUB")

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
            return_value=("DIR_OK\nREPO_OK\n1123\n", 0),
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
        self.assertEqual(probe["git_repo_count"], 1123)
        resolve.assert_called_once_with("10.0.0.9", "hcq", "", source_id="")

    def test_register_via_manual_target(self):
        with mock.patch.object(
            self.manage, "_run_remote_probe",
            return_value=("DIR_OK\nREPO_OK\n1123\n", 0),
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
            response = self.client.get("/api/sdk/manage/sources")
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
        response = self.client.get("/api/sdk/manage/sources")
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
            response = self.client.get("/api/sdk/manage/sources")
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
            response = self.client.get("/api/sdk/manage/sources")
        sources = response.json()["data"]["sources"]
        self.assertFalse(sources[0]["live"]["verified"])
        self.assertFalse(self.store.get("a16")["last_verified_ok"])


if __name__ == "__main__":
    unittest.main()
