"""A selected profile replaces the entire ambient transport identity."""

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ("runtime" if (ROOT / "runtime").is_dir() else "scripts")
sys.path.insert(0, str(RUNTIME))

import mcp_launcher  # noqa: E402
from gms_agent import profile_store  # noqa: E402


def _profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_store, "PROFILE_ROOT", tmp_path / "config/gms-agent/profiles")
    monkeypatch.setattr(profile_store, "STATE_DIR", tmp_path / "state/gms-remote-test")
    for name in ("old", "selected"):
        profile_store.write_profile_toml(name, "codex", f"https://{name}.example:5001", "", insecure=False)
        token = profile_store.token_file(name)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("synthetic-" + name)
        token.chmod(0o600)


def test_cli_profile_overrides_foreign_token_and_tls(tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GMS_", "XDG_"))}
    env.update({"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "GMS_AUTH_TOKEN_FILE": str(profile_store.token_file("old")),
                "GMS_CURL_CA_CERT": "/unused/foreign-ca", "GMS_CURL_INSECURE": "1"})
    result = subprocess.run(
        ["bash", str(RUNTIME / "gms-remote-test.sh"), "--profile", "selected",
         "gms-rt-auth-credential-mode", "--json"],
        env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["token_file"] == str(profile_store.token_file("selected"))
    # Source as a library as well: these are the actual curl TLS inputs.
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; printf "%s\\n" "$GMS_CURL_CA_CERT" "$GMS_CURL_INSECURE"',
         "profile-check", str(RUNTIME / "gms-remote-test.sh")],
        env={**env, "GMS_RT_PROFILE": "selected"}, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == ["", "0"]


def test_mcp_named_profile_clears_ambient_tls(tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch)
    monkeypatch.setenv("GMS_CURL_CA_CERT", "/unused/foreign-ca")
    monkeypatch.setenv("GMS_CURL_INSECURE", "1")
    monkeypatch.setenv("GMS_AUTH_TOKEN_FILE", str(profile_store.token_file("old")))
    assert mcp_launcher.load_named_profile("selected", "codex")
    assert os.environ["GMS_AUTH_TOKEN_FILE"] == str(profile_store.token_file("selected"))
    assert os.environ["GMS_CURL_CA_CERT"] == ""
    assert os.environ["GMS_CURL_INSECURE"] == "0"
