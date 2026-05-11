"""
USB设备监控模块 - 监听USB插拔事件并自动刷新设备列表

支持两种模式:
1. udev模式: 通过pyudev库监听Linux内核的udev事件 (推荐，实时性高，CPU占用低)
2. 轮询模式: 定期轮询adb设备变化 (兼容性好，无需额外依赖)
"""
import logging
import asyncio
import threading
import time
from typing import Callable, Optional, Set, List, Tuple
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class USBMonitor:
    """
    USB设备监控器

    特性:
    - 监听USB设备插拔事件
    - 自动检测设备列表变化
    - 回调通知机制
    - 支持udev和轮询两种模式
    - 智能防抖，避免频繁触发

    性能对比:
    - udev模式: 事件驱动，~0% CPU，实时响应
    - 轮询模式: 定期检查，~0.1% CPU，2秒延迟
    """

    def __init__(
        self,
        device_getter: Callable[[], List[str]],
        on_devices_changed: Optional[Callable[[List[str]], None]] = None,
        check_interval: float = 2.0,
        use_udev: bool = True,
        debounce_count: int = 2  # 需要连续检测到变化的次数
    ):
        """
        初始化USB监控器

        Args:
            device_getter: 获取当前设备列表的函数
            on_devices_changed: 设备变化时的回调函数
            check_interval: 轮询模式的检查间隔(秒)，默认2秒
            use_udev: 是否优先使用udev模式
            debounce_count: 防抖计数，连续N次检测到变化才触发（避免抖动）
        """
        self.device_getter = device_getter
        self.on_devices_changed = on_devices_changed
        self.check_interval = check_interval
        self.use_udev = use_udev
        self.debounce_count = max(1, debounce_count)  # 至少为1

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._current_devices: Set[str] = set()

        # 防抖相关
        self._pending_changes: Optional[Set[str]] = None  # 待确认的变化
        self._debounce_counter = 0  # 防抖计数器
        self._last_check_time: Optional[datetime] = None  # 上次检查时间

        # 尝试导入pyudev
        self._pyudev_context = None
        self._pyudev_monitor = None
        self._pyudev_available = False

        if use_udev:
            try:
                import pyudev
                self._pyudev_context = pyudev.Context()
                self._pyudev_monitor = pyudev.Monitor.from_netlink(self._pyudev_context)
                self._pyudev_monitor.filter_by(subsystem='usb')
                self._pyudev_available = True
                logger.info("[USBMonitor] ✓ pyudev可用，将使用udev模式（高性能，事件驱动）")
            except ImportError:
                self._pyudev_available = False
                logger.warning("[USBMonitor] ✗ pyudev未安装，将使用轮询模式")
                logger.info("[USBMonitor] 提示: 安装pyudev可获得更好的性能: pip install pyudev")
            except Exception as e:
                self._pyudev_available = False
                logger.warning(f"[USBMonitor] ✗ pyudev初始化失败: {e}，将使用轮询模式")

    @property
    def is_running(self) -> bool:
        """监控器是否正在运行"""
        return self._running

    @property
    def mode(self) -> str:
        """返回当前使用的监控模式"""
        if self._pyudev_available:
            return "udev"
        return "polling"

    @property
    def pyudev_available(self) -> bool:
        """pyudev是否可用"""
        return self._pyudev_available

    def start(self):
        """启动USB监控"""
        if self._running:
            logger.warning("[USBMonitor] Monitor is already running")
            return

        self._running = True
        self._last_check_time = datetime.now()
        self._debounce_counter = 0
        self._pending_changes = None

        # 初始化当前设备列表
        try:
            devices = self.device_getter()
            self._current_devices = set(devices)
            logger.info(f"[USBMonitor] 初始设备列表: {devices}")
        except Exception as e:
            logger.error(f"[USBMonitor] Failed to get initial devices: {e}")

        # 启动监控线程
        if self.mode == "udev" and self._pyudev_available:
            self._thread = threading.Thread(
                target=self._udev_monitor_loop,
                name="USBMonitor-Udev",
                daemon=True
            )
            logger.info("[USBMonitor] 启动udev模式监控（高性能，实时响应）")
        else:
            self._thread = threading.Thread(
                target=self._polling_monitor_loop,
                name="USBMonitor-Polling",
                daemon=True
            )
            logger.info(f"[USBMonitor] 启动轮询模式监控（检查间隔: {self.check_interval}秒）")

        self._thread.start()

    def stop(self):
        """停止USB监控"""
        if not self._running:
            return

        logger.info("[USBMonitor] Stopping USB monitor")
        self._running = False

        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _udev_monitor_loop(self):
        """
        udev模式监控循环

        监听Linux内核的udev事件，当检测到USB设备变化时触发设备列表检查
        """
        logger.info("[USBMonitor] Udev monitor loop started")

        # 设置监控为非阻塞模式
        self._pyudev_monitor.start()

        while self._running:
            try:
                # 使用 pyudev 的正确 API 接收事件
                device = self._pyudev_monitor.poll(timeout=1.0)

                if device:
                    action = device.action
                    device_info = f"{device.get('ID_VENDOR', 'Unknown')} {device.get('ID_MODEL', 'Unknown')}"

                    logger.debug(f"[USBMonitor] USB event: {action} - {device_info}")

                    # 检测到USB add或remove事件后，延迟一小段时间再检查设备列表
                    # 这样可以让adb有足够时间识别设备
                    time.sleep(0.5)

                    # 检查设备列表是否变化
                    self._check_and_notify_devices()

            except Exception as e:
                if self._running:
                    logger.error(f"[USBMonitor] Error in udev monitor loop: {e}")
                time.sleep(1)

        logger.info("[USBMonitor] Udev monitor loop stopped")

    def _polling_monitor_loop(self):
        """
        轮询模式监控循环

        定期检查adb设备列表是否发生变化
        """
        logger.info(f"[USBMonitor] Polling monitor loop started (interval: {self.check_interval}s)")

        while self._running:
            try:
                self._check_and_notify_devices()
                time.sleep(self.check_interval)
            except Exception as e:
                if self._running:
                    logger.error(f"[USBMonitor] Error in polling monitor loop: {e}")
                time.sleep(self.check_interval)

        logger.info("[USBMonitor] Polling monitor loop stopped")

    def _check_and_notify_devices(self):
        """
        检查设备列表是否变化，如果变化则触发回调

        使用智能防抖机制：
        - 连续N次检测到相同变化才触发（避免抖动）
        - 减少不必要的回调和网络传输
        """
        try:
            current_devices = set(self.device_getter())
            now = datetime.now()

            # 如果设备列表没有变化，重置防抖计数器
            if current_devices == self._current_devices:
                self._debounce_counter = 0
                self._pending_changes = None
                self._last_check_time = now
                return

            # 检查时间间隔（避免频繁检查）
            if self._last_check_time:
                time_since_last_check = (now - self._last_check_time).total_seconds()
                if time_since_last_check < 0.5:  # 两次检查间隔至少0.5秒
                    return

            self._last_check_time = now

            # 防抖逻辑：连续N次检测到相同变化才触发
            if self._pending_changes is None or self._pending_changes != current_devices:
                # 检测到新的变化，重置计数器
                self._pending_changes = current_devices
                self._debounce_counter = 1
                logger.debug(
                    f"[USBMonitor] 检测到潜在变化 (1/{self.debounce_count}): "
                    f"{current_devices - self._current_devices} / {self._current_devices - current_devices}"
                )
                return

            # 连续检测到相同变化
            self._debounce_counter += 1

            if self._debounce_counter < self.debounce_count:
                logger.debug(
                    f"[USBMonitor] 确认变化进度 ({self._debounce_counter}/{self.debounce_count})"
                )
                return

            # 达到防抖阈值，确认是真正的变化
            old_devices = self._current_devices
            self._current_devices = current_devices

            added = current_devices - old_devices
            removed = old_devices - current_devices

            logger.info(
                f"[USBMonitor] ✓ 确认设备变化: "
                f"新增 {added if added else '无'}, 移除 {removed if removed else '无'} | "
                f"当前设备: {list(current_devices) if current_devices else '无'}"
            )

            # 重置防抖状态
            self._debounce_counter = 0
            self._pending_changes = None

            # 触发回调
            if self.on_devices_changed:
                try:
                    self.on_devices_changed(list(current_devices))
                except Exception as e:
                    logger.error(f"[USBMonitor] 回调执行错误: {e}")

        except Exception as e:
            logger.error(f"[USBMonitor] 检查设备时出错: {e}")

    def force_check(self):
        """强制立即检查设备列表变化"""
        if self._running:
            self._check_and_notify_devices()


