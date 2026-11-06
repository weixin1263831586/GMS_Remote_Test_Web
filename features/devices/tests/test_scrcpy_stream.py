"""scrcpy 实时投屏会话与路由的单元测试（全桩，不碰真机）。"""

from __future__ import annotations

import hashlib
import re
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.devices import scrcpy_stream
from features.devices.aoa_api import router as aoa_router
from features.devices.scrcpy_stream import ScrcpySession, server_jar_path
from foundation.error_model import register_api_error_handler


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("features.devices.aoa_api._stream_access", lambda *a: "owner-1")
    monkeypatch.setattr("features.devices.aoa_api.acquire_device_operation_claim",
                        lambda *a: ("source", [], None))
    monkeypatch.setattr("features.devices.aoa_api.release_device_operation_claim", lambda *a: None)
    app = FastAPI()
    register_api_error_handler(app)
    app.include_router(aoa_router)
    return TestClient(app)


# ---- 会话生命周期（桩 adb / Popen / socket）----


class FakeSocket:
    """永不 EOF 的循环码流（真实 server 持续推流；EOF 才会停会话）。"""

    def __init__(self, data: bytes):
        self.pattern = data
        self.sent = []
        self.closed = False

    def settimeout(self, _t):
        pass

    def recv(self, size):
        if self.closed:
            return b""
        # 无限循环同一 pattern，模拟持续视频流。
        out = (self.pattern * (size // len(self.pattern) + 1))[:size]
        return out

    def close(self):
        self.closed = True


@pytest.fixture
def fake_adb_env(monkeypatch):
    """桩掉 adb/Popen/socket：状态推进按真实协议（socket 名/握手/帧头）。"""
    state = SimpleNamespace(popen_cmds=[], push_calls=0, failed_once=False,
                            socket=SimpleNamespace(closed=False))

    def fake_adb(serial, *args, timeout=15):
        joined = " ".join(args)
        if joined.startswith("shell md5sum"):
            jar = server_jar_path().read_bytes()
            return SimpleNamespace(returncode=0, stdout=f"{hashlib.md5(jar).hexdigest()}  {args[-1]}\n",
                                   stderr="")
        if joined.startswith("shell cat /proc/net/unix"):
            match = re.search(r"scid=([0-9a-f]{8})", str(state.popen_cmds))
            name = f"@scrcpy_{match.group(1)}" if match else ""
            return SimpleNamespace(returncode=0, stdout=f"ffff 00000000 {name}\n", stderr="")
        if joined.startswith("forward tcp:0"):
            return SimpleNamespace(returncode=0, stdout="43321\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_adb_bytes(serial, *args, timeout=20):
        if args[:1] == ("push",):
            state.push_calls += 1
        return SimpleNamespace(returncode=0, stdout="", stderr=b"")

    class FakeProc:
        def __init__(self, cmd, *args, **kwargs):
            self.cmd = cmd
            state.popen_cmds.append(cmd)

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=3):
            return 0

        def kill(self):
            pass

    # 纯帧流 pattern（无限循环）：内容不参与解析，仅验证中继。
    fake_socket = FakeSocket(b"\x00" * 13 + b"\x01")
    state.socket = fake_socket

    monkeypatch.setattr(scrcpy_stream, "_adb", fake_adb)
    monkeypatch.setattr(scrcpy_stream, "_adb_bytes", fake_adb_bytes)
    monkeypatch.setattr(scrcpy_stream.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(scrcpy_stream.socket, "create_connection",
                        lambda addr, timeout=3: fake_socket)
    return state


def teardown_function(_):
    scrcpy_stream.stop_all()


def test_server_jar_extracts_from_tools_tarball_into_data_cache():
    jar = server_jar_path()
    assert jar.is_file() and jar.stat().st_size > 0
    assert jar.name == "scrcpy-server"
    # tools/ 只保留分发 tarball；解包产物落在 data 缓存，不回写 tools/。
    assert "tools" not in jar.parts


def test_start_stream_lifecycle_and_protocol(fake_adb_env):
    state = fake_adb_env
    info = scrcpy_stream.start_stream("ser-1")
    # 元数据头已显式关闭：hello 只声明编码器（宽高由客户端解码帧决定）。
    assert info["codec"] == "h264"
    assert "width" not in info and "height" not in info
    # jar 已 push；server 命令以单字符串传递 CLASSPATH 且 control=false。
    assert state.push_calls == 1
    cmd = state.popen_cmds[-1][-1]  # Popen argv 末位即 shell 命令串
    assert "CLASSPATH=/data/local/tmp/gms_scrcpy_server.jar" in cmd
    assert "control=false" in cmd and "tunnel_forward=true" in cmd
    assert "send_dummy_byte=false" in cmd and "send_codec_meta=false" in cmd
    scid_match = re.search(r"scid=([0-9a-f]{8})", cmd)
    scid = int(scid_match.group(1), 16)
    # scid 必须落在有符号 int32 正数区间（parseInt radix 16 不接受溢出）。
    assert 1 <= scid <= 0x7FFFFFFF
    session = scrcpy_stream.get_stream("ser-1")
    assert session is not None
    assert session.forward_port == 43321
    assert scrcpy_stream.stop_stream("ser-1") == {"stopped": True}
    assert scrcpy_stream.get_stream("ser-1") is None


def test_start_stream_retries_push_failure(fake_adb_env, monkeypatch):
    state = fake_adb_env
    real = scrcpy_stream._adb_bytes

    def flaky(serial, *args, timeout=20):
        if args[:1] == ("push",) and not state.failed_once:
            # 模拟设备 /data/local/tmp 被清理：首次 push 失败，重试成功。
            state.failed_once = True
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"push failed")
        return real(serial, *args, timeout=timeout)

    monkeypatch.setattr(scrcpy_stream, "_adb_bytes", flaky)
    info = scrcpy_stream.start_stream("ser-1")
    assert info["codec"] == "h264"
    assert state.failed_once and state.push_calls == 1


def test_start_stream_reports_upstream_failure(monkeypatch, fake_adb_env):
    from foundation.error_model import ApiError

    def dead_adb(serial, *args, timeout=15):
        return SimpleNamespace(returncode=1, stdout="", stderr="no device")

    monkeypatch.setattr(scrcpy_stream, "_adb", dead_adb)
    with pytest.raises(ApiError):
        scrcpy_stream.start_stream("ser-1")


def test_idle_unsubscribe_marks_session(fake_adb_env):
    scrcpy_stream.start_stream("ser-1")
    session = scrcpy_stream.get_stream("ser-1")
    assert session.idle_since == 0.0  # 启动即视为被使用
    import asyncio

    class _Ws:
        pass

    async def scenario():
        ws = _Ws()
        sub = session.subscribe(ws)
        session.unsubscribe(sub)
        assert session.idle_since > 0
        session.subscribe(ws)
        assert session.idle_since == 0.0
        session.stop()

    asyncio.run(scenario())


def test_late_subscriber_receives_replay_keyframe(fake_adb_env):
    """晚订阅者必须收到 [关键帧+其后帧+尾巴] 的追帧数据（无参考断裂）。"""
    import asyncio

    scrcpy_stream.start_stream("ser-1")
    session = scrcpy_stream.get_stream("ser-1")

    class _Ws:
        pass

    async def scenario():
        time.sleep(0.3)  # 等中继消费 FakeSocket 内容（无 SPS/IDR → recent 空）
        keyframe = (b"\x00" * 8 + (20).to_bytes(4, "big")
                    + b"\x00\x00\x00\x01\x67\x42" + b"x" * 16)
        delta = b"\x00" * 8 + (5).to_bytes(4, "big") + b"\x00\x00\x00\x01\x41zz"
        # 模拟中继已缓冲的关键帧 + 其后 delta + 半帧尾巴
        session._recent = [keyframe, delta]
        session._stream_tail = b"\x00\x00"
        sub = session.subscribe(_Ws())
        catchup = await asyncio.wait_for(sub.queue.get(), timeout=2)
        # 追帧数据 = 关键帧 + delta + 尾巴，字节连续
        assert catchup == keyframe + delta + b"\x00\x00"
        session.unsubscribe(sub)
        session.stop()

    asyncio.run(scenario())


def test_relay_survives_recv_timeout_while_server_alive(fake_adb_env):
    """静态画面不出帧：recv 超时不判死（仅当 scrcpy server 进程退出才
    结束会话）；EOF 仍是真死并关闭订阅者 WS。"""
    import asyncio

    scrcpy_stream.start_stream("ser-1")
    session = scrcpy_stream.get_stream("ser-1")

    class _Ws:
        pass

    # 记录原始 FakeSocket，之后切换为"总是超时"的桩。
    original = session.video
    server_alive = {"poll": None}

    class _TimeoutSock:
        def settimeout(self, _t):
            pass

        def recv(self, _size):
            raise TimeoutError()

        def close(self):
            pass

    session.proc = SimpleNamespace(
        poll=lambda *a, **k: server_alive["poll"],
        terminate=lambda *a, **k: None, kill=lambda *a, **k: None,
        wait=lambda *a, **k: 0)
    session.video = _TimeoutSock()

    async def scenario():
        sub = session.subscribe(_Ws())
        await asyncio.sleep(1.2)  # 覆盖多个 6s？否——真实 timeout 由桩立即抛出
        # 桩每次 recv 立即抛 socket.timeout：进程活着 → 中继必须仍在运行。
        assert not session.stopped.is_set(), "recv 超时不应终止会话"
        assert session.relay_thread.is_alive()
        # server 退出（poll 返回码）→ 下一次超时检查判死。
        server_alive["poll"] = 1
        await asyncio.sleep(0.3)
        assert session.stopped.is_set(), "server 退出后应结束会话"
        session.unsubscribe(sub)

    asyncio.run(scenario())
    session.stop()
    original.close()


def test_frame_carries_sps_or_idr_detection():
    carry = b"\x00\x00\x00\x01\x67\x42\xc0\x20"
    plain = b"\x00\x00\x00\x01\x41\x9a"
    assert ScrcpySession._frame_carries_sps_or_idr(carry)
    assert not ScrcpySession._frame_carries_sps_or_idr(plain)


# ---- REST 路由 ----


def _auth(_request=None):
    from features.auth import CurrentUser

    user = CurrentUser(id="owner-1", username="tester", role="user")

    class FakeRequest:
        state = type("S", (), {"current_user": user})()

    return FakeRequest()


def test_stream_start_stop_routes(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "features.devices.aoa_api.require_human_principal", _auth)
    monkeypatch.setattr(
        "features.devices.aoa_api.get_local_worker_id", lambda: "local-worker")
    def fake_start(serial, owner_id):
        assert owner_id == "owner-1"
        captured["start"] = serial
        return {"width": 800, "height": 1280, "codec": "avc1"}

    def fake_stop(serial):
        captured["stop"] = serial
        return {"stopped": True}

    monkeypatch.setattr(scrcpy_stream, "start_stream", fake_start)
    monkeypatch.setattr(scrcpy_stream, "stop_stream", fake_stop)

    body = {"worker_id": "local-worker"}
    response = client.post("/api/devices/aoa/record/stream/ser-1/start", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["codec"] == "avc1"
    response = client.post("/api/devices/aoa/record/stream/ser-1/stop", json=body)
    assert response.status_code == 200
    assert captured == {"start": "ser-1", "stop": "ser-1"}


def test_stream_start_rejects_remote_worker(client, monkeypatch):
    monkeypatch.setattr("features.devices.aoa_api.require_human_principal", _auth)
    monkeypatch.setattr(
        "features.devices.aoa_api.get_local_worker_id", lambda: "local-worker")
    response = client.post("/api/devices/aoa/record/stream/ser-1/start",
                           json={"worker_id": "ats-worker-remote"})
    assert response.status_code == 422


def test_stream_start_rejects_bad_serial(client, monkeypatch):
    monkeypatch.setattr("features.devices.aoa_api.require_human_principal", _auth)
    monkeypatch.setattr(
        "features.devices.aoa_api.get_local_worker_id", lambda: "local-worker")
    for bad in ("", "../etc", "a/b"):
        response = client.post(f"/api/devices/aoa/record/stream/{bad}/start",
                               json={"worker_id": "local-worker"})
        assert response.status_code in (404, 422), (bad, response.text)


# ---- WebSocket 通道 ----


class FakeSession:
    def __init__(self):
        self.subscribed = False
        self.unsubscribed = False

    def hello(self):
        return {"codec": "avc1", "frames": 0}

    def subscribe(self, ws):
        import asyncio

        self.subscribed = True
        return scrcpy_stream._Subscriber(ws, asyncio.get_running_loop())

    async def pump(self, sub):
        # 与生产 pump 保持同一发送语义：PONG 哨兵 → JSON 文本帧。
        while sub.alive:
            chunk = await sub.queue.get()
            if chunk is None:
                break
            if chunk == scrcpy_stream._Subscriber.PONG:
                await sub.ws.send_json({"type": "pong"})
                continue

    def unsubscribe(self, sub):
        self.unsubscribed = True


def test_stream_ws_hello_and_pong(client, monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(
        "features.devices.aoa_api.validate_websocket_request",
        lambda websocket: (None, None))
    monkeypatch.setattr(scrcpy_stream, "get_stream", lambda serial: fake)
    with client.websocket_connect("/api/devices/aoa/record/stream/ser-1") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["codec"] == "avc1"
        ws.send_text("ping")
        assert ws.receive_json()["type"] == "pong"
    assert fake.subscribed and fake.unsubscribed


def test_stream_ws_closes_without_session(client, monkeypatch):
    monkeypatch.setattr(
        "features.devices.aoa_api.validate_websocket_request",
        lambda websocket: (None, None))
    monkeypatch.setattr(scrcpy_stream, "get_stream", lambda serial: None)
    with client.websocket_connect("/api/devices/aoa/record/stream/ser-1") as ws:
        error = ws.receive_json()
        assert error["type"] == "error"


@pytest.mark.parametrize("session_owner,claim_owner,source_type", [
    ("other", "owner-1", "cluster-reservation"),
    ("owner-1", "other", "cluster-reservation"),
    ("owner-1", "owner-1", "cluster-job"),
])
def test_stream_access_rejects_foreign_session_or_exclusive_claim(
        monkeypatch, session_owner, claim_owner, source_type):
    from features.devices import aoa_api
    from foundation.error_model import ApiError

    monkeypatch.setattr(aoa_api, "require_human_principal",
                        lambda _: SimpleNamespace(resource_owner_id="owner-1"))
    monkeypatch.setattr(aoa_api, "get_local_worker_id", lambda: "local")
    monkeypatch.setattr(scrcpy_stream, "get_stream",
                        lambda _: SimpleNamespace(owner_id=session_owner))
    claims = SimpleNamespace(list_active=lambda **k: [{
        "device_key": "local:serial", "owner_id": claim_owner, "source_type": source_type}])
    monkeypatch.setattr(aoa_api.cluster_port, "get_cluster_service",
                        lambda: SimpleNamespace(repository=SimpleNamespace(claims=claims)))
    with pytest.raises(ApiError):
        aoa_api._stream_access(None, "serial")


def test_stream_reuse_rejects_another_owner(monkeypatch):
    from foundation.error_model import ApiError

    monkeypatch.setattr(scrcpy_stream, "_ensure_reaper", lambda: None)
    monkeypatch.setattr(scrcpy_stream, "_sessions", {"serial": SimpleNamespace(owner_id="alice", stop=lambda: None)})
    with pytest.raises(ApiError):
        scrcpy_stream.start_stream("serial", owner_id="bob")


def test_session_death_closes_subscriber_ws(fake_adb_env):
    """会话死亡（USB 重枚举/adb 断开）必须主动关闭订阅者 WS。

    否则 pump 永远等空队列、前端画面冻结且无提示（实机 66df 复现：
    点触摸标定后投屏不同步，刷新才恢复）。
    """
    import asyncio

    scrcpy_stream.start_stream("ser-death")
    session = scrcpy_stream.get_stream("ser-death")

    class _Ws:
        def __init__(self):
            self.closed = None

        async def send_bytes(self, _data):
            pass

        async def close(self, code=None):
            self.closed = code

    closed = []

    async def scenario():
        ws = _Ws()
        sub = session.subscribe(ws)
        task = asyncio.get_running_loop().create_task(session.pump(sub))
        await asyncio.sleep(0.05)
        assert ws.closed is None            # 会话活着：WS 保持打开
        session.stop()                      # 会话死亡：stop → shutdown 哨兵
        await asyncio.wait_for(task, timeout=2)
        assert ws.closed is not None        # pump 关闭了 WS → 前端走回退
        closed.append(ws.closed)

    asyncio.run(scenario())
    assert closed and closed[0] == 4409


def test_session_death_with_full_queue_still_closes_ws(fake_adb_env):
    """哨兵被丢弃（队列满）时也必须关 WS：pump 只看到 alive=False。

    shutdown() 在队列满时投不进 None 哨兵，pump 退出循环拿不到
    session_ended 标记；finally 里按 stopped 兜底关闭，否则前端画面
    冻结且不回退（会话死亡 + 客户端消费慢同时发生时复现）。
    """
    import asyncio

    scrcpy_stream.start_stream("ser-full-queue")
    session = scrcpy_stream.get_stream("ser-full-queue")

    class _Ws:
        def __init__(self):
            self.closed = None

        async def send_bytes(self, _data):
            pass

        async def close(self, code=None):
            self.closed = code

    async def scenario():
        ws = _Ws()
        sub = session.subscribe(ws)
        for _ in range(sub.QUEUE_LIMIT - sub.queue.qsize()):  # 填满：哨兵将投递失败
            sub.enqueue(b"\x00")
        session.stop()                      # stop → shutdown 哨兵被满队列丢弃
        task = asyncio.get_running_loop().create_task(session.pump(sub))
        await asyncio.wait_for(task, timeout=2)
        assert ws.closed == 4409            # 无哨兵也兜底关闭 → 前端走回退

    asyncio.run(scenario())
