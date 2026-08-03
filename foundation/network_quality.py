"""Small dependency-free TCP quality probe for transport preflight checks."""

from __future__ import annotations

import socket
import statistics
import time
from typing import Any


def probe_tcp_quality(
    host: str,
    port: int,
    *,
    attempts: int = 4,
    timeout: float = 2.0,
) -> dict[str, Any]:
    attempts = min(10, max(1, int(attempts)))
    latencies: list[float] = []
    errors: list[str] = []
    for _index in range(attempts):
        started = time.monotonic()
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                latencies.append(round((time.monotonic() - started) * 1000, 2))
        except OSError as exc:
            errors.append(str(exc))
    failures = attempts - len(latencies)
    loss_percent = round(failures * 100 / attempts, 1)
    average = round(statistics.fmean(latencies), 2) if latencies else None
    jitter = (
        round(statistics.pstdev(latencies), 2) if len(latencies) > 1 else 0.0
    )
    maximum = round(max(latencies), 2) if latencies else None
    reachable = bool(latencies)
    if not reachable:
        rating = "blocked"
    elif loss_percent > 0 or (average or 0) > 80 or jitter > 20:
        rating = "poor"
    elif (average or 0) > 50 or jitter > 10:
        rating = "warning"
    else:
        rating = "good"
    return {
        "host": host,
        "port": int(port),
        "reachable": reachable,
        "attempts": attempts,
        "successful_attempts": len(latencies),
        "loss_percent": loss_percent,
        "average_rtt_ms": average,
        "maximum_rtt_ms": maximum,
        "jitter_ms": jitter,
        "rating": rating,
        "errors": errors[-2:],
    }
