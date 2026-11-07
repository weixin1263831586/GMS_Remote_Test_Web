"""scrcpy 实时投屏会话（AOA 录制器专用，纯显示流）。

录制机的投屏**只做显示器，不做输入器**：每次点按必须经
``/api/devices/aoa/record/input`` 执行并生成 AOA 命令（ADR 0022），
因此 scrcpy-server 以 ``control=false`` 启动，主机侧只消费 H.264 流。

协议（实机 8e4dbf3781825b31 + scrcpy-server 3.3.4 验证）：
- socket 名 ``scrcpy_%08x``（scid 为不带 0x 前缀的 hex，且必须落在
  有符号 int32 正数区间 < 0x80000000，``Integer.parseInt(..,16)`` 溢出
  会直接拒绝）；
- ``tunnel_forward=true``（server 监听抽象套接字）+ ``adb forward``（主机
  主动连）；CLASSPATH 必须以单字符串 shell 命令传递（参数列表形式会让
  env 赋值失效 → ``ClassNotFoundException`` abort）；
- ``send_dummy_byte=false send_codec_meta=false send_device_meta=false``：
  视频 socket 从首字节起就是纯帧流，每帧 ``[时间戳(8), 长度(4, BE)]``
  12 字节头 + 裸 H.264 Annex-B 负载；时间戳字段无稳定语义（config 帧
  为 AV_NOPTS 标记 0x8000…），客户端忽略并以单调时钟自造时间戳，帧
  类型从 NAL 自判（IDR(5)=关键帧，仅 SPS(7)/PPS(8)=config）。实机验证
  12 帧零残留闭合。
- 设备侧存在 /data/local/tmp 周期清理（测试框架卫生机制）：启动前必须
  push jar 并校验，失败即重推重试。

WS 侧做字节透明中继：握手字节（1+12）解析为 JSON hello 文本帧下发，
其后原始 ``[13B 帧头 + 负载]`` 字节流原样转发，客户端自行 demux 解码。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from foundation.error_model import ApiError


logger = logging.getLogger(__name__)


_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
_SERVER_VERSION = "3.3.4"
_DEVICE_JAR = "/data/local/tmp/gms_scrcpy_server.jar"
_SOCKET_CONNECT_TIMEOUT = 5.0
_START_ATTEMPTS = 3
_MAX_SESSIONS = 4

_lock = threading.Lock()
_sessions: dict[str, ScrcpySession] = {}
_reaper_started = False


class _Subscriber:
    """单个 WS 订阅者：线程安全的待发队列 + 事件循环内 pump 消费。"""

    QUEUE_LIMIT = 600  # 约 20s@30fps；超出视为客户端卡死，剔除
    # pong 哨兵：心跳应答必须走 pump 的唯一 sender——WS handler 若直接
    # send_json 会与 pump 的 send_bytes 并发（实机复现：建连约 7 秒后被
    # 静默关闭，与前端 5s 首个 ping 对齐；hello/pump 同类竞态已修，这是
    # 同一约束的 pong 侧补全）。
    PONG = b"\x00gms-pong"

    def __init__(self, ws, loop):
        self.ws = ws
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_LIMIT)
        self.alive = True

    def enqueue(self, chunk: bytes) -> None:
        if self.alive:
            if self.queue.full():
                # 队列满：正常帧可丢（保活优先），pong 哨兵绝不能丢——丢一次
                # pong 前端要等 15s 心跳超时。清一个位置给哨兵。
                if chunk == self.PONG:
                    try:
                        self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self.queue.put_nowait(chunk)
                else:
                    self.alive = False
                return
            self.queue.put_nowait(chunk)

    def request_pong(self) -> None:
        """WS handler 调用：把 pong 应答排入唯一 sender（pump）。"""
        self.enqueue(self.PONG)

    def offer(self, chunk: bytes) -> bool:
        """中继线程调用：经 call_soon_threadsafe 调度入队；False=已死剔除。"""

        def _put():
            self.enqueue(chunk)

        if not self.alive:
            return False
        try:
            self.loop.call_soon_threadsafe(_put)
        except RuntimeError:
            # 事件循环已关闭（页面断开/服务停止）：标记剔除。
            self.alive = False
            return False
        return True

    def shutdown(self) -> None:
        self.alive = False
        try:
            self.loop.call_soon_threadsafe(
                lambda: self.queue.put_nowait(None) if not self.queue.full() else None)
        except RuntimeError:
            pass

# 无订阅者持续超过该时长自动停流（设备端编码器/线程不常开）。
_IDLE_TIMEOUT_SECONDS = 180.0

# 新订阅者追帧缓冲上限：关键帧 + 其后帧的字节预算（超出丢弃后续 delta，
# 极端动态画面下最多轻微花屏，不至内存失控）。
_REPLAY_BUFFER_BYTES = 4 * 1024 * 1024


def _ensure_reaper() -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True

    def reap() -> None:
        while True:
            time.sleep(30)
            now = time.time()
            with _lock:
                stale = [
                    serial for serial, session in _sessions.items()
                    if session.idle_since > 0
                    and now - session.idle_since > _IDLE_TIMEOUT_SECONDS
                ]
                for serial in stale:
                    session = _sessions.pop(serial)
                    session.stop()

    threading.Thread(target=reap, name="scrcpy-stream-reaper", daemon=True).start()


def _adb(serial: str, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["adb", "-s", serial, *args], capture_output=True, text=True,
        timeout=timeout, check=False)


def _adb_bytes(serial: str, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["adb", "-s", serial, *args], capture_output=True, timeout=timeout, check=False)


def _server_cache_dir() -> Path:
    """scrcpy-server 解包缓存根：Controller data 目录（运行时产物）。"""
    try:
        from features.devices import deployment_service

        return deployment_service.store().root / "scrcpy-server"
    except Exception:
        # 单测/未装配 cluster port 时 data 根不可达：退回仓库 data/，
        # 保持 jar 可解析（tools/ 只保留 git 跟踪的分发 tarball）。
        return Path(__file__).resolve().parents[2] / "data" / "scrcpy-server"


def server_jar_path() -> Path:
    """解析 scrcpy-server jar：优先 data 根解包缓存，缺失时从 tools/ 分发 tar 提取。

    tools/ 目录只保留 git 跟踪的分发 tarball（架构测试约束目录清单），
    解包产物落在 Controller data 根；用 ``extractfile`` 手写文件而非
    ``tarfile.extract``（py3.10 无 filter 参数，避免成员路径遍历）。
    """
    cache_dir = _server_cache_dir()
    candidate = cache_dir / "scrcpy-server"
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    import tarfile

    tarballs = sorted(
        _TOOLS_DIR.glob("scrcpy-linux-x86_64-v*.tar.gz"),
        key=lambda item: item.stat().st_mtime, reverse=True)
    if not tarballs:
        raise ApiError.not_found(
            "scrcpy-server 分发缺失：tools/scrcpy-linux-x86_64-v*.tar.gz")
    with tarfile.open(tarballs[0]) as archive:
        member = next(
            item for item in archive.getmembers()
            if item.name.endswith("/scrcpy-server") and item.isfile())
        source = archive.extractfile(member)
        if source is None:
            raise ApiError.not_found(f"scrcpy-server tar 成员不可读：{member.name}")
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = cache_dir / ".scrcpy-server.tmp"
        with source, temporary.open("wb") as output:
            shutil.copyfileobj(source, output)
        temporary.replace(candidate)
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    raise ApiError.not_found("scrcpy-server 提取失败")


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class ScrcpySession:
    """单设备的 scrcpy 显示流会话：server 进程 + forward + 中继线程。"""

    # 编码器候选链（实机验证）：
    # - 8e4d/Android 16：默认软编码器可用；
    # - c3d9/Android 17 预览固件：全部路径在 C2AllocatorIon 空指针崩溃
    #   （libcodec2_vndk，软/硬、h264/h265 均崩）——候选全败后由调用方
    #   回退截图模式，并带出各候选的失败原因。
    ENCODER_CANDIDATES: tuple[dict[str, str], ...] = (
        {"codec": "h264"},
        {"codec": "h264", "video_encoder": "c2.rk.avc.encoder"},
        {"codec": "h265"},
        {"codec": "h265", "video_encoder": "c2.rk.hevc.encoder"},
    )
    FIRST_BYTE_TIMEOUT_SECONDS = 6.0

    def __init__(self, serial: str):
        self.serial = serial
        self.max_size = 1024
        self.max_fps = 30
        self.bit_rate = 4_000_000
        # 投屏期间屏幕常亮标志：stop/回收时恢复默认熄屏策略。
        self.stayon_active = False
        self.proc: subprocess.Popen | None = None
        self.video: socket.socket | None = None
        self.forward_port: int | None = None
        self.codec = "h264"
        self.video_encoder = ""
        self.subscribers: list[Any] = []      # 已 accept 的 WebSocket
        self.lock = threading.Lock()
        self.relay_thread: threading.Thread | None = None
        self.stopped = threading.Event()
        self.first_byte = threading.Event()
        self.started_at = 0.0
        self.frames = 0
        # 最近一帧含 SPS(7)/IDR(5) 的完整帧（12B 头+负载）：新订阅者接入
        # 时先重放该帧，否则会话启动瞬间的配置帧早已被无订阅者的中继吃掉
        # （RK3576 实测：编码器整段会话只发一次 SPS），客户端解码器永远
        # 无法初始化 → 画面空白。重放的是最近关键帧，画面可能略旧，属
        # 录制预览可接受；随后为实时帧。
        self.replay_frame: bytes | None = None
        # >0 表示当前无订阅者（自该时刻起空闲）；0 表示有订阅者。
        self.idle_since = 0.0

    # ---- 启动（含 jar 推送与重试，见模块 docstring 的协议要点）----

    def start(self) -> dict[str, Any]:
        jar = server_jar_path()
        jar_bytes = jar.read_bytes()
        _adb(self.serial, "shell", "input", "keyevent", "224")
        # 投屏期间屏幕常亮：录制全程需要观察画面，熄屏会导致编码器无帧
        # （前端提示"等待设备画面"）。``svc power stayon usb`` 只在 USB
        # 供电时保持亮屏，安全性优于全局 stayon true；会话结束（手动
        # 停止/空闲回收/新会话替换）时恢复默认熄屏策略。
        _adb(self.serial, "shell", "svc", "power", "stayon", "usb")
        self.stayon_active = True
        # jar 推送与编码器无关：候选链外层只推一次（设备 /data/local/tmp
        # 存在周期清理，失败重推）。
        for _push_attempt in range(_START_ATTEMPTS):
            push = _adb_bytes(self.serial, "push", str(jar), _DEVICE_JAR)
            verify = _adb(self.serial, "shell", "md5sum", _DEVICE_JAR)
            if push.returncode == 0 and _md5(jar_bytes)[:8] in verify.stdout:
                break
            time.sleep(0.6)
        else:
            raise ApiError.upstream_failure(
                f"scrcpy-server push failed after {_START_ATTEMPTS} attempts")
        errors: list[str] = []
        for candidate in self.ENCODER_CANDIDATES:
            self.codec = candidate["codec"]
            self.video_encoder = candidate.get("video_encoder", "")
            label = self.codec + (f"/{self.video_encoder}" if self.video_encoder
                                  else "/default")
            try:
                self._launch()
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                errors.append(f"{label}: {exc}")
                self._teardown_processes()
                continue
            # 存活检测：编码器崩溃（如 C2AllocatorIon SIGSEGV）发生在
            # socket 建立之后——必须等到首字节才认为候选可用。
            if self.first_byte.wait(self.FIRST_BYTE_TIMEOUT_SECONDS) \
                    and self.proc is not None and self.proc.poll() is None:
                return {"codec": self.codec}
            errors.append(f"{label}: no video data (encoder crashed or screen off)")
            self._teardown_processes()
        self._teardown_processes()
        raise ApiError.upstream_failure(
            f"scrcpy 视频流不可用（{len(errors)} 个编码器候选均失败）: "
            + "; ".join(errors))

    def _launch(self) -> None:
        scid = int(time.monotonic_ns() * 2654435761 % 0x7FFFFFFE) + 1
        socket_name = f"scrcpy_{scid:08x}"
        encoder_arg = (f"video_encoder={self.video_encoder} "
                       if self.video_encoder else "")
        server_cmd = (
            f"CLASSPATH={_DEVICE_JAR} app_process / "
            f"com.genymobile.scrcpy.Server {_SERVER_VERSION} "
            "log_level=info "
            f"scid={scid:08x} max_size={self.max_size} max_fps={self.max_fps} "
            f"video_bit_rate={self.bit_rate} video_codec={self.codec} "
            f"{encoder_arg}"
            "audio=false control=false send_frame_meta=true "
            "send_dummy_byte=false send_codec_meta=false "
            "send_device_meta=false tunnel_forward=true")
        self.proc = subprocess.Popen(
            ["adb", "-s", self.serial, "shell", server_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 轮询设备侧抽象套接字就绪（/proc/net/unix），避免过早 forward。
        deadline = time.monotonic() + _SOCKET_CONNECT_TIMEOUT
        ready = False
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("scrcpy server exited during start")
            listing = _adb(self.serial, "shell", "cat", "/proc/net/unix").stdout or ""
            if socket_name in listing:
                ready = True
                break
            time.sleep(0.3)
        if not ready:
            raise RuntimeError("scrcpy server socket not ready in time")
        fwd = _adb(self.serial, "forward", "tcp:0", f"localabstract:{socket_name}")
        if fwd.returncode != 0:
            raise RuntimeError(f"adb forward failed: {fwd.stderr.strip()}")
        self.forward_port = int(fwd.stdout.strip())
        self.video = socket.create_connection(("127.0.0.1", self.forward_port), timeout=3)
        self.video.settimeout(6)
        # send_dummy_byte/send_codec_meta/send_device_meta 已显式关闭：视频
        # socket 从首字节起就是 [时间戳(8) 长度(4)] 12 字节头 + 负载的纯帧
        # 流（实机 8e4d 验证 12 帧零残留；头部与设备名元数据在部分启动下
        # 顺序有歧义，全部关闭后无歧义）。宽高由客户端解码帧决定。
        self.stopped.clear()
        self.first_byte.clear()
        self.started_at = time.time()
        self.relay_thread = threading.Thread(
            target=self._relay_loop, name=f"scrcpy-relay-{self.serial}", daemon=True)
        self.relay_thread.start()

    # ---- 中继：纯帧流透明转发给全部 WS 订阅者 ----

    @staticmethod
    def _frame_carries_sps_or_idr(payload: bytes) -> bool:
        """负载的 NAL 类型含 SPS(7) 或 IDR(5)（Annex-B 起始码扫描）。"""
        cursor = 0
        while cursor < len(payload) - 4:
            if payload[cursor:cursor + 3] == b"\x00\x00\x01":
                nal_start = cursor + 3
            elif payload[cursor:cursor + 4] == b"\x00\x00\x00\x01":
                nal_start = cursor + 4
            else:
                cursor += 1
                continue
            nal_type = payload[nal_start] & 0x1F
            if nal_type in (5, 7):
                return True
            cursor = nal_start
        return False

    def _relay_loop(self) -> None:
        stream_buf = b""
        recent: list[bytes] = []       # 自最近关键帧起的完整帧（含关键帧）
        recent_bytes = 0
        try:
            while not self.stopped.is_set():
                try:
                    chunk = self.video.recv(262144)
                except TimeoutError:
                    # recv 超时 ≠ 会话死亡：静态画面下 scrcpy 本来就不出帧
                    # （实机复现：RK3576GMS1 静止桌面 → 首帧突发后 6 秒无帧
                    # → timeout 被 OSError 分支吞掉 → 会话被误杀，WS 4409，
                    # 前端每 ~7 秒断开重连一次）。超时只检查设备端 server
                    # 进程是否仍活着；活着就继续等帧。
                    if self.proc is not None and self.proc.poll() is not None:
                        break
                    continue
                if not chunk:
                    break
                self.first_byte.set()
                self.frames += 1
                # 被动 demux：维护"自最近关键帧起的完整帧"缓冲（帧边界以
                # 12B 头长度字段闭合）。新订阅者需要关键帧 + 其后全部帧
                # 才能无鬼影追帧：只重放关键帧会缺参考链（实机绿底鬼影）。
                stream_buf += chunk
                while len(stream_buf) >= 12:
                    length = int.from_bytes(stream_buf[8:12], "big")
                    if length > 512 * 1024:
                        stream_buf = b""
                        recent = []
                        recent_bytes = 0
                        break
                    if len(stream_buf) < 12 + length:
                        break
                    frame = stream_buf[:12 + length]
                    stream_buf = stream_buf[12 + length:]
                    if self._frame_carries_sps_or_idr(frame[12:]):
                        recent = [frame]
                        recent_bytes = len(frame)
                        self.replay_frame = frame
                    elif recent_bytes + len(frame) <= _REPLAY_BUFFER_BYTES:
                        recent.append(frame)
                        recent_bytes += len(frame)
                with self.lock:
                    self._recent = recent
                    self._stream_tail = stream_buf
                    dead = [sub for sub in self.subscribers if not sub.offer(chunk)]
                    for sub in dead:
                        self.subscribers.remove(sub)
        except OSError:
            pass
        finally:
            self.stopped.set()
            # 会话死亡（USB 重枚举/adb 断开/编码器退出）时必须主动关闭
            # 全部订阅者：否则 pump 永远等空队列、WS 保持打开，前端画面
            # 冻结且无任何提示（实机复现：点触摸标定后画面不同步，刷新
            # 才恢复）。先投递哨兵让 pump 退出循环，再由 pump 关闭 WS，
            # 前端 onclose 走既有回退（截图轮询）恢复画面。
            with self.lock:
                subs = list(self.subscribers)
            for sub in subs:
                sub.shutdown()

    # ---- 订阅者：asyncio 队列 + 事件循环内 pump 任务 ----
    #
    # 中继线程不能直接 await Starlette 的 send_bytes（它是协程，裸调用
    # 只创建协程对象、数据从未上线——实机复现"8 秒未出画面"的根因）。
    # 线程侧经 ``loop.call_soon_threadsafe`` 投递到每订阅者队列，pump
    # 任务在事件循环内真正 await 发送；队列满/断开由 pump 与线程两侧
    # 协同剔除。重放关键帧在 subscribe 时先入队，保证先于实时帧。

    def subscribe(self, ws) -> _Subscriber:
        """注册订阅者（必须在事件循环内调用）；返回待 pump 的订阅者。

        入队顺序（原子，事件循环单线程保证不被 offer 插队）：
        ``[自最近关键帧起的全部完整帧] + [半帧尾巴]``——与之后的实时
        chunk 字节流无缝衔接，客户端从关键帧解到实时，无参考断裂。
        """
        loop = asyncio.get_running_loop()
        sub = _Subscriber(ws, loop)
        with self.lock:
            self.idle_since = 0.0
            self.subscribers.append(sub)
            catchup = b"".join(getattr(self, "_recent", [])) \
                + getattr(self, "_stream_tail", b"")
        if catchup:
            sub.enqueue(catchup)
        return sub

    async def pump(self, sub: _Subscriber) -> None:
        """事件循环内真正发送订阅数据（由 WS handler create_task 驱动）。"""
        sent_bytes = 0
        session_ended = False
        try:
            while not self.stopped.is_set() and sub.alive:
                chunk = await sub.queue.get()
                if chunk is None:
                    session_ended = True   # 中继线程死亡：shutdown() 投递的哨兵
                    break
                if chunk == sub.PONG:
                    await sub.ws.send_json({"type": "pong"})
                    continue
                await sub.ws.send_bytes(chunk)
                sent_bytes += len(chunk)
                if sent_bytes == len(chunk):
                    logger.info("scrcpy stream: first send to subscriber (%d bytes, serial=%s)",
                                len(chunk), self.serial)
        except Exception:
            logger.warning("scrcpy stream: pump failed for %s", self.serial,
                           exc_info=True)
            sub.alive = False
        finally:
            self.unsubscribe(sub)
            if session_ended or self.stopped.is_set():
                # 会话已死：关掉这条 WS，让前端 onclose 触发截图回退。
                # stopped 分支覆盖哨兵被丢弃的边缘路径（队列满时 shutdown()
                # 投递失败，pump 只看到 alive=False 退出，拿不到 None）。
                try:
                    await sub.ws.close(code=4409)
                except Exception:
                    pass

    def unsubscribe(self, sub: _Subscriber) -> None:
        sub.alive = False
        with self.lock:
            if sub in self.subscribers:
                self.subscribers.remove(sub)
            if not self.subscribers:
                self.idle_since = time.time()

    def hello(self) -> dict[str, Any]:
        return {"codec": self.codec, "frames": self.frames}

    # ---- 清理 ----

    def stop(self) -> None:
        self.stopped.set()
        with self.lock:
            for sub in list(self.subscribers):
                sub.shutdown()
            self.subscribers.clear()
        self._teardown_processes(restore_stayon=True)

    def _teardown_processes(self, restore_stayon: bool = False) -> None:
        if restore_stayon and self.stayon_active:
            self.stayon_active = False
            try:
                _adb(self.serial, "shell", "svc", "power", "stayon", "false")
            except subprocess.SubprocessError:
                pass
        if self.video is not None:
            try:
                self.video.close()
            except OSError:
                pass
            self.video = None
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self.forward_port is not None:
            try:
                _adb(self.serial, "forward", "--remove", f"tcp:{self.forward_port}")
            except subprocess.SubprocessError:
                pass
            self.forward_port = None
        try:
            _adb(self.serial, "shell", "pkill", "-f", "gms_scrcpy_server")
        except subprocess.SubprocessError:
            pass


def start_stream(serial: str, owner_id: str = "") -> dict[str, Any]:
    """启动（或复用）设备的实时投屏会话；远端 Worker 版暂不支持。"""
    _ensure_reaper()
    with _lock:
        existing = _sessions.get(serial)
        if existing is not None and getattr(existing, "owner_id", "") != owner_id:
            raise ApiError.forbidden("投屏会话属于其他账号")
        if existing is not None and not existing.stopped.is_set() \
                and existing.proc is not None and existing.proc.poll() is None:
            return existing.hello()
        if existing is not None:
            existing.stop()
        if len(_sessions) >= _MAX_SESSIONS:
            stale = [key for key, session in _sessions.items() if session.stopped.is_set()]
            for key in stale:
                _sessions.pop(key, None)
            if len(_sessions) >= _MAX_SESSIONS:
                raise ApiError.invalid_semantics(
                    f"too many active scrcpy sessions (max {_MAX_SESSIONS})")
        session = ScrcpySession(serial)
        session.owner_id = owner_id
        try:
            info = session.start()
        except Exception:
            session.stop()
            raise
        _sessions[serial] = session
        return info


def stop_stream(serial: str) -> dict[str, Any]:
    with _lock:
        session = _sessions.pop(serial, None)
    if session is None:
        return {"stopped": False}
    session.stop()
    return {"stopped": True}


def get_stream(serial: str) -> ScrcpySession | None:
    with _lock:
        session = _sessions.get(serial)
        if session is None or session.stopped.is_set():
            return None
        return session


def stop_all() -> None:
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.stop()
