#!/usr/bin/env python3
"""
GMS public client agent.

Run this on the public Windows PC that has Android devices attached. It creates
an outbound WebSocket connection to the GMS server and relays server-side local
ports to Windows localhost services:

  server 127.0.0.1:22022 -> Windows 127.0.0.1:22
  server 127.0.0.1:3240  -> Windows 127.0.0.1:3240
"""

import argparse
import asyncio
import base64
import ctypes
import json
import os
import site
import socket
import subprocess
import sys
import time
import traceback
from urllib.parse import quote, urlencode, urlparse, urlunparse

# 与 reverse_tunnel.py 保持一致的缓冲区大小
STREAM_CHUNK_SIZE = 16384

# WebSocket 心跳间隔（秒）
WEBSOCKET_PING_INTERVAL = 30


def ensure_websockets():
    try:
        import websockets  # noqa: F401
        return
    except ImportError:
        print("[agent] installing Python package: websockets", flush=True)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "websockets"])
        try:
            site.addsitedir(site.getusersitepackages())
        except Exception:
            pass
        import importlib
        importlib.invalidate_caches()


ensure_websockets()
import websockets  # noqa: E402


TARGET_PORTS = {
    "ssh": 22,
    "usbipd": 3240,
}


def decode_process_output(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def build_ws_url(server_url: str, client_id: str, username: str) -> str:
    parsed = urlparse(server_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    netloc = parsed.netloc
    query = urlencode({"client_id": client_id, "username": username})
    return urlunparse((scheme, netloc, "/api/public-client/tunnel", "", query, ""))


def probe_windows_sshd_status() -> dict:
    service_query = subprocess.run(
        ["sc", "query", "sshd"],
        capture_output=True,
        shell=False,
    )
    sc_output = decode_process_output(service_query.stdout) + "\n" + decode_process_output(service_query.stderr)
    running = "RUNNING" in sc_output
    installed = service_query.returncode == 0 or os.path.exists(r"C:\Windows\System32\OpenSSH\sshd.exe")
    return {
        "installed": installed,
        "running": running,
        "service_output": sc_output[-2000:],
    }


def probe_windows_usbipd_status() -> dict:
    try:
        version_query = subprocess.run(
            ["usbipd", "--version"],
            capture_output=True,
            shell=False,
        )
        output = decode_process_output(version_query.stdout).strip() or decode_process_output(version_query.stderr).strip()
        installed = version_query.returncode == 0 and bool(output)
        return {
            "installed": installed,
            "version": output,
        }
    except FileNotFoundError:
        return {
            "installed": False,
            "version": "",
        }
    except Exception as exc:
        return {
            "installed": False,
            "version": str(exc),
        }


def probe_windows_admin_status() -> dict:
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        return {"is_admin": is_admin}
    except Exception as exc:
        return {"is_admin": False, "error": str(exc)}


async def send_json(ws, message: dict):
    await ws.send(json.dumps(message, separators=(",", ":")))


def execute_windows_command(command: str, timeout: int = 30) -> dict:
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=timeout,
        )
        return {
            "returncode": completed.returncode,
            "stdout": decode_process_output(completed.stdout),
            "stderr": decode_process_output(completed.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": -1,
            "stdout": decode_process_output(exc.stdout),
            "stderr": f"command timed out after {timeout}s",
        }
    except Exception as exc:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }


async def relay_socket_to_ws(ws, conn_id: str, reader: asyncio.StreamReader):
    try:
        while True:
            data = await reader.read(STREAM_CHUNK_SIZE)
            if not data:
                break
            await send_json(ws, {
                "type": "data",
                "conn_id": conn_id,
                "data": base64.b64encode(data).decode("ascii"),
            })
    except Exception as exc:
        print(f"[agent] stream {conn_id} read closed: {exc}")
    finally:
        try:
            await send_json(ws, {"type": "close", "conn_id": conn_id})
        except Exception:
            pass


async def open_target_stream(ws, conn_id: str, target: str, streams: dict):
    port = TARGET_PORTS.get(target)
    if not port:
        await send_json(ws, {"type": "connect_failed", "conn_id": conn_id, "error": f"unknown target {target}"})
        return

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except Exception as exc:
        await send_json(ws, {"type": "connect_failed", "conn_id": conn_id, "error": str(exc)})
        return

    streams[conn_id] = writer
    await send_json(ws, {"type": "connected", "conn_id": conn_id})
    print(f"[agent] connected {target} stream {conn_id} -> 127.0.0.1:{port}")
    asyncio.create_task(relay_socket_to_ws(ws, conn_id, reader))


async def close_stream(conn_id: str, streams: dict):
    writer = streams.pop(conn_id, None)
    if not writer:
        return
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def run_agent(server_url: str, client_id: str, username: str):
    ws_url = build_ws_url(server_url, client_id, username)
    print(f"[agent] connecting: {ws_url}", flush=True)
    while True:
        streams = {}
        try:
            async with websockets.connect(ws_url, max_size=None, ping_interval=WEBSOCKET_PING_INTERVAL, ping_timeout=WEBSOCKET_PING_INTERVAL) as ws:
                print("[agent] connected to GMS server", flush=True)
                await send_json(ws, {
                    "type": "hello",
                    "client_id": client_id,
                    "username": username,
                    "targets": TARGET_PORTS,
                    "windows_sshd": probe_windows_sshd_status(),
                    "windows_usbipd": probe_windows_usbipd_status(),
                    "windows_admin": probe_windows_admin_status(),
                })

                async for raw in ws:
                    message = json.loads(raw)
                    msg_type = message.get("type")
                    conn_id = message.get("conn_id")

                    if msg_type == "hello":
                        print("[agent] server ready", flush=True)
                    elif msg_type == "connect" and conn_id:
                        asyncio.create_task(open_target_stream(ws, conn_id, message.get("target"), streams))
                    elif msg_type == "data" and conn_id:
                        writer = streams.get(conn_id)
                        if writer:
                            writer.write(base64.b64decode(message.get("data") or ""))
                            await writer.drain()
                    elif msg_type == "close" and conn_id:
                        await close_stream(conn_id, streams)
                    elif msg_type == "shutdown":
                        print(f"[agent] server shutdown: {message.get('reason')}", flush=True)
                        return
                    elif msg_type == "run_command" and message.get("request_id"):
                        request_id = message.get("request_id")
                        command = message.get("command") or ""
                        timeout = int(message.get("timeout") or 30)
                        result = execute_windows_command(command, timeout=timeout)
                        await send_json(ws, {
                            "type": "command_result",
                            "request_id": request_id,
                            **result,
                        })
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[agent] disconnected: {exc}; retrying in 3s", flush=True)
            await asyncio.sleep(3)
        finally:
            for conn_id in list(streams.keys()):
                await close_stream(conn_id, streams)


def default_client_id(username: str) -> str:
    hostname = socket.gethostname()
    return f"{username}@{hostname}"


def main():
    parser = argparse.ArgumentParser(description="GMS public Windows client agent")
    parser.add_argument("--server", required=True, help="GMS public URL, for example https://xxx.ngrok-free.dev")
    parser.add_argument("--username", default="", help="Windows SSH username")
    parser.add_argument("--client-id", default="", help="Stable client id shown in server status")
    args = parser.parse_args()

    username = args.username or input("Windows SSH username: ").strip()
    if not username:
        raise SystemExit("username is required")

    client_id = args.client_id or default_client_id(username)
    try:
        asyncio.run(run_agent(args.server.rstrip("/"), client_id, username))
    except KeyboardInterrupt:
        print("\n[agent] stopped", flush=True)
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
