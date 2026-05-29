import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# 隧道数据流读取缓冲区大小（16KB，平衡内存占用和响应性）
STREAM_CHUNK_SIZE = 16384


@dataclass
class LocalTunnelStream:
    target: str
    writer: asyncio.StreamWriter
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    created_at: float = field(default_factory=time.time)


@dataclass
class TunnelAgent:
    websocket: WebSocket
    client_id: str
    username: str
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    streams: Dict[str, LocalTunnelStream] = field(default_factory=dict)
    windows_sshd: dict = field(default_factory=dict)
    windows_usbipd: dict = field(default_factory=dict)
    windows_admin: dict = field(default_factory=dict)

    async def send(self, message: dict):
        async with self.send_lock:
            await self.websocket.send_text(json.dumps(message, separators=(",", ":")))


class ReverseTunnelManager:
    """WebSocket-backed reverse TCP tunnel for public Windows clients.

    The server exposes local loopback ports. A Windows agent connects outbound
    over WebSocket and relays those local connections to its own 127.0.0.1
    services.
    """

    def __init__(self, ssh_port: int = 22022, usbip_port: int = 3240):
        self.listen_host = "127.0.0.1"
        self.targets = {
            "ssh": {"listen_port": ssh_port, "remote_port": 22},
            "usbipd": {"listen_port": usbip_port, "remote_port": 3240},
        }
        self.agent: Optional[TunnelAgent] = None
        self.agent_lock = asyncio.Lock()
        self.servers: Dict[str, asyncio.AbstractServer] = {}
        self.pending_commands: Dict[str, asyncio.Future] = {}
        self.pending_commands_lock = asyncio.Lock()

    async def start(self):
        for target, config in self.targets.items():
            port = int(config["listen_port"])
            if target in self.servers:
                continue
            try:
                server = await asyncio.start_server(
                    lambda r, w, t=target: self._handle_local_client(t, r, w),
                    host=self.listen_host,
                    port=port,
                )
                self.servers[target] = server
                logger.info("[PublicClientTunnel] listening %s:%s for %s", self.listen_host, port, target)
            except OSError as exc:
                logger.error("[PublicClientTunnel] failed to listen on %s:%s for %s: %s",
                             self.listen_host, port, target, exc)

    async def stop(self):
        async with self.agent_lock:
            if self.agent:
                await self._close_agent_streams(self.agent)
                self.agent = None
        async with self.pending_commands_lock:
            for future in self.pending_commands.values():
                if not future.done():
                    future.set_exception(RuntimeError("public client tunnel stopped"))
            self.pending_commands.clear()
        for server in self.servers.values():
            server.close()
            await server.wait_closed()
        self.servers.clear()

    def is_connected(self) -> bool:
        return bool(self.agent)

    def get_status(self) -> dict:
        agent = self.agent
        return {
            "connected": bool(agent),
            "client_id": agent.client_id if agent else None,
            "username": agent.username if agent else None,
            "connected_at": agent.connected_at if agent else None,
            "last_seen": agent.last_seen if agent else None,
            "active_streams": len(agent.streams) if agent else 0,
            "windows_sshd": agent.windows_sshd if agent else {},
            "windows_usbipd": agent.windows_usbipd if agent else {},
            "windows_admin": agent.windows_admin if agent else {},
            "listeners": {
                target: {
                    "host": self.listen_host,
                    "port": config["listen_port"],
                    "remote_port": config["remote_port"],
                    "listening": target in self.servers,
                }
                for target, config in self.targets.items()
            },
        }

    def get_ssh_device_host(self, username: str) -> Optional[str]:
        if not self.agent:
            return None
        safe_username = username or self.agent.username or "unknown"
        return f"{safe_username}@{self.listen_host}:{self.targets['ssh']['listen_port']}"

    def get_usbip_attach_host(self) -> str:
        return self.listen_host

    async def run_windows_command(self, command: str, timeout: int = 30) -> dict:
        async with self.agent_lock:
            agent = self.agent
        if not agent:
            raise RuntimeError("public client agent is not connected")

        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        async with self.pending_commands_lock:
            self.pending_commands[request_id] = future

        try:
            await agent.send({
                "type": "run_command",
                "request_id": request_id,
                "command": command,
                "timeout": timeout,
            })
            result = await asyncio.wait_for(future, timeout=timeout + 5)
            return result
        finally:
            async with self.pending_commands_lock:
                self.pending_commands.pop(request_id, None)

    async def handle_agent(self, websocket: WebSocket, client_id: str, username: str):
        await websocket.accept()
        agent = TunnelAgent(websocket=websocket, client_id=client_id, username=username or "unknown")
        async with self.agent_lock:
            if self.agent:
                try:
                    await self.agent.send({"type": "shutdown", "reason": "replaced"})
                except Exception:
                    pass
                await self._close_agent_streams(self.agent)
            self.agent = agent

        logger.info("[PublicClientTunnel] agent connected: client_id=%s username=%s", client_id, username)
        await agent.send({"type": "hello", "status": self.get_status()})

        try:
            while True:
                raw = await websocket.receive_text()
                agent.last_seen = time.time()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("[PublicClientTunnel] invalid agent message")
                    continue
                await self._handle_agent_message(agent, message)
        except Exception as exc:
            logger.info("[PublicClientTunnel] agent disconnected: %s", exc)
        finally:
            async with self.agent_lock:
                if self.agent is agent:
                    await self._close_agent_streams(agent)
                    self.agent = None

    async def _handle_agent_message(self, agent: TunnelAgent, message: dict):
        msg_type = message.get("type")
        conn_id = message.get("conn_id")
        if msg_type == "pong":
            return
        if msg_type == "hello":
            agent.windows_sshd = message.get("windows_sshd") or {}
            agent.windows_usbipd = message.get("windows_usbipd") or {}
            agent.windows_admin = message.get("windows_admin") or {}
            return
        if msg_type == "command_result":
            request_id = message.get("request_id")
            async with self.pending_commands_lock:
                future = self.pending_commands.get(request_id)
            if future and not future.done():
                future.set_result({
                    "returncode": message.get("returncode", -1),
                    "stdout": message.get("stdout", ""),
                    "stderr": message.get("stderr", ""),
                })
            return
        if not conn_id:
            return
        stream = agent.streams.get(conn_id)
        if not stream:
            return

        if msg_type == "connected":
            stream.connected.set()
        elif msg_type == "connect_failed":
            logger.warning("[PublicClientTunnel] connect_failed %s: %s", conn_id, message.get("error"))
            stream.connected.set()
            await self._close_stream(agent, conn_id)
        elif msg_type == "data":
            data = base64.b64decode(message.get("data") or "")
            if data and not stream.closed:
                stream.writer.write(data)
                await stream.writer.drain()
        elif msg_type == "close":
            await self._close_stream(agent, conn_id)

    async def _handle_local_client(self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        async with self.agent_lock:
            agent = self.agent
        if not agent:
            writer.close()
            await writer.wait_closed()
            logger.warning("[PublicClientTunnel] no agent for local %s connection", target)
            return

        conn_id = uuid.uuid4().hex
        stream = LocalTunnelStream(target=target, writer=writer)
        agent.streams[conn_id] = stream

        try:
            await agent.send({
                "type": "connect",
                "conn_id": conn_id,
                "target": target,
                "remote_port": self.targets[target]["remote_port"],
            })
            await asyncio.wait_for(stream.connected.wait(), timeout=10)
            if stream.closed:
                return

            while True:
                data = await reader.read(STREAM_CHUNK_SIZE)
                if not data:
                    break
                await agent.send({
                    "type": "data",
                    "conn_id": conn_id,
                    "data": base64.b64encode(data).decode("ascii"),
                })
        except Exception as exc:
            logger.debug("[PublicClientTunnel] local stream %s closed: %s", conn_id, exc)
        finally:
            try:
                await agent.send({"type": "close", "conn_id": conn_id})
            except Exception:
                pass
            await self._close_stream(agent, conn_id)

    async def _close_stream(self, agent: TunnelAgent, conn_id: str):
        stream = agent.streams.pop(conn_id, None)
        if not stream or stream.closed:
            return
        stream.closed = True
        try:
            stream.writer.close()
            await stream.writer.wait_closed()
        except Exception:
            pass

    async def _close_agent_streams(self, agent: TunnelAgent):
        for conn_id in list(agent.streams.keys()):
            await self._close_stream(agent, conn_id)


public_client_tunnel = ReverseTunnelManager()
