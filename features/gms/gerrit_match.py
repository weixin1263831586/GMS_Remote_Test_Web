"""Match Critical Fix bulletin rows to Gerrit revisions or project and subject.

Keep row evidence separate from bug mentions and from Android version scope.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, unquote


_COMMIT_URL = re.compile(
    r"https://(?:partner-android|android)\.googlesource\.com/"
    r"(?P<project>.+?)/\+/(?P<sha>[0-9a-f]{40})",
    re.IGNORECASE,
)
_BUG_ID = re.compile(r"\AA-(\d{5,12})\Z")
_SHA = re.compile(r"\A[0-9a-f]{40}\Z")
_CACHE_TTL_SECONDS = 900
_CACHE_MAX_ENTRIES = 128
_QUERY_CHANGES = Callable[..., Awaitable[dict[str, Any]]]

_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = asyncio.Lock()


def clear_gerrit_match_cache() -> None:
    _cache.clear()


def gerrit_is_configured(cfg: dict[str, Any]) -> bool:
    return bool(str(cfg.get("base_url") or "").strip() or str(cfg.get("ssh_host") or "").strip())


def upstream_commits(detail_text: str) -> list[dict[str, str]]:
    """Return unique googlesource commits in bulletin order."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _COMMIT_URL.finditer(detail_text or ""):
        sha = match.group("sha").lower()
        if sha in seen:
            continue
        seen.add(sha)
        found.append({"sha": sha, "project": unquote(match.group("project"))})
    return found


def project_key(project: str) -> str:
    """Compare ``android/platform/frameworks/base`` with the bulletin path."""
    text = unquote(str(project or "")).strip().strip("/")
    marker = "platform/"
    index = text.find(marker)
    return text[index:] if index >= 0 else text


