"""Read bounded git-grep output without blocking on partial records."""

from __future__ import annotations

import os
import selectors
import subprocess
import time

from .source_provider_contract import MAX_BLOB_BYTES, SourceProviderError


def grep_matches(
    repo_root: str, commit: str, query: str, *, path_filter: str, limit: int, timeout: float,
) -> tuple[list[dict], int, bool]:
    # NUL separates the filename and line number, so punctuation and newlines
    # in Git filenames cannot change the record's meaning.
    try:
        process = subprocess.Popen(
            [
                "git", "-C", repo_root, "grep", "--fixed-strings", "--ignore-case",
                "--no-color", "--no-heading", "--no-break", "--no-column", "--full-name",
                "-z", "-n", "-I", "-e", query, commit, "--",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
    except OSError as exc:
        raise SourceProviderError("git grep 启动失败", status_code=502) from exc

    matches: list[dict] = []
    scanned = 0
    limited = False
    pending = bytearray()
    fields: list[bytes] = []
    stderr_tail = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() and not limited:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    limited = True
                    break
                ready = selector.select(remaining)
                if not ready:
                    limited = True
                    break
                for key, _ in ready:
                    try:
                        block = os.read(key.fd, 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is process.stderr:
                        # Drain stderr concurrently, retaining only a bounded
                        # diagnostic tail instead of filling its pipe.
                        stderr_tail.extend(block)
                        del stderr_tail[:-2048]
                        continue
                    pending.extend(block)
                    consumed = 0
                    while True:
                        delimiter = b"\0" if len(fields) < 2 else b"\n"
                        end = pending.find(delimiter, consumed)
                        if end < 0:
                            break
                        fields.append(bytes(pending[consumed:end]))
                        consumed = end + 1
                        if len(fields) != 3:
                            continue
                        label, line_number, snippet = fields
                        fields = []
                        scanned += 1
                        prefix = (commit + ":").encode()
                        if not label.startswith(prefix) or not line_number.isdigit():
                            raise SourceProviderError("git grep 输出格式无效", status_code=502)
                        path = label[len(prefix):].decode("utf-8", errors="replace")
                        if path_filter and path_filter not in path.lower():
                            continue
                        matches.append({
                            "path": path, "line": int(line_number),
                            "snippet": snippet.decode("utf-8", errors="replace")[:400],
                        })
                        if len(matches) > limit:
                            limited = True
                            break
                    del pending[:consumed]
                    if len(pending) + sum(map(len, fields)) > MAX_BLOB_BYTES:
                        limited = True
                    if limited:
                        break
            if not limited:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    limited = True
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        finally:
            process.stdout.close()
            process.stderr.close()

    if not limited and process.returncode not in (0, 1):
        detail = stderr_tail.decode("utf-8", errors="replace").strip()[:200]
        raise SourceProviderError(
            "git grep 失败: " + (detail or f"exit {process.returncode}"), status_code=502,
        )
    if not limited and (pending or fields):
        raise SourceProviderError("git grep 输出不完整", status_code=502)
    return matches[:limit], scanned, limited
