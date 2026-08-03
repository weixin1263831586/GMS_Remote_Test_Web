from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from foundation.transport_contract import execute_transport, transport_result


ADB_PROXY_VERSION = "0.4.5"
ADB_PROXY_PORT = 5038
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
_PAIR_CONTEXT = b"gms-adbproxy-rs-v0.4.5-grant"
_LOG_MONITORS: set[str] = set()
_LOG_MONITOR_LOCK = threading.Lock()


def pair_code_from_grant(secret: str | bytes, grant: str) -> str:
    """Derive an assignment-specific adbproxy-rs code from a signed grant."""
    value = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not value:
        raise ValueError("ADB Proxy secret is empty")
    normalized_grant = str(grant or "").strip()
    if not normalized_grant:
        raise ValueError("ADB Proxy access grant is empty")
    digest = hmac.new(
        value,
        _PAIR_CONTEXT + b"\0" + normalized_grant.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b32encode(digest).decode("ascii")[:8]


def _state_root(*, create: bool = False) -> Path:
    configured = os.getenv("GMS_ADB_PROXY_STATE_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else (
        Path.home() / ".local/state/gms-adbproxy"
    )
    if create:
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
    return root


def _binary(name: str) -> str:
    configured = os.getenv("GMS_ADB_PROXY_BIN_DIR", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser() / name)
    candidates.append(Path.home() / ".local/bin" / name)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise RuntimeError(
        f"{name} 未安装；请重新部署 Worker 或执行 scripts/install_adbproxy_rs.sh"
    )


def capability_status() -> dict[str, Any]:
    try:
        proxy = _binary("adb-proxy")
        hub = _binary("adb-hub")
        completed = subprocess.run(
            [proxy, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        version = (completed.stdout or completed.stderr).strip()
        return {
            "installed": completed.returncode == 0,
            "version": version,
            "proxy_path": proxy,
            "hub_path": hub,
        }
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        return {"installed": False, "version": ""}


def imported_devices() -> dict[str, dict[str, str]]:
    """Return adb-hub imports keyed by their source device serial."""
    root = _state_root()
    if not _managed_running(root / "hub.pid", "adb-hub"):
        return {}
    state = _read_json(root / "target.json")
    return {
        str(serial): {
            "source_worker_id": str(item.get("source_worker_id") or ""),
            "source_address": str(item.get("source_address") or ""),
            "source_serial": str(serial),
        }
        for item in state.get("imports") or []
        for serial in item.get("devices") or []
        if str(serial)
    }


def imported_serials() -> set[str]:
    """Backward-compatible serial-only view of current adb-hub imports."""
    return set(imported_devices())


def imported_device_for_serial(serial: str) -> dict[str, str] | None:
    """Resolve either the raw or adb-hub-prefixed serial to its source."""
    value = str(serial or "")
    for source_serial, metadata in imported_devices().items():
        if value == source_serial or value.endswith(f":{source_serial}"):
            return metadata
    return None


def sync_source_policy() -> None:
    """Refresh the explicit source allowlist used by the default-deny proxy."""
    root = _state_root()
    state = _read_json(root / "source.json")
    if not state.get("running") or not _managed_running(
        root / "proxy.pid", "adb-proxy"
    ):
        return
    selected = set(_validated_serials(state.get("devices") or []))
    _write_source_policy(root / "proxy.toml", sorted(selected))


def recover_managed_state(*, secret: str | bytes = "") -> dict[str, Any]:
    """Restore persisted adb-proxy/adb-hub processes after a host restart."""
    root = _state_root()
    recovered: list[str] = []
    errors: list[str] = []
    source = _read_json(root / "source.json")
    target = _read_json(root / "target.json")

    if source.get("running") and not _managed_running(
        root / "proxy.pid", "adb-proxy"
    ):
        try:
            access_token = str(source.get("access_token") or "")
            _source_start(
                {
                    "devices": source.get("devices") or [],
                    "listen_address": source.get("listen_address") or "",
                    "allowed_peer_address": (
                        source.get("allowed_peer_address") or ""
                    ),
                    "access_token": access_token,
                    "generation": int(source.get("generation") or 0),
                },
                pair_code_from_grant(secret, access_token),
            )
            recovered.append("source")
        except Exception as exc:
            # USB/ADB may still be coming back. The heartbeat retries without
            # deleting the persisted selection.
            errors.append(f"source: {exc}")

    imports = target.get("imports") or []
    config_path = root / "hub.toml"
    if imports and not _managed_running(root / "hub.pid", "adb-hub"):
        try:
            config = _read_hub_config(config_path)
            if not config.get("backend"):
                raise RuntimeError("adb-hub持久化配置无效")
            _restart_hub(config_path)
            recovered.append("target")
        except Exception as exc:
            errors.append(f"target: {exc}")

    return {"recovered": recovered, "errors": errors}


def execute_adb_proxy_action(
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    pair_code: str = "",
) -> dict[str, Any]:
    payload = payload or {}
    external_payload = dict(payload)
    if pair_code:
        external_payload["pair_code"] = pair_code
    return execute_transport(
        "GMS_ADB_PROXY_CONTROL_BIN",
        transport="adb_proxy",
        action=action,
        payload=external_payload,
        timeout=120 if action == "target_connect" else 30,
        builtin=lambda: _execute_adb_proxy_builtin(
            action,
            payload,
            pair_code=pair_code,
        ),
    )


def _execute_adb_proxy_builtin(
    action: str,
    payload: dict[str, Any],
    *,
    pair_code: str,
) -> dict[str, Any]:
    generation = int(payload.get("generation") or 0)
    if action == "status":
        return _status()
    if action == "logs":
        return {
            "proxy": _tail_log("proxy", 100),
            "hub": _tail_log("hub", 100),
        }
    if action == "source_devices":
        return {"devices": _adb_devices()}
    if action == "source_start":
        result = _source_start(payload, pair_code)
        return transport_result(
            "adb_proxy", result, transport_state="connected",
            protocol_state="adb", readiness="test_ready", generation=generation,
        )
    if action == "source_stop":
        return transport_result(
            "adb_proxy", _source_stop(payload), transport_state="disconnected",
            readiness="not_ready", generation=generation,
        )
    if action == "target_connect":
        result = _target_connect(payload, pair_code)
        return transport_result(
            "adb_proxy", result, transport_state="connected",
            protocol_state="adb", readiness="test_ready", generation=generation,
        )
    if action == "target_disconnect":
        result = _target_disconnect(payload)
        return transport_result(
            "adb_proxy", result, transport_state="disconnected",
            readiness="not_ready", generation=generation,
        )
    raise ValueError(f"unsupported adb-proxy action: {action}")


def _adb_devices() -> list[dict[str, str]]:
    completed = subprocess.run(
        ["adb", "devices", "-l"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "adb devices failed").strip())
    devices: list[dict[str, str]] = []
    for line in completed.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2:
            continue
        serial, state = fields[:2]
        properties = {
            key: value
            for field in fields[2:]
            if ":" in field
            for key, value in [field.split(":", 1)]
        }
        devices.append({
            "serial": serial,
            "state": state,
            "model": properties.get("model", ""),
            "product": properties.get("product", ""),
        })
    return devices


def _source_start(payload: dict[str, Any], pair_code: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Z0-9]{8}", pair_code or ""):
        raise ValueError("invalid adb-proxy pair code")
    root = _state_root()
    current = _read_json(root / "source.json")
    requested_generation = int(payload.get("generation") or 0)
    if int(current.get("generation") or 0) > requested_generation:
        raise RuntimeError("stale ADB Proxy source generation")
    if (_read_json(root / "target.json").get("imports") or []):
        raise RuntimeError("同一主机不能同时作为 ADB Proxy 设备来源和接入主机")
    requested = _validated_serials(payload.get("devices") or [])
    live = _adb_devices()
    available = {
        item["serial"] for item in live if item.get("state") == "device"
    }
    missing = [serial for serial in requested if serial not in available]
    if missing:
        raise RuntimeError("设备当前不可执行ADB操作: " + ", ".join(missing))

    root = _state_root(create=True)
    proxy_bin = _binary("adb-proxy")
    requested_listen = str(payload.get("listen_address") or "").strip()
    allowed_peer_address = str(
        payload.get("allowed_peer_address") or ""
    ).strip()
    if not allowed_peer_address:
        raise ValueError("ADB Proxy target address is empty")
    allowed_peers = sorted(_private_addresses(allowed_peer_address))
    listen_host = (
        _private_bind_address(requested_listen)
        if requested_listen
        else "0.0.0.0"
    )
    policy_path = root / "proxy.toml"
    _write_source_policy(policy_path, requested)
    _stop_managed(root / "proxy.pid", "adb-proxy")

    env = dict(os.environ)
    env["ADB_PROXY_PAIR_CODE"] = pair_code
    proxy_args = [
        proxy_bin,
        "--listen", _socket_address(listen_host, ADB_PROXY_PORT),
        "--target", "127.0.0.1:5037",
        "--config", str(policy_path),
        "--log-level", "warn",
    ]
    for allowed_peer in allowed_peers:
        proxy_args.extend(["--allow-peer", allowed_peer])
    process = subprocess.Popen(
        proxy_args,
        stdin=subprocess.DEVNULL,
        stdout=_process_log("proxy"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
    _write_pid(root / "proxy.pid", process.pid)
    ready = _wait_tcp(
        "127.0.0.1" if listen_host == "0.0.0.0" else listen_host,
        ADB_PROXY_PORT,
    )
    if not ready or process.poll() is not None:
        _stop_managed(root / "proxy.pid", "adb-proxy")
        raise RuntimeError("adb-proxy 未能监听5038端口")
    state = {
        "running": True,
        "devices": requested,
        "listen_address": requested_listen,
        "allowed_peer_address": allowed_peer_address,
        "access_token": str(payload.get("access_token") or ""),
        "port": ADB_PROXY_PORT,
        "pid": process.pid,
        "generation": int(payload.get("generation") or 0),
        "updated_at": time.time(),
    }
    _write_json(root / "source.json", state)
    return {
        **{key: value for key, value in state.items() if key != "access_token"},
        "version": ADB_PROXY_VERSION,
    }


def _source_stop(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _state_root()
    current = _read_json(root / "source.json")
    requested_generation = int((payload or {}).get("generation") or 0)
    if requested_generation and int(current.get("generation") or 0) > requested_generation:
        raise RuntimeError("stale ADB Proxy source generation")
    stopped = _stop_managed(root / "proxy.pid", "adb-proxy")
    (root / "source.json").unlink(missing_ok=True)
    (root / "proxy.toml").unlink(missing_ok=True)
    return {"stopped": stopped, "running": False}


def _target_connect(payload: dict[str, Any], pair_code: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Z0-9]{8}", pair_code or ""):
        raise ValueError("invalid adb-proxy pair code")
    if _read_json(_state_root() / "source.json").get("running"):
        raise RuntimeError("同一主机不能同时作为 ADB Proxy 设备来源和接入主机")
    source_worker_id = _validated_name(payload.get("source_worker_id"), "source_worker_id")
    backend_name = _backend_name(source_worker_id)
    source_address = str(payload.get("source_address") or "").strip()
    backend_host = _private_bind_address(source_address)
    devices = _validated_serials(payload.get("devices") or [])

    root = _state_root(create=True)
    config_path = root / "hub.toml"
    config = _read_hub_config(config_path)
    backends = [
        item for item in config.get("backend") or []
        if str(item.get("name") or "") != backend_name
    ]
    backends.append({
        "name": backend_name,
        "addr": _socket_address(backend_host, ADB_PROXY_PORT),
        "pair_code": pair_code,
        "enabled": True,
    })
    _write_hub_config(config_path, backends)

    state = _read_json(root / "target.json")
    requested_generation = int(payload.get("generation") or 0)
    current_import = next((
        item for item in state.get("imports") or []
        if item.get("source_worker_id") == source_worker_id
    ), None)
    if current_import and int(current_import.get("generation") or 0) > requested_generation:
        raise RuntimeError("stale ADB Proxy target generation")
    imports = [
        item for item in state.get("imports") or []
        if item.get("source_worker_id") != source_worker_id
    ]
    imports.append({
        "source_worker_id": source_worker_id,
        "source_address": source_address,
        "backend_name": backend_name,
        "devices": devices,
        "generation": int(payload.get("generation") or 0),
        "updated_at": time.time(),
    })
    _write_json(root / "target.json", {"imports": imports})
    try:
        _restart_hub(config_path)
        missing = list(devices)
        deadline = time.monotonic() + _hub_device_wait_seconds(
            len(backends)
        )
        while missing and time.monotonic() < deadline:
            visible = {
                item["serial"] for item in _adb_devices()
                if item.get("state") == "device"
            }
            missing = [
                serial for serial in devices
                if serial not in visible and f"{backend_name}:{serial}" not in visible
            ]
            if missing:
                time.sleep(0.25)
        if missing:
            raise RuntimeError(
                "ADB Hub未发现来源设备，请检查来源主机TCP/5038防火墙和设备状态: "
                + ", ".join(missing)
            )
    except Exception:
        try:
            _target_disconnect({"source_worker_id": source_worker_id})
        except Exception:
            pass
        raise
    return {
        "connected": True,
        "source_worker_id": source_worker_id,
        "source_address": source_address,
        "target_port": 5037,
        "devices": devices,
        "version": ADB_PROXY_VERSION,
    }


def _target_disconnect(payload: dict[str, Any]) -> dict[str, Any]:
    source_worker_id = _validated_name(
        payload.get("source_worker_id"), "source_worker_id"
    )
    backend_name = _backend_name(source_worker_id)
    root = _state_root()
    state = _read_json(root / "target.json")
    requested_generation = int(payload.get("generation") or 0)
    current_import = next((
        item for item in state.get("imports") or []
        if item.get("source_worker_id") == source_worker_id
    ), None)
    if (
        requested_generation
        and current_import
        and int(current_import.get("generation") or 0) > requested_generation
    ):
        raise RuntimeError("stale ADB Proxy target generation")
    imports = [
        item for item in state.get("imports") or []
        if item.get("source_worker_id") != source_worker_id
    ]
    config_path = root / "hub.toml"
    config = _read_hub_config(config_path)
    backends = [
        item for item in config.get("backend") or []
        if str(item.get("name") or "") != backend_name
    ]
    if imports:
        _write_json(root / "target.json", {"imports": imports})
        _write_hub_config(config_path, backends)
        _restart_hub(config_path)
    else:
        _stop_managed(root / "hub.pid", "adb-hub")
        (root / "target.json").unlink(missing_ok=True)
        config_path.unlink(missing_ok=True)
        _stop_side_adb()
        subprocess.run(
            ["adb", "start-server"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    return {
        "connected": bool(imports),
        "source_worker_id": source_worker_id,
        "remaining_imports": imports,
    }


def _status() -> dict[str, Any]:
    root = _state_root()
    source = _read_json(root / "source.json")
    target = _read_json(root / "target.json")
    capability = capability_status()
    proxy_running = _managed_running(root / "proxy.pid", "adb-proxy")
    hub_running = _managed_running(root / "hub.pid", "adb-hub")
    if proxy_running:
        _ensure_log_monitor(_log_path("proxy"))
    if hub_running:
        _ensure_log_monitor(_log_path("hub"))
    source_running = bool(source.get("running")) and proxy_running
    imports = target.get("imports") or []
    target_running = bool(imports) and hub_running
    protocol_devices: list[dict[str, str]] = []
    if source_running or target_running:
        try:
            protocol_devices = _adb_devices()
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            protocol_devices = []
    ready_serials = [
        item["serial"] for item in protocol_devices
        if item.get("state") == "device"
    ]
    return {
        "installed": capability.get("installed", False),
        "version": capability.get("version", ""),
        "source": {
            key: value for key, value in source.items()
            if key != "access_token"
        },
        "target": target,
        "proxy_running": proxy_running,
        "hub_running": hub_running,
        "transport_state": (
            "connected" if source_running or target_running
            else "degraded" if source.get("running") or imports
            else "disconnected"
        ),
        "protocol_state": (
            "adb" if ready_serials
            else "enumerating" if source_running or target_running
            else "unknown"
        ),
        "readiness": (
            "test_ready" if ready_serials
            else "transport_ready" if source_running or target_running
            else "not_ready"
        ),
        "protocol_devices": protocol_devices,
        "recent_errors": {
            "proxy": _recent_error("proxy"),
            "hub": _recent_error("hub"),
        },
    }


def _restart_hub(config_path: Path) -> None:
    root = _state_root()
    config = _read_hub_config(config_path)
    _stop_managed(root / "hub.pid", "adb-hub")
    # Kill any ADB server that may still own :5037 or :5039. A leftover
    # fork-server on either port prevents the new adb-hub (and its side
    # ADB on local_adb_port=5039) from binding cleanly, which surfaces as
    # "protocol fault (couldn't read status): Connection reset by peer".
    _force_kill_adb_port(5037)
    _force_kill_adb_port(5039)
    process = subprocess.Popen(
        [
            _binary("adb-hub"),
            "--config", str(config_path),
            "--daemon",
            "--single-user",
        ],
        stdin=subprocess.DEVNULL,
        stdout=_process_log("hub"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _write_pid(root / "hub.pid", process.pid)
    tcp_ready = _wait_tcp("127.0.0.1", 5037)
    adb_ready, adb_error = (
        _wait_adb_server(
            process,
            timeout=_hub_start_wait_seconds(
                len(config.get("backend") or [])
            ),
        )
        if tcp_ready
        else (False, "")
    )
    if not adb_ready or process.poll() is not None:
        _stop_managed(root / "hub.pid", "adb-hub")
        detail = f": {adb_error}" if adb_error else ""
        raise RuntimeError(f"adb-hub 未能在5037端口完成ADB协议初始化{detail}")


def _log_path(name: str) -> Path:
    root = _state_root(create=True) / "logs"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root / f"{name}.log"


def _process_log(name: str):
    """Open a private append-only process log and keep it copy-truncated."""
    path = _log_path(name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    _ensure_log_monitor(path)
    return os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)


def _ensure_log_monitor(path: Path) -> None:
    key = str(path)
    with _LOG_MONITOR_LOCK:
        if key in _LOG_MONITORS:
            return
        _LOG_MONITORS.add(key)

    def monitor() -> None:
        while True:
            try:
                _rotate_log_if_needed(path)
            except OSError:
                pass
            time.sleep(5)

    threading.Thread(
        target=monitor,
        name=f"ADBProxyLog-{path.stem}",
        daemon=True,
    ).start()


def _rotate_log_if_needed(path: Path) -> None:
    max_bytes = max(
        1024 * 1024,
        int(os.getenv("GMS_ADB_PROXY_LOG_MAX_BYTES", str(10 * 1024 * 1024))),
    )
    backups = min(10, max(1, int(os.getenv("GMS_ADB_PROXY_LOG_BACKUPS", "4"))))
    if not path.is_file() or path.stat().st_size <= max_bytes:
        return
    for index in range(backups, 1, -1):
        previous = path.with_name(f"{path.name}.{index - 1}")
        current = path.with_name(f"{path.name}.{index}")
        if previous.exists():
            os.replace(previous, current)
    first = path.with_name(f"{path.name}.1")
    shutil.copyfile(path, first)
    first.chmod(0o600)
    with path.open("r+", encoding="utf-8", errors="replace") as stream:
        stream.truncate(0)


def _tail_log(name: str, limit: int) -> list[str]:
    path = _log_path(name)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [_sanitize_log_line(line) for line in lines[-max(1, min(limit, 500)):]]


def _recent_error(name: str) -> str:
    markers = ("error", "failed", "panic", "refused", "reset", "timeout")
    for line in reversed(_tail_log(name, 100)):
        if any(marker in line.lower() for marker in markers):
            return line[-500:]
    return ""


def _sanitize_log_line(line: str) -> str:
    value = re.sub(
        r"(?i)(pair[_ -]?code|grant|access[_ -]?token|secret)(\s*[:=]\s*)\S+",
        r"\1\2[REDACTED]",
        str(line or ""),
    )
    return value[-2000:]


def _hub_start_wait_seconds(backend_count: int) -> float:
    """Allow for adb-hub's sequential compatibility and inventory probes."""
    return float(max(20, 15 + max(0, backend_count) * 15))


def _hub_device_wait_seconds(backend_count: int) -> float:
    """Wait for the initial poller to publish all configured backends."""
    return float(max(15, 10 + max(0, backend_count) * 10))


def _force_kill_adb_port(port: int) -> None:
    """Kill every process listening on the given loopback port.

    ``adb kill-server`` can fail when the server is in a half-broken state
    (the exact scenario that triggers the protocol fault). Use ``fuser`` to
    forcefully clear the port so adb-hub can bind cleanly.
    """
    for socket_addr in (f"127.0.0.1:{port}", f"127.0.0.1:{port}"):
        _kill_adb_server(socket_addr)
    # fuser fallback: OS-level kill of anything still on the port.
    subprocess.run(
        ["fuser", "-k", f"{port}/tcp"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    time.sleep(0.5)


def _read_hub_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        backends: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line == "[[backend]]":
                current = {}
                backends.append(current)
                continue
            if current is None or "=" not in line:
                continue
            key, value = (item.strip() for item in line.split("=", 1))
            if key in {"name", "addr", "pair_code"}:
                parsed = json.loads(value)
                if not isinstance(parsed, str):
                    return {}
                current[key] = parsed
            elif key == "enabled":
                current[key] = value.lower() == "true"
        valid = [
            item for item in backends
            if item.get("name") and item.get("addr") and item.get("pair_code")
        ]
        return {"backend": valid}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}


def _write_hub_config(path: Path, backends: list[dict[str, Any]]) -> None:
    lines = [
        'listen = "127.0.0.1:5037"',
        "poll_interval_ms = 1000",
        "include_local = true",
        "local_adb_port = 5039",
        "",
    ]
    for item in sorted(backends, key=lambda value: str(value.get("name") or "")):
        lines.extend([
            "[[backend]]",
            f'name = "{_toml_string(item["name"])}"',
            f'addr = "{_toml_string(item["addr"])}"',
            f'pair_code = "{_toml_string(item["pair_code"])}"',
            "enabled = true",
            "",
        ])
    _write_private(path, "\n".join(lines))


def _write_source_policy(path: Path, allowed_serials: list[str]) -> None:
    policy_text = "".join(
        f'[[device]]\nserial = "{_toml_string(serial)}"\nenabled = true\n\n'
        for serial in allowed_serials
    )
    _write_private(path, policy_text)


def _validated_serials(values: list[Any]) -> list[str]:
    result: list[str] = []
    for raw in values:
        serial = str(raw or "").strip()
        if not serial or len(serial) > 256 or any(ord(char) < 33 for char in serial):
            raise ValueError(f"invalid ADB serial: {serial!r}")
        if serial not in result:
            result.append(serial)
    if not result:
        raise ValueError("至少选择一个ADB设备")
    return result


def _validated_name(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_NAME.fullmatch(normalized):
        raise ValueError(f"invalid {field}")
    return normalized


def _backend_name(worker_id: str) -> str:
    return "gms-" + re.sub(r"[^A-Za-z0-9._-]", "-", worker_id)[:100]


def _private_addresses(value: str) -> set[str]:
    if not value or len(value) > 255:
        raise ValueError("invalid source address")
    try:
        resolved = {
            item[4][0] for item in socket.getaddrinfo(value, None)
        }
    except socket.gaierror as exc:
        raise ValueError(f"无法解析来源主机: {value}") from exc
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    for raw in resolved:
        address = ipaddress.ip_address(raw)
        if not (
            address.is_private
            or address.is_loopback
            or address in cgnat
        ):
            raise ValueError(f"ADB Proxy来源必须使用内网/VPN地址: {address}")
    return resolved


def _socket_address(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return f"{host}:{port}"
    return f"[{address}]:{port}" if address.version == 6 else f"{address}:{port}"


def _private_bind_address(value: str) -> str:
    resolved = _private_addresses(value)
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return sorted(
            resolved,
            key=lambda item: (
                ipaddress.ip_address(item).is_loopback,
                ipaddress.ip_address(item).version,
                item,
            ),
        )[0]


def _kill_adb_server(socket_addr: str = "127.0.0.1:5037") -> None:
    """Kill an ADB server on the given socket address.

    adb-hub spawns a side ADB server on local_adb_port (5039). Both the main
    (:5037) and side (:5039) servers must be stopped before (re)starting the
    hub, otherwise a stale fork-server keeps the port and the new instance
    fails its ADB protocol handshake.
    """
    env = dict(os.environ)
    env["ADB_SERVER_SOCKET"] = f"tcp:{socket_addr}"
    subprocess.run(
        ["adb", "kill-server"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=env,
    )


def _stop_side_adb() -> None:
    _kill_adb_server("127.0.0.1:5039")


def _toml_string(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _wait_tcp(host: str, port: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _wait_adb_server(process: Any, timeout: float = 20.0) -> tuple[bool, str]:
    """Wait until the managed Hub answers an ADB inventory request.

    A bound TCP socket is not sufficient: adb-hub reserves :5037 before its
    side ADB server and remote backends finish initialization. An immediate
    ``adb devices`` can therefore hit a short connection-refused/startup
    window. Retry that protocol request while the managed process is alive.
    """
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False, last_error or "adb-hub进程已退出"
        try:
            _adb_devices_safe()
            return True, ""
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            last_error = str(exc).strip()
            time.sleep(0.5)
    return False, last_error


def _adb_devices_safe() -> list[dict[str, str]]:
    """Like _adb_devices but never auto-starts an ADB server.

    During hub startup the default ``adb devices`` client may try to spawn
    its own :5037 server if the hub is not ready yet, which races with the
    hub for the port. Forcing start-server=0 avoids that.
    """
    env = dict(os.environ)
    env["ADB_SERVER_SOCKET"] = "tcp:127.0.0.1:5037"
    completed = subprocess.run(
        ["adb", "devices", "-l"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "adb devices failed").strip())
    devices: list[dict[str, str]] = []
    for line in completed.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2:
            continue
        serial, state = fields[:2]
        properties = {
            key: value
            for field in fields[2:]
            if ":" in field
            for key, value in [field.split(":", 1)]
        }
        devices.append({
            "serial": serial,
            "state": state,
            "model": properties.get("model", ""),
            "product": properties.get("product", ""),
        })
    return devices


def _managed_running(path: Path, expected: str) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        return expected.encode() in cmdline
    except (OSError, ValueError):
        return False


def _stop_managed(path: Path, expected: str) -> bool:
    if not _managed_running(path, expected):
        path.unlink(missing_ok=True)
        return False
    pid = int(path.read_text(encoding="utf-8").strip())
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return False
    for _ in range(30):
        if not _managed_running(path, expected):
            path.unlink(missing_ok=True)
            return True
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    path.unlink(missing_ok=True)
    return True


def _write_pid(path: Path, pid: int) -> None:
    _write_private(path, f"{pid}\n")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_private(
        path,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
    )


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