def normalize_subject(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold().rstrip(" .")


_TABLE_HEADER = "Android version Subject Project Download"
_TABLE_ROW = re.compile(
    r"(?P<version>\d+(?:\s+QPR\d+)?)\s+(?P<subject>.+?)\s+"
    r"(?P<project>platform/\S+)\s+Download",
    re.IGNORECASE,
)
_PROSE_ROW = re.compile(
    r"(?P<subject>[^\n]{8,180}?)\s+Project:\s+(?P<project>platform/\S+)\s+Download",
    re.IGNORECASE,
)
_SUBJECT_UNSAFE = re.compile(r'["\\\x00-\x1f]')


def bulletin_subjects(detail_text: str) -> list[dict[str, str]]:
    """Return commit subjects from the bulletin's patch table, in order."""
    text = detail_text or ""
    if _TABLE_HEADER in text:
        matches = _TABLE_ROW.finditer(text.split(_TABLE_HEADER, 1)[1])
    elif "Project:" in text:
        matches = _PROSE_ROW.finditer(text)
    else:
        return []
    rows: list[dict[str, str]] = []
    for match in matches:
        subject = re.sub(r"\s+", " ", match.group("subject")).strip()
        if not subject or subject.endswith("Project:"):
            continue
        rows.append({
            "subject": subject, "project": unquote(match.group("project")),
            "version": str(match.groupdict().get("version") or "").upper(),
        })
    return rows


def upstream_patches(detail_text: str) -> list[dict[str, str]]:
    """Pair each googlesource commit with the subject of that table row."""
    commits = upstream_commits(detail_text)
    subjects = bulletin_subjects(detail_text)
    used = [False] * len(subjects)
    patches: list[dict[str, str]] = []
    for commit in commits:
        subject = ""
        version = ""
        key = project_key(commit["project"])
        for index, row in enumerate(subjects):
            if used[index] or project_key(row["project"]) != key:
                continue
            used[index] = True
            subject = row["subject"]
            version = row["version"]
            break
        patches.append({**commit, "subject": subject, "version": version})
    # A missing or inaccessible download link must not erase a bulletin row.
    for index, row in enumerate(subjects):
        if not used[index]:
            patches.append({"sha": "", **row})
    return patches


def _patch_key(patch: dict[str, str]) -> str:
    sha = str(patch.get("sha") or "")
    if sha and not patch.get("version"):
        return sha
    return "|".join((sha, project_key(patch.get("project", "")),
                     normalize_subject(patch.get("subject", "")), patch.get("version", "")))


def _project_matches(actual: str, expected: str) -> bool:
    actual, expected = project_key(actual), project_key(expected)
    if not actual or not expected:
        return False
    if actual == expected:
        return True
    if expected.startswith("platform/"):
        suffix = expected.removeprefix("platform/")
        # Only match a complete repository path, not a substring or basename.
        return "/" in suffix and (actual == suffix or actual.endswith("/" + suffix))
    return False


def _branch_version(branch: str) -> str:
    match = re.search(r"(?:^|/)(?:android[-_ ]?)?(1[0-9])(?:\.\d+)?(?:[-_ ]?qpr(\d+))?(?=/|$)",
                      branch, re.IGNORECASE)
    if not match:
        return ""
    return match.group(1) + (f" QPR{match.group(2)}" if match.group(2) else "")


def _version_matches(raw: dict[str, Any], patch: dict[str, str]) -> bool:
    expected = str(patch.get("version") or "").upper()
    actual = _branch_version(str(raw.get("branch") or ""))
    return not expected or actual == expected


def query_subject(subject: str) -> str:
    """Return a subject safe to place inside a quoted Gerrit query."""
    text = re.sub(r"\s+", " ", str(subject or "").strip())
    if len(text) < 12 or len(text) > 180 or _SUBJECT_UNSAFE.search(text):
        return ""
    return text


def bug_number(patch_id: str) -> str:
    match = _BUG_ID.fullmatch(str(patch_id or "").strip())
    return match.group(1) if match else ""


def text_has_bug(text: str, bug: str) -> bool:
    if not bug or not _BUG_ID.fullmatch(f"A-{bug}"):
        return False
    return re.search(rf"(?<!\d){re.escape(bug)}(?!\d)", text or "") is not None


def text_has_sha(text: str, sha: str) -> bool:
    if not _SHA.fullmatch(sha or ""):
        return False
    return re.search(
        rf"(?<![0-9a-f]){re.escape(sha)}(?![0-9a-f])", (text or "").lower(),
    ) is not None


def _commit_body(raw: dict[str, Any]) -> str:
    """Return the commit message when Gerrit actually sent one.

    The subject is not a substitute: a cherry-pick SHA usually lives in
    the body, and a subject that only contains a longer bug number must
    not be treated as proof of the shorter one.
    """
    message = str(raw.get("commitMessage") or "")
    if message:
        return message
    revisions = raw.get("revisions") or {}
    revision: dict[str, Any] | None = None
    if isinstance(revisions, dict) and revisions:
        current = raw.get("current_revision")
        selected = revisions.get(current) if current else None
        if isinstance(selected, dict):
            revision = selected
    commit = (revision or {}).get("commit") or {}
    if isinstance(commit, dict) and commit.get("message"):
        return str(commit["message"])
    return ""


def change_commit_message(raw: dict[str, Any]) -> str:
    return _commit_body(raw) or str(raw.get("subject") or "")


def change_revision(raw: dict[str, Any]) -> str:
    patchset = raw.get("currentPatchSet") or raw.get("current_patch_set") or {}
    if isinstance(patchset, dict) and patchset.get("revision"):
        return str(patchset["revision"]).lower()
    current = str(raw.get("current_revision") or "").lower()
    if _SHA.fullmatch(current):
        return current
    revisions = raw.get("revisions") or {}
    if isinstance(revisions, dict):
        selected = revisions.get(raw.get("current_revision")) or {}
        commit = selected.get("commit") if isinstance(selected, dict) else {}
        sha = str((commit or {}).get("commit") or "").lower()
        if _SHA.fullmatch(sha):
            return sha
    return ""


def change_matches_patch(raw: dict[str, Any], patch: dict[str, str]) -> str:
    """Return ``commit`` or ``subject`` when this change is that bulletin row.

    A bug number in the message is not enough. Partner SHAs often differ
    from the internal cherry-pick, so the same project plus the same
    commit subject is the other exact match.
    """
    sha = str(patch.get("sha") or "")
    if _SHA.fullmatch(sha) and (
        change_revision(raw) == sha or text_has_sha(change_commit_message(raw), sha)
    ):
        return "commit"
    expected = normalize_subject(patch.get("subject") or "")
    actual = normalize_subject(str(raw.get("subject") or ""))
    if not expected or expected != actual:
        return ""
    if not _project_matches(str(raw.get("project") or ""), patch.get("project") or ""):
        return ""
    return "subject"


def patches_query(patches: list[dict[str, str]]) -> str:
    """Build a status-bounded query for these bulletin commits."""
    clauses: list[str] = []
    for patch in patches:
        sha = str(patch.get("sha") or "")
        if _SHA.fullmatch(sha):
            clauses.append(f"commit:{sha}")
            clauses.append(f"message:{sha}")
        subject = query_subject(patch.get("subject") or "")
        if subject:
            clauses.append(f'subject:"{subject}"')
    if not clauses:
        raise ValueError("refusing a Gerrit query with no safe bulletin token")
    return f"({' OR '.join(clauses)}) (status:merged OR status:open)"


def token_query(tokens: list[str]) -> str:
    """Build a status-bounded Gerrit query from already-validated tokens."""
    clauses: list[str] = []
    for token in tokens:
        if _SHA.fullmatch(token):
            clauses.append(f"commit:{token}")
            clauses.append(f"message:{token}")
        elif _BUG_ID.fullmatch(f"A-{token}"):
            clauses.append(f"message:{token}")
    if not clauses:
        raise ValueError("refusing a Gerrit query with no safe bulletin token")
    return f"({' OR '.join(clauses)}) (status:merged OR status:open)"


def _row_status(changes: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "").upper() for item in changes}
    if "MERGED" in statuses:
        return "merged"
    if "NEW" in statuses or "OPEN" in statuses:
        return "open"
    return "missing"


