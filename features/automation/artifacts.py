"""Firmware artifact retrieval and lease-fenced transfer progress.

Jenkins requests remain on the configured origin; progress reports contain
only bounded transfer counters and cannot outlive the owning run lease.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

import requests

from features.automation.models import RUN_STATUS_ARTIFACT_FETCHING


if TYPE_CHECKING:
    from features.automation.repository import AutomationStore


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


def open_jenkins_artifact(
    url: str,
    *,
    configured_url: str,
    auth,
    verify: bool,
    timeout: tuple[int, int] = (20, 3600),
):
    """GET ``url`` without leaving the configured Jenkins origin.

    The returned response is still open. The caller closes it.
    """

    configured = urlparse(configured_url)
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        response = requests.get(
            current,
            auth=auth,
            verify=verify,
            stream=True,
            timeout=timeout,
            allow_redirects=False,
        )
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = str(response.headers.get('Location') or '')
        response.close()
        if not location:
            raise ValueError('Jenkins artifact redirect is missing a location')
        nxt = urljoin(current, location)
        parsed = urlparse(nxt)
        if (
            parsed.scheme != configured.scheme
            or parsed.netloc != configured.netloc
            or parsed.username
            or parsed.password
        ):
            raise ValueError('Jenkins redirected the artifact outside its configured origin')
        current = nxt
    raise ValueError('Jenkins artifact redirect limit exceeded')


def _normalize_progress(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        total = int(value["total_bytes"])
        downloaded = int(value["downloaded_bytes"])
        speed = float(value.get("bytes_per_second", 0))
        elapsed = float(value.get("elapsed_seconds", 0))
    except (KeyError, TypeError, ValueError, OverflowError):
        return {}
    if total <= 0 or not 0 <= downloaded <= total or not all(
        math.isfinite(number) and number >= 0 for number in (speed, elapsed)
    ):
        return {}
    return {
        "total_bytes": total, "downloaded_bytes": downloaded,
        "percent": round(downloaded * 100 / total, 1),
        "bytes_per_second": round(speed, 1), "elapsed_seconds": round(elapsed, 1),
    }


def artifact_download_progress(run: dict[str, Any]) -> dict[str, Any]:
    """Expose transfer counters only; never include artifact paths or credentials."""
    try:
        result = json.loads(run.get("result_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return _normalize_progress(result.get("artifact_download")) if isinstance(result, dict) else {}


def artifact_progress_callback(
    store: AutomationStore, run_id: str, lease_owner: str,
) -> Callable[[dict[str, Any]], None]:
    last_reported: float | None = None

    def report(value: dict[str, Any]) -> None:
        nonlocal last_reported
        progress = _normalize_progress(value)
        if not progress:
            return
        now = time.monotonic()
        complete = progress["downloaded_bytes"] == progress["total_bytes"]
        if last_reported is not None and now - last_reported < 2 and not complete:
            return
        current = store.get_run(run_id)
        if (not current or current["status"] != RUN_STATUS_ARTIFACT_FETCHING
                or current.get("lease_owner") != lease_owner):
            raise RuntimeError("Firmware download stopped: run cancelled or claim lost")
        try:
            result = json.loads(current.get("result_json") or "{}")
        except (TypeError, ValueError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        result["artifact_download"] = progress
        _, applied = store.update_run_if_status(
            run_id, RUN_STATUS_ARTIFACT_FETCHING, expected_lease_owner=lease_owner,
            result_json=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        )
        if not applied:
            raise RuntimeError("Firmware download stopped: run cancelled or claim lost")
        last_reported = now

    return report
