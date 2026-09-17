"""VNC 端口、进程匹配模式与 x11vnc 启动参数的唯一定义点。

本地（``vnc.py``）与远程（``vnc_remote.py``）启动路径依赖同一组端口、
进程模式和 CLI 旗标；集中在这里保证两侧命令行为一致，测试也据此
断言命令格式。修改任一旗标前先读对应注释了解回归背景。
"""

import uuid

from foundation.novnc import NOVNC_WEB_PORT


VNC_DISPLAY = ':0'
VNC_PORT = 5900
REMOTE_NOVNC_DIR = '/opt/noVNC'


def x11vnc_port_pattern(port: int = VNC_PORT) -> str:
    return f'x11vnc.*-rfbport {port}'


def x11vnc_display_pattern(display: str = VNC_DISPLAY) -> str:
    return f'x11vnc.*{display}'


def websockify_pattern(web_port: int = NOVNC_WEB_PORT) -> str:
    return f'websockify.*{web_port}'


def vnc_password_temp_path() -> str:
    return f'/tmp/.gms_vnc_passwd_{uuid.uuid4().hex}'


LOCAL_X11VNC_PATTERN = x11vnc_port_pattern()
X11VNC_DISPLAY_PATTERN = x11vnc_display_pattern()
WEBSOCKIFY_PATTERN = websockify_pattern()

# x11vnc 性能参数：合成型窗口管理器（GNOME/KDE）下 XDamage 事件风暴会让
# x11vnc 卡顿甚至停顿，改用快速轮询检测变化；降低 wait/defer 提高刷新率
# 并降低延迟；-threads 让每个客户端的输入/输出在独立线程处理。
# Some desktop sessions exhaust their MIT-SHM allocation even though :0 is
# otherwise reachable.  -noshm keeps x11vnc alive by using XGetImage polling.
X11VNC_PERF_FLAGS = ('-threads', '-noxdamage', '-wait', '5', '-defer', '5', '-noshm')
X11VNC_PERF_ARGS = ' '.join(X11VNC_PERF_FLAGS)

# x11vnc 默认 -norepeat 会在有 VNC 客户端时关闭 X11 自动重复，导致方向键
# 等按键长按只触发一次。noVNC 能正确转发重复 keydown，因此显式保留 X11
# 的自动重复。
X11VNC_INPUT_FLAGS = ('-repeat',)
X11VNC_INPUT_ARGS = ' '.join(X11VNC_INPUT_FLAGS)

# x11vnc 大小写参数：远端 X 的 CapsLock 状态会把客户端发来的按键大小写
# 反向（noVNC 的按键 keysym 已携带本地大小写，远端再叠加一次 caps 会双
# 重取反），而 x11vnc 不支持 QEMU LED 状态回传，noVNC 的自动纠偏不会触
# 发。仅用 -clear_mods 释放可能卡住的普通修饰键；不可用 -clear_all，后者
# 会清除 NumLock，使 noVNC 数字键盘的数字键被当作导航键。-skip_lockkeys
# 令大小写和数字锁状态完全由客户端的 keysym 决定，并把 KP_n 映射为普通
# 数字，避免客户端与远端的锁定状态不同而使数字键盘失效。
X11VNC_KEYMAP_FLAGS = ('-clear_mods', '-skip_lockkeys')
X11VNC_KEYMAP_ARGS = ' '.join(X11VNC_KEYMAP_FLAGS)
