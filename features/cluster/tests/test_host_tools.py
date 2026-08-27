from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.configure_gms_host_tools import END, START, configure_bashrc
from scripts.extract_zip_preserve_mode import extract_archive


PROJECT_ROOT = Path(__file__).resolve().parents[3]
HOST_TOOLS = PROJECT_ROOT / "tools/GMS-Host-Tools"
PREPARE_SCRIPT = PROJECT_ROOT / "scripts/prepare_gms_host_tools.sh"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_only_non_sensitive_host_tool_helpers_are_tracked():
    tracked = subprocess.run(
        ["git", "ls-files", "tools/GMS-Host-Tools"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert tracked == [
        "tools/GMS-Host-Tools/README.md",
        "tools/GMS-Host-Tools/env.sh",
        "tools/GMS-Host-Tools/verify.sh",
    ]


def test_prepare_host_tools_fetches_verified_artifacts(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    jdk_tree = source / "jdk-11"
    (jdk_tree / "bin").mkdir(parents=True)
    (jdk_tree / "legal").mkdir()
    (jdk_tree / "bin/java").write_text("#!/bin/sh\n", encoding="utf-8")
    (jdk_tree / "bin/java").chmod(0o755)
    (jdk_tree / "release").write_text('JAVA_VERSION="11"\n', encoding="utf-8")
    (jdk_tree / "legal/LICENSE").write_text("GPLv2", encoding="utf-8")
    jdk_archive = source / "jdk.tar.gz"
    with tarfile.open(jdk_archive, "w:gz") as archive:
        archive.add(jdk_tree, arcname="jdk-11")

    platform_archive = source / "platform-tools.zip"
    with zipfile.ZipFile(platform_archive, "w") as archive:
        archive.writestr("platform-tools/NOTICE.txt", "fixture")

    project = tmp_path / "project"
    (project / "tools/GMS-Host-Tools").mkdir(parents=True)
    env = {
        **os.environ,
        "GMS_HOST_TOOLS_ALLOW_FILE": "1",
        "GMS_HOST_TOOLS_JDK_URL": jdk_archive.as_uri(),
        "GMS_HOST_TOOLS_JDK_SHA256": _sha256(jdk_archive),
        "GMS_HOST_TOOLS_PLATFORM_URL": platform_archive.as_uri(),
        "GMS_HOST_TOOLS_PLATFORM_SHA256": _sha256(platform_archive),
    }
    subprocess.run([PREPARE_SCRIPT, project], env=env, check=True)

    assert (project / "tools/GMS-Host-Tools/jdk-11/bin/java").is_file()
    assert (project / "tools/GMS-Host-Tools/jdk-11/legal/LICENSE").is_file()
    assert (project / "tools/GMS-Host-Tools/platform-tools-gms-linux.zip").is_file()


def test_prepare_host_tools_rejects_checksum_mismatch(tmp_path):
    source = tmp_path / "platform-tools.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("platform-tools/NOTICE.txt", "fixture")
    project = tmp_path / "project"
    host_tools = project / "tools/GMS-Host-Tools"
    (host_tools / "jdk-11/bin").mkdir(parents=True)
    java = host_tools / "jdk-11/bin/java"
    java.write_text("#!/bin/sh\n", encoding="utf-8")
    java.chmod(0o755)
    completed = subprocess.run(
        [PREPARE_SCRIPT, project],
        env={
            **os.environ,
            "GMS_HOST_TOOLS_ALLOW_FILE": "1",
            "GMS_HOST_TOOLS_PLATFORM_URL": source.as_uri(),
            "GMS_HOST_TOOLS_PLATFORM_SHA256": "0" * 64,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "SHA256 verification failed" in completed.stderr


def test_host_tools_archives_contain_every_required_runtime(tmp_path):
    if not (HOST_TOOLS / "jdk-11").is_dir() or not (
        HOST_TOOLS / "platform-tools-gms-linux.zip"
    ).is_file():
        pytest.skip("deployment-only host tools are intentionally untracked")
    shutil.copytree(HOST_TOOLS / "jdk-11", tmp_path / "jdk-11")
    lib_dir = tmp_path / "jdk-11/lib"
    parts = sorted(lib_dir.glob("modules.part.*"))
    assert parts
    with (lib_dir / "modules").open("wb") as output:
        for part in parts:
            output.write(part.read_bytes())
    extract_archive(HOST_TOOLS / "platform-tools-gms-linux.zip", tmp_path)

    required = (
        "jdk-11/bin/java",
        "platform-tools/adb",
        "platform-tools/fastboot",
        "platform-tools/aapt",
        "platform-tools/aapt2",
        "platform-tools/lib64/libc++.so",
    )
    assert all((tmp_path / name).is_file() for name in required)
    assert all((tmp_path / name).stat().st_mode & 0o111 for name in required[:-1])


def test_bashrc_configuration_replaces_legacy_paths_and_is_idempotent(tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(
        "export JAVA_HOME=/home/old/Software/jdk-11\n"
        "export JRE_HOME=${JAVA_HOME}/jre\n"
        "export CLASSPATH=.:${JAVA_HOME}/lib:${JRE_HOME}/lib\n"
        "export PATH=${JAVA_HOME}/bin:$PATH\n"
        "export PATH=/home/old/Software/android-sdk-linux/tools:$PATH\n"
        'export PATH="$HOME/.local/bin:$HOME/Software/custom-tools:$PATH"\n'
        "export APE_API_KEY=/secure/gts.json\n",
        encoding="utf-8",
    )

    configure_bashrc(bashrc)
    configure_bashrc(bashrc)
    result = bashrc.read_text(encoding="utf-8")

    assert result.count(START) == 1
    assert result.count(END) == 1
    assert "android-sdk-linux" not in result
    assert "JRE_HOME" not in result
    assert "${JAVA_HOME}/bin" not in result
    assert "$HOME/Software/custom-tools" in result
    assert "export APE_API_KEY=/secure/gts.json" in result


def test_worker_installer_protects_and_exports_gts_credential():
    installer = (PROJECT_ROOT / "scripts/install_cluster_worker.sh").read_text(
        encoding="utf-8"
    )
    environment = (HOST_TOOLS / "env.sh").read_text(encoding="utf-8")

    assert 'install -m 600 "${GTS_CREDENTIAL_FILE}"' in installer
    assert "GTS credential must be supplied" in installer
    assert "Environment=APE_API_KEY=${SOFTWARE_ROOT}/gts-rockchip.json" in installer
    # env.sh 只引用部署侧解耦后的凭据；仓库/代码包内不再携带真实凭据。
    assert '[[ -f "${GMS_SOFTWARE_ROOT}/gts-rockchip.json" ]]' in environment
    assert "APE_API_KEY" in environment


def test_worker_installer_provisions_a_headless_x_display():
    installer = (PROJECT_ROOT / "scripts/install_cluster_worker.sh").read_text(
        encoding="utf-8"
    )

    assert "x11vnc xvfb novnc websockify" in installer
    assert "gms-worker-xvfb.service" in installer
    assert "Xvfb :99 -screen 0 1920x1080x24" in installer
    assert '"${session_type}" == "x11"' in installer
    assert 'display="${display:-:99}"' in installer
    assert 'if [[ "${display}" != ":99" && -n "${auth}"' in installer
    assert "IPAddressDeny=any" in installer
    assert "IPAddressAllow=${CONTROLLER_ALLOW_ADDRESSES}" in installer
    assert "--token-plugin=TokenFile" in installer


def test_worker_installer_replaces_read_only_tool_directories_before_extracting():
    installer = (PROJECT_ROOT / "scripts/install_cluster_worker.sh").read_text(
        encoding="utf-8"
    )

    assert 'rm -rf "${SOFTWARE_ROOT}/jdk-11" "${SOFTWARE_ROOT}/platform-tools"' in installer
    assert 'glob("modules.part.*")' in installer
