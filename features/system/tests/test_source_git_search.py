"""Exercise actual pipes, deadlines, and record framing for SDK searches."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from features.system import source_git_search
from features.system.source_provider_contract import SourceProviderError


COMMIT = "a" * 40
RECORD = (COMMIT + ":vendor:chip.c\0" + "7\0match text\n").encode()


def _fake_git(monkeypatch, code):
    popen = subprocess.Popen
    children = []

    def spawn(_argv, **kwargs):
        child = popen([sys.executable, "-c", code], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(source_git_search.subprocess, "Popen", spawn)
    return children


def _search(**kwargs):
    options = {"path_filter": "", "limit": 2, "timeout": 2}
    options.update(kwargs)
    return source_git_search.grep_matches(
        "/unused", COMMIT, "match", **options,
    )


@pytest.mark.parametrize("prefix", [b"", b"partial header", RECORD + b"unfinished"])
def test_deadline_kills_a_process_with_silent_or_partial_output(monkeypatch, prefix):
    children = _fake_git(
        monkeypatch, f"import os,time; os.write(1,{prefix!r}); time.sleep(10)",
    )
    started = time.monotonic()
    matches, _, limited = _search(timeout=0.3)
    assert time.monotonic() - started < 2
    assert limited
    assert len(matches) == (1 if prefix.startswith(RECORD) else 0)
    assert children[0].poll() is not None


def test_stderr_is_drained_while_stdout_record_is_fragmented(monkeypatch):
    code = (
        f"import os,time; r={RECORD!r}; os.write(1,r[:12]); "
        "os.write(2,b'e'*300_000); time.sleep(0.03); os.write(1,r[12:])"
    )
    _fake_git(monkeypatch, code)
    matches, scanned, limited = _search()
    assert not limited
    assert scanned == 1
    assert matches == [{"path": "vendor:chip.c", "line": 7, "snippet": "match text"}]


def test_extra_match_limits_results_and_reaps_child(monkeypatch):
    children = _fake_git(monkeypatch, f"import os,time; os.write(1,{RECORD!r}*3); time.sleep(10)")
    matches, _, limited = _search()
    assert len(matches) == 2 and limited
    assert children[0].poll() is not None


def test_incomplete_record_is_an_upstream_error(monkeypatch):
    _fake_git(monkeypatch, "import os; os.write(1,b'incomplete')")
    with pytest.raises(SourceProviderError, match="输出不完整"):
        _search()


def test_large_unterminated_record_is_bounded(monkeypatch):
    monkeypatch.setattr(source_git_search, "MAX_BLOB_BYTES", 128)
    children = _fake_git(monkeypatch, "import os,time; os.write(1,b'x'*256); time.sleep(10)")
    matches, _, limited = _search()
    assert not matches and limited
    assert children[0].poll() is not None


def test_empty_search_exits_cleanly(monkeypatch):
    _fake_git(monkeypatch, "raise SystemExit(1)")
    assert _search() == ([], 0, False)


def test_git_failure_preserves_semantic_error(monkeypatch):
    _fake_git(monkeypatch, "import os; os.write(2,b'fixture failure'); raise SystemExit(2)")
    with pytest.raises(SourceProviderError, match="fixture failure") as exc:
        _search()
    assert exc.value.status_code == 502
