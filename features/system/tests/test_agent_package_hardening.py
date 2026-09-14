"""Agent package hardening tests (long-term hardening items).

Covers the Ed25519 manifest signature chain (registry signs, gms-agent
verifies, bootstrap pins the key) and the TOML profile store (data-only,
no shell semantics, TOML-only with fail-closed profile resolution).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


REPO_ROOT = Path(__file__).resolve().parents[3]
GMS_AGENT = REPO_ROOT / "agent" / "gms-remote-test" / "runtime" / "gms-agent"
REGISTRY = REPO_ROOT / "features" / "system" / "agent_package_registry.py"


def load_pm():
    """Import the lifecycle module the thin shell delegates to."""
    runtime_dir = REPO_ROOT / "agent" / "gms-remote-test" / "runtime"
    if str(runtime_dir) not in sys.path:
        sys.path.insert(0, str(runtime_dir))
    spec = importlib.util.spec_from_loader(
        "gms_agent_pm_hardening",
        importlib.machinery.SourceFileLoader(
            "gms_agent_pm_hardening",
            str(runtime_dir / "gms_agent" / "package_manager.py"),
        ),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["gms_agent_pm_hardening"] = module
    spec.loader.exec_module(module)
    return module


class ManifestSignatureTests(unittest.TestCase):
    def setUp(self):
        self.pm = load_pm()
        self.key = Ed25519PrivateKey.generate()
        self.verify_key_b64 = base64.b64encode(
            self.key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii")

    def _manifest(self, version="0.14.0", sha=None, size=1234):
        return {
            "name": "gms-remote-test",
            "version": version,
            "artifacts": {
                "universal": {
                    "url": "https://ctrl:5001/api/agent/packages/gms-remote-test/0.14.0",
                    "sha256": sha or ("a" * 64),
                    "size": size,
                }
            },
        }

    def _sign(self, manifest):
        payload = self.pm._manifest_signature_payload(manifest)
        return base64.b64encode(self.key.sign(payload)).decode("ascii")

    def test_valid_signature_verifies(self):
        manifest = self._manifest()
        self.assertTrue(
            self.pm._verify_manifest_signature(manifest, self._sign(manifest), self.verify_key_b64)
        )

    def test_tampered_manifest_is_rejected(self):
        manifest = self._manifest(sha="a" * 64)
        signature = self._sign(manifest)
        manifest["artifacts"]["universal"]["sha256"] = "b" * 64  # swapped digest
        self.assertFalse(
            self.pm._verify_manifest_signature(manifest, signature, self.verify_key_b64)
        )

    def test_wrong_key_is_rejected(self):
        manifest = self._manifest()
        other = Ed25519PrivateKey.generate()
        other_b64 = base64.b64encode(
            other.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii")
        self.assertFalse(
            self.pm._verify_manifest_signature(manifest, self._sign(manifest), other_b64)
        )

    def test_registry_signature_covers_the_served_bytes(self):
        """End-to-end: what the registry signs matches what the client checks."""
        import importlib.machinery as machinery

        spec = importlib.util.spec_from_loader(
            "registry_under_test", machinery.SourceFileLoader("registry_under_test", str(REGISTRY))
        )
        registry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(registry)
        version = registry._package_version()
        if not version:
            self.skipTest("package.yaml unavailable")
        archive = registry._build_archive(version)
        sha = hashlib.sha256(archive).hexdigest()
        size = len(archive)
        manifest = self._manifest(version=version, sha=sha, size=size)
        # Registry-side signature payload format must equal client-side:
        registry_payload = f"gms-remote-test|{version}|{sha}|{size}".encode()
        client_payload = self.pm._manifest_signature_payload(manifest)
        self.assertEqual(registry_payload, client_payload)
        signature = base64.b64encode(self.key.sign(registry_payload)).decode("ascii")
        self.assertTrue(
            self.pm._verify_manifest_signature(manifest, signature, self.verify_key_b64)
        )


class TomlProfileTests(unittest.TestCase):
    def setUp(self):
        self.pm = load_pm()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        # profile/token 存储唯一实现在 gms_agent.profile_store（ADR 0003）——
        # 沙箱只 patch 那里（package_manager 经它读写）。legacy .env /
        # MCP_ENV_DIR 契约已删除，不再有第二存储可 patch。
        from gms_agent import profile_store

        self.profile_store = profile_store
        for attr in ("PROFILE_ROOT", "STATE_DIR"):
            self.addCleanup(
                setattr, profile_store, attr, getattr(profile_store, attr)
            )
        profile_store.PROFILE_ROOT = self.home / ".config" / "gms-agent" / "profiles"
        profile_store.STATE_DIR = self.home / ".local" / "state" / "gms-remote-test"

    def test_profile_toml_is_data_only(self):
        server = 'https://ctrl:5001 $(dangerous) `backtick` "quotes" \\'
        self.pm.write_profile_toml("codex-host01-1000", "codex", server, "")
        path = self.profile_store.profile_path("codex-host01-1000")
        self.assertTrue(path.is_file())
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        # No shell interpretation: the raw URL round-trips through the reader.
        flat = self.pm.load_profile("codex-host01-1000")
        self.assertEqual(flat["url"], server)
        # And sourcing it in bash does NOT execute anything.
        result = subprocess.run(
            ["bash", "-c", f"source {path} 2>/dev/null; printf %s \"$url\""],
            capture_output=True, text=True,
        )
        self.assertEqual(result.stdout, "")  # TOML has no $url shell variable

    def test_profile_server_and_ca_prefers_toml(self):
        self.pm.write_profile_toml("kimi-host02-1000", "kimi", "https://toml-host:5001", "/ca.pem")
        server, ca = self.pm.profile_server_and_ca("kimi")
        self.assertEqual(server, "https://toml-host:5001")
        self.assertEqual(ca, "/ca.pem")

    def test_profile_server_is_fail_closed_when_client_profile_is_ambiguous(self):
        # 0 个或多个同 client profile 时必须拒绝猜测（ADR 0003，绝不
        # sorted()-first —— 多 Controller 主机要显式选择）。
        self.pm.write_profile_toml("kkagent-hostA-1000", "kkagent", "https://a:5001", "")
        self.pm.write_profile_toml("kkagent-hostB-1000", "kkagent", "https://b:5001", "")
        self.assertEqual(self.pm.profile_server_and_ca("kkagent"), ("", ""))
        # 0 profiles 同样返回空而不是报错（update/rollback 只需要提示）。
        self.assertEqual(self.pm.profile_server_and_ca("codex"), ("", ""))

    def test_write_profile_is_toml_only(self):
        name = self.pm.write_profile("codex", "https://ctrl:9000", "")
        self.assertTrue(self.profile_store.profile_path(name).is_file())
        # TOML-only contract: write_profile must not create any
        # legacy <client>.env anywhere in the sandbox home.
        self.assertEqual([], list(self.home.rglob("*.env")))
        flat = self.pm.load_profile(name)
        self.assertEqual(flat["url"], "https://ctrl:9000")
        self.assertEqual(flat["mode"], "service-token")
        # The token path is derived by profile_store (STATE_DIR/<profile>.token).
        self.assertEqual(
            flat["token_file"],
            str(self.profile_store.token_file(name)),
        )


class InstallerTlsPolicyTests(unittest.TestCase):
    """install.sh 渲染必须 TLS fail-closed(评审 P0)。

    - 模板本身不得再含 curl -k / wget --no-check-certificate 的无条件降级;
    - 默认路径使用系统信任链;显式降级仅 GMS_INSTALL_ALLOW_INSECURE=1;
    - production Controller 渲染 ALLOW_INSECURE=0,insecure 请求被拒绝。
    """

    def _template(self) -> str:
        import re as _re

        source = REGISTRY.read_text(encoding="utf-8")
        match = _re.search(
            r"_INSTALL_SH_TEMPLATE = r'''(.*?)'''", source, _re.DOTALL
        )
        self.assertIsNotNone(match, "install.sh 模板缺失")
        return match.group(1)

    def test_template_has_no_unconditional_insecure_downgrade(self):
        template = self._template()
        # 降级分支必须由 GMS_INSTALL_ALLOW_INSECURE 显式守护,且仅出现一次
        # (curl/wget 各自的分支内),不允许出现无条件 -k 降级。
        self.assertIn('${GMS_INSTALL_ALLOW_INSECURE:-0}" == "1', template)
        # 生产拒绝分支存在。
        self.assertIn('"$ALLOW_INSECURE" != "1"', template)
        # 每个下载器的跳过校验调用最多两处: 显式 ALLOW_INSECURE 分支 +
        # TOFU 回退(仅当系统信任链校验失败且无显式 CA 时,用于拉取
        # /api/agent/ca.crt 后严格重试; 包完整性另由 SHA-256+Ed25519 保证)。
        for flag in ("-kfsSL", "--no-check-certificate"):
            count = template.count(flag)
            self.assertGreaterEqual(count, 1, flag)
            self.assertLessEqual(count, 2, flag)
        # TOFU 抓取必须排在"系统校验失败"(elif fetch_bootstrap "")之后,
        # 且显式 GMS_INSTALL_CA_CERT 校验失败时直接报错,不得静默降级。
        self.assertIn("elif fetch_bootstrap \"\"", template)
        self.assertIn("/api/agent/ca.crt", template)
        self.assertIn("无法用 GMS_INSTALL_CA_CERT", template)
        guard_index = template.index('elif fetch_bootstrap ""')
        tofu_index = template.index("/api/agent/ca.crt", guard_index)
        self.assertLess(guard_index, tofu_index)

    def test_template_passes_enroll_code_via_env_not_argv(self):
        """配对码不得进 argv/ps/shell history(评审 P2/P3)。

        install.sh 把位置参数/--enroll-code 转为 GMS_AGENT_ENROLL_CODE
        环境变量;gms-agent 端环境变量优先于 --enroll-code。
        """
        template = self._template()
        self.assertIn('export GMS_AGENT_ENROLL_CODE="$CODE"', template)
        self.assertNotIn("--enroll-code \"$CODE\"", template)
        # 配对码必须支持显式命名形式（--paircode, 及等价别名）。
        self.assertIn("--enroll-code|--paircode|--pairing-code", template)
        # 消费后脚本内不再保留明文。
        self.assertIn('CODE=""', template)
        # gms-agent 端:环境变量优先。
        pm_source = (
            REPO_ROOT / "agent" / "gms-remote-test" / "runtime"
            / "gms_agent" / "package_manager.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'os.environ.get("GMS_AGENT_ENROLL_CODE", "").strip()',
            pm_source,
        )

    def test_rendered_installer_is_tls_fail_closed_by_default(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from features.system.api import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, base_url="https://controller:5001")

        rendered = client.get("/api/agent/install.sh")
        self.assertEqual(rendered.status_code, 200, rendered.text)

        with tempfile.TemporaryDirectory() as temporary:
            # stub curl:记录参数后假装下载成功(空文件即可让 python3 阶段退出)。
            stub_dir = Path(temporary) / "bin"
            stub_dir.mkdir()
            curl_log = Path(temporary) / "curl.log"
            (stub_dir / "curl").write_text(
                "#!/bin/bash\nprintf '%s\\n' \"$@\" > " + repr(str(curl_log)) + "\nexit 0\n",
                encoding="utf-8",
            )
            (stub_dir / "curl").chmod(0o755)
            script = Path(temporary) / "install.sh"
            script.write_text(rendered.text, encoding="utf-8")
            result = subprocess.run(
                ["bash", str(script)],
                env={
                    "PATH": f"{stub_dir}:/usr/bin:/bin",
                    "HOME": temporary,
                    "GMS_INSTALL_ALLOW_INSECURE": "0",
                },
                capture_output=True,
                text=True,
                timeout=20,
            )
            args = curl_log.read_text(encoding="utf-8") if curl_log.exists() else ""

        # python3 对空 bootstrap 失败是预期;关键验证发生在 curl 传参上。
        self.assertNotEqual(result.returncode, 0)
        # 严格模式:默认系统信任链,无 -k 降级。
        self.assertIn("-fsSL", args)
        self.assertNotIn("-k", args.split())
        # 未显式请求降级时,不得向 python 阶段传导 insecure。
        self.assertNotIn("跳过 TLS 证书校验", result.stderr)

    def test_rendered_installer_rejects_insecure_when_production(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from features.system.api import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, base_url="https://controller:5001")

        rendered = client.get("/api/agent/install.sh")
        self.assertEqual(rendered.status_code, 200)
        # 测试进程默认非 production:占位符被实际值替换。
        self.assertIn("ALLOW_INSECURE='1'", rendered.text)

        with unittest.mock.patch(
            "foundation.runtime_settings.runtime_environment",
            return_value="production",
        ):
            production_render = client.get("/api/agent/install.sh")
        self.assertEqual(production_render.status_code, 200)
        self.assertIn("ALLOW_INSECURE='0'", production_render.text)
        # production 渲染下,insecure 请求直接失败(exit 4)。
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "install.sh"
            script.write_text(production_render.text, encoding="utf-8")
            result = subprocess.run(
                ["bash", str(script)],
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": temporary,
                    "GMS_INSTALL_ALLOW_INSECURE": "1",
                },
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("生产环境", result.stderr)


if __name__ == "__main__":
    unittest.main()
