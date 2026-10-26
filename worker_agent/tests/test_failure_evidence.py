from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from worker_agent.failure_evidence import collect_failure_evidence


def _snapshot_writer(serial: str, device_dir: Path) -> list[Path]:
    path = device_dir / "snapshot_getprop.txt"
    path.write_text("prop", encoding="utf-8")
    return [path]


def test_collect_failure_evidence_sanitizes_serial_into_safe_directory(tmp_path):
    """serial 不得信任为路径段："../" 或绝对路径会写出 work_dir 之外。"""
    import hashlib

    work_dir = tmp_path / "job-1"
    uploaded = []
    serials = ["../escape", "hub-prefix:ABC-1", "normal-1"]

    with patch(
        "worker_agent.failure_evidence._device_snapshot",
        side_effect=_snapshot_writer,
    ), patch(
        "worker_agent.failure_evidence._logcat_window_files", return_value=[]
    ):
        result = collect_failure_evidence(
            "job-1", "attempt-1", serials, work_dir,
            lambda *args: uploaded.append(args), lambda action: action(),
        )

    evidence_root = work_dir / "failure-evidence"
    # 目录名 = 消毒后的 serial 标签 + serial 内容摘要；不含任何路径分隔符
    # 或 ".." 段，取证文件全部落在 work_dir 之内。
    expected = {
        f"device-___escape-{hashlib.sha256(b'../escape').hexdigest()[:12]}",
        (
            "device-hub-prefix_ABC-1-"
            f"{hashlib.sha256(b'hub-prefix:ABC-1').hexdigest()[:12]}"
        ),
        f"device-normal-1-{hashlib.sha256(b'normal-1').hexdigest()[:12]}",
    }
    assert {path.name for path in evidence_root.iterdir()} == expected
    assert all("/" not in name and ".." not in name for name in expected)
    assert not (tmp_path / "escape").exists()
    assert result
    assert all(
        Path(item["path"]).is_relative_to(evidence_root) for item in result
    )
    assert len(uploaded) == len(serials)
