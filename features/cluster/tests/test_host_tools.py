from __future__ import annotations

from pathlib import Path
import shutil

from scripts.configure_gms_host_tools import END, START, configure_bashrc
from scripts.extract_zip_preserve_mode import extract_archive


PROJECT_ROOT = Path(__file__).resolve().parents[3]
HOST_TOOLS = PROJECT_ROOT / "tools/GMS-Host-Tools"


def test_host_tools_archives_contain_every_required_runtime(tmp_path):
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
    assert (HOST_TOOLS / "gts-rockchip.json").is_file()


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

    assert 'install -m 600 "${HOST_TOOLS_SOURCE}/gts-rockchip.json"' in installer
    assert "Environment=APE_API_KEY=${SOFTWARE_ROOT}/gts-rockchip.json" in installer
    assert 'export APE_API_KEY="${GMS_SOFTWARE_ROOT}/gts-rockchip.json"' in environment


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


def test_worker_installer_replaces_read_only_tool_directories_before_extracting():
    installer = (PROJECT_ROOT / "scripts/install_cluster_worker.sh").read_text(
        encoding="utf-8"
    )

    assert 'rm -rf "${SOFTWARE_ROOT}/jdk-11" "${SOFTWARE_ROOT}/platform-tools"' in installer
    assert 'glob("modules.part.*")' in installer