def _changes_for_status(changes: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    if status == "merged":
        wanted = {"MERGED"}
    elif status == "open":
        wanted = {"NEW", "OPEN"}
    else:
        wanted = set()
    shown = [item for item in changes if str(item.get("status") or "").upper() in wanted]
    return shown[:3]


def _rollup(states: list[str]) -> str:
    if not states:
        return "unknown"
    if all(state == "merged" for state in states):
        return "merged"
    if any(state in {"merged", "partial"} for state in states):
        return "partial"
    if "unknown" in states:
        return "unknown"
    if "open" in states:
        return "open"
    return "missing"


def _version_key(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^Android\s+", "", value.strip(), flags=re.I)).upper()


def _expected_versions(value: str) -> list[str]:
    return list(dict.fromkeys(
        version for token in re.split(r"[,，]", value or "")
        if re.fullmatch(r"\d+(?: QPR\d+)?", version := _version_key(token))
    ))


def _bulletin_evidence(record: dict[str, str]) -> list[dict[str, str]]:
    """Read DOM row evidence, falling back conservatively for older indexes."""
    try:
        rows = json.loads(record.get("patch_rows_json") or "[]")
    except (TypeError, ValueError):
        rows = []
    patches = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            version = _version_key(str(row.get("version") or ""))
            subject = str(row.get("subject") or "")
            project = str(row.get("project") or "")
            if not version or not subject or not project:
                continue
            urls = row.get("urls") or []
            commits = upstream_commits("\n".join(str(url) for url in urls)) if isinstance(urls, list) else []
            matching = [item["sha"] for item in commits if project_key(item["project"]) == project_key(project)]
            patches.append({"version": version, "subject": subject, "project": project,
                            "sha": matching[0] if len(matching) == 1 else ""})
    if not patches:
        patches = upstream_patches(record.get("detail_text") or "")
    versions = _expected_versions(record.get("android_versions") or "")
    if len(versions) == 1:
        for patch in patches:
            if not patch.get("version"):
                patch["version"] = versions[0]
    return list({_patch_key(patch): patch for patch in patches}.values())


def _complete_merge_branches(
    pairs: list[tuple[dict[str, str], dict[str, Any]]],
    hits: dict[str, list[dict[str, Any]]],
) -> list[str]:
    branches = [
        {str(change["branch"]) for change in hits.get(_patch_key(patch), [])
         if change.get("status") == "MERGED" and change.get("branch")}
        for patch, _ in pairs
    ]
    return sorted(set.intersection(*branches)) if branches else []


def classify_patch(
    *,
    patches: list[dict[str, str]],
    hits: dict[str, list[dict[str, Any]]],
    truncated: set[str],
    expected_versions: list[str] | None = None,
    stale: bool = False,
) -> dict[str, Any]:
    upstreams: list[dict[str, Any]] = []
    grouped: dict[str, list[tuple[dict[str, str], dict[str, Any]]]] = {}
    for patch in patches:
        key = _patch_key(patch)
        changes = hits.get(key) or []
        status = _row_status(changes)
        if key in truncated and status == "missing":
            status = "unknown"
        version = str(patch.get("version") or "")
        row = {
            "project": project_key(patch.get("project") or ""),
            "subject": str(patch.get("subject") or ""),
            "version": version,
            "status": status,
            "changes": _changes_for_status(changes, status),
        }
        upstreams.append(row)
        grouped.setdefault(version, []).append((patch, row))

    versions = list(dict.fromkeys([*(expected_versions or []), *(v for v in grouped if v)]))
    version_results = []
    branch_coverage_complete = True
    for version in versions:
        pairs = grouped.get(version, [])
        states = [row["status"] for _, row in pairs]
        status = _rollup(states)
        complete_branches = _complete_merge_branches(pairs, hits) if status == "merged" else []
        if status == "merged" and not complete_branches:
            status = "partial"
            branch_coverage_complete = False
        if stale and status == "merged":
            status = "unknown"
        version_results.append({
            "version": version, "status": status,
            "upstream_total": len(states), "upstream_merged": states.count("merged"),
            "upstream_open": states.count("open"), "upstream_unknown": states.count("unknown"),
            "complete_branches": complete_branches,
        })
    states = [row["status"] for row in upstreams]
    if version_results:
        status = _rollup([row["status"] for row in version_results] +
                         (["unknown"] if grouped.get("") else []))
    else:
        status = _rollup(states)
        if status == "merged" and not _complete_merge_branches(grouped[""], hits):
            status = "partial"
            branch_coverage_complete = False
    if stale and status == "merged":
        status = "unknown"
    keys = [_patch_key(patch) for patch in patches]
    changes = _deduped_changes(hits, keys)
    return {
        "status": status, "basis": "patch",
        "upstream_total": len(patches), "upstream_merged": states.count("merged"),
        "upstream_open": states.count("open"),
        "branch_coverage_complete": branch_coverage_complete,
        "truncated": stale or not patches or any(key in truncated for key in keys)
                     or any(not row["upstream_total"] for row in version_results)
                     or bool(versions and grouped.get("")),
        "versions": version_results, "upstreams": upstreams,
        "changes": changes[:6], "change_count": len(changes),
    }


def _deduped_changes(
    hits: dict[str, list[dict[str, Any]]], tokens: list[str],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for token in tokens:
        for item in hits.get(token) or []:
            number = str(item.get("number") or "")
            key = number or str(item.get("url") or id(item))
            current = merged.get(key)
            if current is None:
                copied = dict(item)
                copied["via"] = list(item.get("via") or [])
                merged[key] = copied
                order.append(key)
                continue
            for label in item.get("via") or []:
                if label not in current["via"]:
                    current["via"].append(label)
    rows = [merged[key] for key in order]
    rows.sort(key=lambda item: (
        0 if item.get("status") == "MERGED" else 1,
        str(item.get("branch") or ""),
        str(item.get("number") or ""),
    ))
    return rows


def _public_change(cfg: dict[str, Any], raw: dict[str, Any], via: set[str]) -> dict[str, Any]:
    number = str(raw.get("_number") or raw.get("number") or "")
    status = str(raw.get("status") or "").upper()
    message = change_commit_message(raw)
    subject = str(raw.get("subject") or (message.splitlines()[0] if message else ""))
    return {
        "number": number,
        "status": status,
        "project": str(raw.get("project") or ""),
        "branch": str(raw.get("branch") or ""),
        "subject": subject[:160],
        "url": _change_url(cfg, raw, number),
        "via": sorted(via),
    }


def _change_url(cfg: dict[str, Any], raw: dict[str, Any], number: str) -> str:
    url = str(raw.get("url") or "")
    if url.startswith("https://") or url.startswith("http://"):
        return url
    base = str(cfg.get("base_url") or "").rstrip("/")
    if base and number and (base.startswith("https://") or base.startswith("http://")):
        return f"{base}/c/{quote(number, safe='')}"
    return ""


def _cache_key(cfg: dict[str, Any], patches: list[dict[str, str]]) -> tuple[str, str]:
    # A transport or credential change can alter which Gerrit changes are visible.
    # Keep only a digest of connection settings, never the password itself.
    identity = hashlib.sha256(json.dumps([
        cfg.get(field) for field in (
            "base_url", "ssh_host", "ssh_user", "ssh_port", "ssh_identity_file",
            "rest_username", "rest_password", "rest_verify_ssl", "rest_ca_cert",
            "_match_owner",
        )
    ], default=str).encode()).hexdigest()
    rows = []
    for patch in patches:
        parts = [
            f"{item['sha']}|{project_key(item['project'])}|{normalize_subject(item['subject'])}|{item.get('version', '')}"
            for item in _bulletin_evidence(patch)
        ]
        rows.append(f"v5|{patch.get('record_key') or patch.get('patch_id')}|{patch.get('android_versions')}|{patch.get('detail_status')}|{' ; '.join(parts)}")
    digest = hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()
    return identity, digest


async def _collect_hits(
    cfg: dict[str, Any],
    patches: list[dict[str, str]],
    query_changes: _QUERY_CHANGES,
) -> tuple[dict[str, list[dict[str, Any]]], set[str], str]:
    keys = [_patch_key(patch) for patch in patches]
    hits: dict[str, list[dict[str, Any]]] = {key: [] for key in keys}
    seen: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in keys}
    truncated: set[str] = set()
    queried: set[str] = set()
    error = ""
    queue = [patches[index:index + 8] for index in range(0, len(patches), 8)]
    steps = 0
    deadline = time.monotonic() + 55
    while queue and steps < 80 and time.monotonic() < deadline:
        batch = queue.pop(0)
        if not batch:
            continue
        steps += 1
        batch_keys = {_patch_key(patch) for patch in batch}
        try:
            query = patches_query(batch)
            result = await asyncio.wait_for(
                query_changes(cfg, query, 120, 60, commit_message=True),
                timeout=max(0.01, min(15, deadline - time.monotonic())),
            )
            failure = str(result.get("error") or "")
        except Exception as exc:
            result = {}
            failure = str(exc) or type(exc).__name__
        items = list(result.get("items") or [])
        more = bool((result.get("stats") or {}).get("moreChanges"))
        # A rejected query or result cap must not invalidate unrelated rows.
        if len(batch) > 1 and (failure or more):
            middle = len(batch) // 2
            queue.extend((batch[:middle], batch[middle:]))
            continue
        queried.update(batch_keys)
        if failure:
            error = error or failure
        if failure or more:
            truncated.update(batch_keys)
        for raw in items:
            if not isinstance(raw, dict):
                continue
            for patch in batch:
                key = _patch_key(patch)
                via = change_matches_patch(raw, patch)
                if not via:
                    if _SHA.fullmatch(patch.get("sha") or "") and not _commit_body(raw):
                        # A hidden commit body may contain the upstream SHA even
                        # when a rewritten subject does not match the bulletin.
                        truncated.add(key)
                    continue
                if not _version_matches(raw, patch):
                    # Explicitly different versions are not hits. Unmapped
                    # branches/QPRs cannot prove absence or presence.
                    actual = _branch_version(str(raw.get("branch") or ""))
                    if not actual or actual.split()[0] == str(patch.get("version") or "").split()[0]:
                        truncated.add(key)
                    continue
                public = _public_change(cfg, raw, {via})
                if public["status"] not in {"MERGED", "NEW", "OPEN"}:
                    truncated.add(key)
                    continue
                number = public["number"] or public["url"] or public["subject"]
                current = seen[key].get(number)
                if current is None:
                    seen[key][number] = public
                    hits[key].append(public)
                elif via not in current["via"]:
                    current["via"].append(via)
    truncated.update(set(keys) - queried)
    if queue:
        error = error or "Gerrit 核对达到时间或查询次数上限，请重新核对"
    return hits, truncated, error[:300]


async def match_critical_patches(
    patches: list[dict[str, str]],
    cfg: dict[str, Any],
    query_changes: _QUERY_CHANGES,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return one merge judgement per patch id.

    ``query_changes`` is the Gerrit search callable. Results are cached by
    Gerrit target and bulletin evidence so reopening the page does not
    repeat the search. Failed lookups are not cached.
    """
    if not gerrit_is_configured(cfg):
        return {
            "configured": False,
            "cached": False,
            "checked_at": "",
            "error": "Gerrit 未配置。请先在 Gerrit 看板保存 REST 或 SSH 连接。",
            "items": {},
        }
    key = _cache_key(cfg, patches)
    now = time.monotonic()
    if not refresh:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return {**cached[1], "cached": True}
    evidence: list[dict[str, Any]] = []
    query_patches: list[dict[str, str]] = []
    seen_patch_keys: set[str] = set()
    for patch in patches:
        patch_id = str(patch.get("patch_id") or "")
        commits = _bulletin_evidence(patch)
        evidence.append({"patch_id": str(patch.get("record_key") or patch_id), "patches": commits,
                         "versions": _expected_versions(patch.get("android_versions") or ""),
                         "stale": patch.get("detail_status") in {"stale", "unavailable", "not_scanned"}})
        for commit in commits:
            patch_key = _patch_key(commit)
            if patch_key not in seen_patch_keys:
                seen_patch_keys.add(patch_key)
                query_patches.append(commit)
    async with _cache_lock:
        if not refresh:
            cached = _cache.get(key)
            if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
                return {**cached[1], "cached": True}
        hits, truncated, error = await _collect_hits(cfg, query_patches, query_changes)
    items = {
        row["patch_id"]: classify_patch(
            patches=row["patches"], hits=hits, truncated=truncated,
            expected_versions=row["versions"], stale=row["stale"],
        )
        for row in evidence
        if row["patch_id"]
    }
    payload = {
        "configured": True,
        "cached": False,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "error": error,
        "items": items,
    }
    if not error:
        now = time.monotonic()
        for expired in [entry for entry, (saved, _) in _cache.items()
                        if now - saved >= _CACHE_TTL_SECONDS]:
            del _cache[expired]
        # Refresh moves a key to the newest position; evict oldest entries first.
        _cache.pop(key, None)
        while len(_cache) >= _CACHE_MAX_ENTRIES:
            del _cache[next(iter(_cache))]
        _cache[key] = (now, payload)
    return payload
