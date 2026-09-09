"""SDK local_git provider 测试（计划 §12/§16）。

用临时 git 仓库验证：
- revision 解析为 commit，read 返回解析后 commit 和 blob 哈希；
- 同一 symbol 在两个 revision 有差异时，各自返回正确内容；
- 未配置 source 404；symlink/路径逃逸/`.git` 被拒绝；result_id 签名校验。
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from features.system.source_provider import (
    ProviderConfig,
    SourceProviderError,
    SourceRegistry,
)


def _run_git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "tester")
    env.setdefault("GIT_AUTHOR_EMAIL", "tester@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "tester")
    env.setdefault("GIT_COMMITTER_EMAIL", "tester@example.com")
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {args} failed: {completed.stderr}")
    return completed.stdout.strip()


class LocalGitProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.repo = self.root / "sdk-repo"
        self.repo.mkdir(parents=True)
        _run_git(self.repo, "init", "-q", "--initial-branch=main")
        source_dir = self.repo / "framework" / "base"
        source_dir.mkdir(parents=True)
        (source_dir / "Service.java").write_text(
            "class Service {\n  void run() { /* v1 */ }\n}\n",
            encoding="utf-8",
        )
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "v1")
        self.commit_v1 = _run_git(self.repo, "rev-parse", "HEAD")
        (source_dir / "Service.java").write_text(
            "class Service {\n  void run() { /* v2 fixed */ }\n}\n",
            encoding="utf-8",
        )
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "v2")
        self.commit_v2 = _run_git(self.repo, "rev-parse", "HEAD")

        self.registry = SourceRegistry(
            [ProviderConfig(
                source_id="android-test",
                provider="local_git",
                repo_root=str(self.repo),
                default_revision="main",
            )],
            b"test-secret",
        )
        self.provider = self.registry.get("android-test")

    def tearDown(self):
        self.directory.cleanup()

    def test_unconfigured_source_404(self):
        with self.assertRaises(SourceProviderError) as ctx:
            self.registry.get("nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_revision_resolves_to_commit(self):
        metadata = self.provider.revision_metadata("main")
        self.assertEqual(metadata["commit"], self.commit_v2)
        metadata_v1 = self.provider.revision_metadata(self.commit_v1[:8])
        self.assertEqual(metadata_v1["commit"], self.commit_v1)

    def test_revision_rejects_unknown(self):
        with self.assertRaises(SourceProviderError) as ctx:
            self.provider.resolve_commit("definitely-not-a-ref")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_search_and_read_bound_to_revision(self):
        result_v1 = self.provider.search(self.commit_v1, "v1")
        self.assertEqual(result_v1["commit"], self.commit_v1)
        self.assertEqual(len(result_v1["matches"]), 1)
        match = result_v1["matches"][0]
        self.assertEqual(match["path"], "framework/base/Service.java")
        self.assertEqual(match["line"], 2)

        result_v2 = self.provider.search(self.commit_v1, "v2 fixed")
        self.assertEqual(result_v2["total"], 0)
        result_v2_hit = self.provider.search("main", "v2 fixed")
        self.assertEqual(result_v2_hit["total"], 1)

        # read 只接受对应 commit 的 result_id（不再要求 path/commit 参数）
        read_v1 = self.provider.read_signed(
            match["result_id"], offset=0, limit=10
        )
        self.assertEqual(read_v1["commit"], self.commit_v1)
        self.assertEqual(read_v1["path"], "framework/base/Service.java")
        self.assertIn("v1", read_v1["lines"][1]["text"])
        self.assertTrue(read_v1["blob_sha256"])
        self.assertEqual(read_v1["total_lines"], 3)

        # v1 的 result_id 不能读 v2 的 commit（载荷内 commit 已固定）
        with self.assertRaises(SourceProviderError):
            self.provider.read_signed(
                match["result_id"].replace(match["result_id"][-4:], "0000"),
                offset=0, limit=10,
            )

    def test_result_id_is_self_describing_opaque(self):
        result = self.provider.search(self.commit_v1, "v1")
        match = result["matches"][0]
        # result_id 不泄漏路径明文
        self.assertNotIn("Service.java", match["result_id"])
        # registry 层：跨 provider 读取只凭 result_id
        read = self.registry.read_signed(match["result_id"], offset=0, limit=10)
        self.assertEqual(read["source_id"], "android-test")
        self.assertEqual(read["commit"], self.commit_v1)

    def test_result_id_rejects_foreign_source(self):
        from features.system.source_provider import ProviderConfig, SourceRegistry

        other = SourceRegistry(
            [ProviderConfig(
                source_id="other-source",
                provider="local_git",
                repo_root=str(self.repo),
            )],
            b"test-secret",
        )
        result = self.provider.search(self.commit_v1, "v1")
        with self.assertRaises(SourceProviderError):
            other.read_signed(result["matches"][0]["result_id"])

    def test_read_rejects_git_internals(self):
        result = self.provider.search(self.commit_v1, "v1")
        # 构造一个指向 .git/config 的伪造 result_id：先造正牌再看防线。
        # .git 路径在签发时不会被拒绝，但读取时会被拒绝。
        forged = self.provider._make_result_id(".git/config", self.commit_v1)
        with self.assertRaises(SourceProviderError) as ctx:
            self.provider.read_signed(forged)
        self.assertEqual(ctx.exception.status_code, 422)
        # 正牌 result_id 不受影响
        self.provider.read_signed(result["matches"][0]["result_id"])

    def test_read_rejects_tampered_result_id(self):
        with self.assertRaises(SourceProviderError) as ctx:
            self.provider.read_signed("src_tampered")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_search_path_filter(self):
        result = self.provider.search("main", "Service", path_filter="no/such/dir")
        self.assertEqual(result["total"], 0)
        result = self.provider.search("main", "Service", path_filter="framework")
        self.assertEqual(result["total"], 1)


class SdkApiTests(unittest.TestCase):
    """source_api HTTP 层：scope 与未配置 provider 的降级。"""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from features.auth import CurrentUser
        from features.system import source_api, source_provider

        source_provider.configure_source_registry(
            [
                ProviderConfig(
                    source_id="android-test",
                    provider="local_git",
                    repo_root="/nonexistent",
                )
            ],
            b"test",
        )
        app = FastAPI()

        @app.middleware("http")
        async def authenticate(request, call_next):
            role = request.headers.get("x-test-role", "agent")
            scopes = request.headers.get("x-test-scopes", "sdk.read")
            extra = frozenset(scopes.split(",")) if role == "agent" else frozenset()
            request.state.current_user = CurrentUser(
                id="owner", username="owner", role=role if role != "agent" else "agent_service",
                extra_permissions=extra,
            )
            request.state.auth_method = "session"
            return await call_next(request)

        app.include_router(source_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()

    def test_requires_scope(self):
        response = self.client.get(
            "/api/sdk/sources", headers={"x-test-scopes": "devices.read"}
        )
        self.assertEqual(response.status_code, 403)

    def test_lists_sources_without_repo_root(self):
        response = self.client.get("/api/sdk/sources", headers={})
        self.assertEqual(response.status_code, 200)
        sources = response.json()["data"]["sources"]
        self.assertEqual(sources[0]["source_id"], "android-test")
        self.assertNotIn("repo_root", sources[0])
        self.assertEqual(sources[0]["repo_root_exposed"], False)

    def test_search_reports_invalid_repo_as_502(self):
        response = self.client.get(
            "/api/sdk/search",
            params={"source": "android-test", "revision": "main", "query": "x"},
            headers={},
        )
        self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