# 全局USB监控器实例
_usb_monitor: Optional[USBMonitor] = None


def get_usb_monitor() -> Optional[USBMonitor]:
    """获取全局USB监控器实例"""
    return _usb_monitor


def init_usb_monitor(
    device_getter: Callable[[], List[str]],
    on_devices_changed: Optional[Callable[[List[str]], None]] = None,
    check_interval: float = 2.0,
    use_udev: bool = True
) -> USBMonitor:
    """
    初始化全局USB监控器

    Args:
        device_getter: 获取当前设备列表的函数
        on_devices_changed: 设备变化时的回调函数
        check_interval: 轮询模式的检查间隔(秒)
        use_udev: 是否优先使用udev模式

    Returns:
        USBMonitor实例
    """
    global _usb_monitor

    if _usb_monitor is not None:
        logger.warning("[USBMonitor] USB monitor already initialized, stopping old instance")
        _usb_monitor.stop()

    _usb_monitor = USBMonitor(
        device_getter=device_getter,
        on_devices_changed=on_devices_changed,
        check_interval=check_interval,
        use_udev=use_udev
    )

    return _usb_monitor


def start_usb_monitor():
    """启动全局USB监控器"""
    if _usb_monitor:
        _usb_monitor.start()
        logger.info(f"[USBMonitor] USB monitor started in {_usb_monitor.mode} mode")
    else:
        logger.error("[USBMonitor] USB monitor not initialized, call init_usb_monitor first")


def stop_usb_monitor():
    """停止全局USB监控器"""
    if _usb_monitor:
        _usb_monitor.stop()
        logger.info("[USBMonitor] USB monitor stopped")
