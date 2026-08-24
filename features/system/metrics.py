"""Small dependency-free Prometheus exposition for controller operations."""

from __future__ import annotations

import os
import re
import resource
import threading
from collections import defaultdict
from typing import Any

from foundation.automation_port import get_worker_status


_LOCK = threading.Lock()
_REQUESTS: dict[tuple[str, str, int], int] = defaultdict(int)
_DURATION: dict[tuple[str, str], tuple[int, float]] = defaultdict(lambda: (0, 0.0))


def _fallback_path(path: str) -> str:
    normalized = re.sub(r"/[0-9a-fA-F-]{16,}", "/:id", str(path or ""))
    normalized = re.sub(r"/\d+", "/:number", normalized)
    return normalized[:160] or "/unknown"


def observe_request(request: Any, status_code: int, duration_seconds: float) -> None:
    route = request.scope.get("route") if request is not None else None
    path = str(getattr(route, "path", "") or _fallback_path(request.url.path))
    method = str(request.method or "UNKNOWN").upper()
    with _LOCK:
        _REQUESTS[(method, path, int(status_code))] += 1
        count, total = _DURATION[(method, path)]
        _DURATION[(method, path)] = (count + 1, total + max(0.0, duration_seconds))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics() -> str:
    lines = [
        "# HELP gms_http_requests_total HTTP requests handled by the controller.",
        "# TYPE gms_http_requests_total counter",
    ]
    with _LOCK:
        requests = dict(_REQUESTS)
        durations = dict(_DURATION)
    for (method, path, status), count in sorted(requests.items()):
        labels = f'method="{_escape(method)}",path="{_escape(path)}",status="{status}"'
        lines.append(f"gms_http_requests_total{{{labels}}} {count}")
    lines.extend([
        "# HELP gms_http_request_duration_seconds_sum Total request handling time.",
        "# TYPE gms_http_request_duration_seconds_sum counter",
        "# HELP gms_http_request_duration_seconds_count Timed HTTP requests.",
        "# TYPE gms_http_request_duration_seconds_count counter",
    ])
    for (method, path), (count, total) in sorted(durations.items()):
        labels = f'method="{_escape(method)}",path="{_escape(path)}"'
        lines.append(f"gms_http_request_duration_seconds_sum{{{labels}}} {total:.6f}")
        lines.append(f"gms_http_request_duration_seconds_count{{{labels}}} {count}")

    from features.system.state import global_state

    with global_state.websocket_connections_lock:
        websocket_count = len(global_state.websocket_connections)
    with global_state.terminal_lock:
        terminal_count = len(global_state.terminal_ssh_sessions)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    lines.extend([
        "# HELP gms_websocket_connections Active authenticated WebSocket connections.",
        "# TYPE gms_websocket_connections gauge",
        f"gms_websocket_connections {websocket_count}",
        "# HELP gms_terminal_sessions Active privileged terminal sessions.",
        "# TYPE gms_terminal_sessions gauge",
        f"gms_terminal_sessions {terminal_count}",
        "# HELP gms_process_max_rss_bytes Maximum resident set size.",
        "# TYPE gms_process_max_rss_bytes gauge",
        f"gms_process_max_rss_bytes {int(usage.ru_maxrss) * 1024}",
    ])
    try:
        worker = get_worker_status()
        lines.extend([
            "# HELP gms_automation_worker_up Whether the automation scheduler is running.",
            "# TYPE gms_automation_worker_up gauge",
            f"gms_automation_worker_up {1 if worker.get('running') else 0}",
        ])
    except Exception:
        lines.append("gms_automation_worker_up 0")
    return "\n".join(lines) + "\n"


def metrics_token() -> str:
    return os.getenv("GMS_METRICS_TOKEN", "").strip()
