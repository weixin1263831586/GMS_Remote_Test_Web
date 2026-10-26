"""Repair each OCR environment variable without requiring a fresh process."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from features.redmine import tesseract_finder as finder


@pytest.fixture
def bundled_env(tmp_path, monkeypatch):
    root = tmp_path / "tesseract"
    monkeypatch.setattr(finder, "_tesseract_dir", lambda: root)
    monkeypatch.setattr(finder, "bundled_tesseract_cmd", lambda: str(root / "tesseract"))
    monkeypatch.setattr(finder, "_configured", None)
    monkeypatch.setenv("LD_LIBRARY_PATH", str(root))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    return root


def test_library_path_alone_is_not_a_complete_ocr_environment(bundled_env):
    assert finder.configure_bundled_tesseract()
    assert os.environ["TESSDATA_PREFIX"] == str(bundled_env / "tessdata")
    assert os.environ["PATH"].split(os.pathsep)[0] == str(bundled_env)


@pytest.mark.parametrize("key", ["LD_LIBRARY_PATH", "PATH", "TESSDATA_PREFIX"])
def test_independently_removed_values_are_restored_after_configuration(bundled_env, monkeypatch, key):
    assert finder.configure_bundled_tesseract()
    monkeypatch.delenv(key)
    assert finder.configure_bundled_tesseract()
    expected = str(bundled_env / "tessdata") if key == "TESSDATA_PREFIX" else str(bundled_env)
    assert os.environ[key].split(os.pathsep)[0] == expected


def test_wrong_tessdata_directory_is_corrected_and_paths_remain_idempotent(bundled_env, monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/other/lib" + os.pathsep + str(bundled_env))
    monkeypatch.setenv("TESSDATA_PREFIX", "/wrong/tessdata")
    for _ in range(3):
        assert finder.configure_bundled_tesseract()
    assert os.environ["TESSDATA_PREFIX"] == str(bundled_env / "tessdata")
    for key in ("LD_LIBRARY_PATH", "PATH"):
        assert os.environ[key].split(os.pathsep).count(str(bundled_env)) == 1
    assert "/other/lib" in os.environ["LD_LIBRARY_PATH"]
    assert "/usr/bin" in os.environ["PATH"]


def test_real_bundled_engine_can_load_languages_after_partial_environment_repair(monkeypatch):
    command = finder.bundled_tesseract_cmd()
    if command is None:
        pytest.skip("bundled tesseract is not installed")
    root = finder._tesseract_dir()
    monkeypatch.setattr(finder, "_configured", None)
    with patch.dict(os.environ, {"LD_LIBRARY_PATH": str(root), "PATH": "/usr/bin"}, clear=True):
        assert finder.configure_bundled_tesseract()
        result = subprocess.run([command, "--list-langs"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    languages = result.stdout.splitlines()
    assert "eng" in languages and "chi_sim" in languages
