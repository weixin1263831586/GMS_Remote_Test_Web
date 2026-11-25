"""Hard source-line limit and shrink-only effective-limit comparison."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime
from pathlib import Path


BASELINES_DIR = Path(__file__).resolve().parent / "baselines"
MAX_SOURCE_LINES = 2000
BACKEND_SOURCE_SUFFIXES = {".py", ".sh", ".bash", ".bat", ".cmd", ".ps1", ".rs", ".java", ".gradle",
                           ".yml", ".yaml", ".toml", ".service", ".timer", ".c", ".h", ".cc", ".cpp",
                           ".hpp", ".kt", ".kts", ".go", ".sql"}
FRONTEND_SOURCE_SUFFIXES = {".html", ".js", ".css", ".ts", ".mjs", ".cjs", ".jsx", ".tsx", ".vue", ".svelte"}
THIRD_PARTY_PREFIXES = ("web/static/vendor/", "tools/jadx/", "tools/tesseract/")
REPO_ROOT = BASELINES_DIR.parents[2]


def load_baseline(name: str) -> dict:
    """读取 baselines/<name>；结构异常直接抛错（门禁配置错误必须翻红）。"""
    path = BASELINES_DIR / name
    data = json.loads(path.read_text(encoding="utf-8"))
    ceilings = data.get("ceilings")
    if not isinstance(ceilings, dict):
        raise ValueError(f"{path}: missing 'ceilings' mapping")
    waivers = data.get("waivers") or {}
    if not isinstance(waivers, dict):
        raise ValueError(f"{path}: 'waivers' must be a mapping")
    if data.get("default_limit") != MAX_SOURCE_LINES:
        raise ValueError(f"{path}: default_limit must be exactly {MAX_SOURCE_LINES}")
    if any(type(value) is not int or not 0 <= value <= MAX_SOURCE_LINES for value in ceilings.values()):
        raise ValueError(f"{path}: per-file ceilings must not exceed {MAX_SOURCE_LINES}")
    return data


def _waiver_valid(waiver: dict, *, today: date) -> str:
    """返回空串表示豁免有效；否则返回不通过的原因。"""
    reason = str(waiver.get("reason") or "").strip()
    if not reason:
        return "missing reason"
    expires_raw = str(waiver.get("expires") or "").strip()
    try:
        expires = date.fromisoformat(expires_raw[:10])
    except ValueError:
        return f"unparseable expires {expires_raw!r}"
    if expires < today:
        return f"expired {expires.isoformat()}"
    return ""


def tracked_source_paths(suffixes: set[str]) -> list[Path]:
    """All tracked first-party source files; exclude only bundled third-party assets."""
    proc = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True,
    )
    paths = []
    for name in set(os.fsdecode(proc.stdout).split("\0")):
        if not name or name.startswith(THIRD_PARTY_PREFIXES):
            continue
        path = REPO_ROOT / name
        if path.suffix.lower() in suffixes:
            paths.append(path)
        elif ".sh" in suffixes and not path.suffix and path.is_file():
            with path.open("rb") as handle:
                if handle.read(2) == b"#!":
                    paths.append(path)
    return sorted(paths)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _comparison_ref() -> str | None:
    """CI compares against the previous push or PR base, never the checked-out HEAD."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        candidate = event.get("pull_request", {}).get("base", {}).get("sha") or event.get("before")
        if candidate and candidate.strip("0"):
            return _git("rev-parse", "--verify", candidate + "^{commit}")
    # cat-file exposes the parent even in shallow clones; a missing parent must fail.
    parents = [line.split()[1] for line in _git("cat-file", "-p", "HEAD").splitlines()
               if line.startswith("parent ")]
    return _git("rev-parse", "--verify", parents[0] + "^{commit}") if parents else None


def _assert_effective_limits(previous: dict, current: dict, existing: set[str], today: date) -> None:
    # Removing a registered ceiling must not raise a surviving file's effective limit.
    if previous.get("default_limit") != MAX_SOURCE_LINES:
        return
    old = previous.get("ceilings") or {}
    new = current["ceilings"]
    waivers = current.get("waivers") or {}
    violations = []
    for key in sorted((set(old) | set(new)) & existing):
        old_limit = old.get(key, MAX_SOURCE_LINES)
        new_limit = new.get(key, MAX_SOURCE_LINES)
        if new_limit <= old_limit:
            continue
        waiver = waivers.get(key)
        problem = _waiver_valid(waiver, today=today) if isinstance(waiver, dict) else "no waiver"
        if problem:
            violations.append(f"{key}: {old_limit} -> {new_limit} ({problem})")
    if violations:
        raise AssertionError(f"size-ratchet violation: {violations}")


def assert_no_ceiling_raises(name: str, *, today: date | None = None) -> None:
    current = load_baseline(name)
    ref = _comparison_ref()
    if ref is None:
        return
    rel_path = f"tests/architecture/baselines/{name}"
    if not _git("ls-tree", "--name-only", ref, "--", rel_path):
        return  # first registration only; the hard limit is already enforced.
    previous = json.loads(_git("show", f"{ref}:{rel_path}"))
    existing = {path.relative_to(REPO_ROOT).as_posix()
                for path in tracked_source_paths(BACKEND_SOURCE_SUFFIXES | FRONTEND_SOURCE_SUFFIXES)}
    _assert_effective_limits(previous, current, existing, today or date.today())


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")
