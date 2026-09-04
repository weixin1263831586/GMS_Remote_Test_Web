"""GMS Windows Source Agent - firmware flash job executor (15.txt architecture).

常驻运行在 Windows 源主机的交互桌面会话（计划任务 /IT 自启动），监听文件
队列并驱动 RKDevTool GUI 完成完整固件烧写。

为什么必须常驻在桌面会话：SSH 会话（session 隔离）启动的 GUI 进程没有
可见窗口（实测 MainWindowHandle=0，双 backend 均枚举不到控件）；只有
交互桌面会话内的进程才能自动化 RKDevTool（实测「瑞芯微开发工具 v3.41」
cls=#32770 可枚举可操作）。

文件队列协议（Controller 经 SFTP 投递，Agent 轮询消费）：
    任务:   QUEUE_DIR\\<task_id>.json
            {"firmware": "C:\\gms-flash\\<task_id>\\update.img"}
    结果:   QUEUE_DIR\\<task_id>.result.json
            {"status": "SUCCESS"|"FAILED"|"TIMEOUT",
             "log_tail": "...", "error": "...", "elapsed_seconds": N}

烧写结果判定：轮询 RKDevTool 日志（Log 目录按天滚动）尾部，匹配
Download Firmware Success / Fail。Controller 端会另做 SFTP 拉取复核。
"""

import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from pywinauto import Desktop


RKDEVTOOL_EXE = r"D:\RKDevTool_v3.41_for_window\RKDevTool.exe"
RKDEVTOOL_LOG_DIR = r"D:\RKDevTool_v3.41_for_window\Log"
# 队列目录跟随运行 Agent 的 Windows 账户（Controller 端按 SSH 用户名
# 推导同一目录）。
QUEUE_DIR = str(Path.home() / "gms-flash-queue")
POLL_INTERVAL_SECONDS = 2.0
FLASH_TIMEOUT_SECONDS = 3600
WINDOW_CLASS = "#32770"
WINDOW_TITLE_FRAGMENT = "瑞芯微"
LAUNCH_WAIT_SECONDS = 60


def kill_existing_rkdevtool() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "RKDevTool.exe"],
        capture_output=True, timeout=20, check=False,
    )
    time.sleep(2)


def find_main_window():
    """Return the RKDevTool main window wrapper, or None."""
    for w in Desktop(backend="win32").windows():
        try:
            cls = w.element_info.class_name or ""
            txt = w.window_text() or ""
        except Exception:
            continue
        if cls == WINDOW_CLASS and WINDOW_TITLE_FRAGMENT in txt:
            return w
    return None


def launch_and_connect():
    """Reuse an already-running RKDevTool window, else launch a new one."""
    for _ in range(5):
        win = find_main_window()
        if win is not None:
            try:
                win.set_focus()
            except Exception:
                pass
            return win
        subprocess.Popen([RKDEVTOOL_EXE])
        deadline = time.time() + LAUNCH_WAIT_SECONDS
        while time.time() < deadline:
            win = find_main_window()
            if win is not None:
                return win
            time.sleep(1)
    raise RuntimeError("RKDevTool 主窗口未能启动")


def _wait_rkdevtool_idle(win, device: str = "") -> None:
    """Ensure the device is in Loader/Maskrom before clicking 升级.

    当 RKDevTool 显示 ADB 设备时，先 adb reboot loader 并等待ComboBox
    明确出现 Loader/Maskrom（不再以「无 ADB」作为判定，见
    _wait_for_loader_mode）。设备既不在 ADB 也看不到 Loader/Maskrom
    文本时（空设备列表等），视为目标不明确并终止烧写。

    ``device`` 是任务指定的 ADB serial：多设备在线时必须带 ``-s``，否则
    adb 直接报 more than one device。
    """
    try:
        combo_texts = [
            (c.window_text() or "")
            for c in win.descendants()
            if (c.element_info.class_name or "") == "ComboBox"
        ]
    except Exception:
        combo_texts = []
    for text in combo_texts:
        if "ADB" in text:
            _adb_reboot_loader(device)
            _wait_for_loader_mode(win, device=device)
            return
    # 未显示 ADB：必须已处于 Loader/Maskrom 才允许继续。
    for text in combo_texts:
        lowered = (text or "").casefold()
        if "loader" in lowered or "maskrom" in lowered or "rockusb" in lowered:
            return
    raise RuntimeError(
        "RKDevTool 未显示可确认的 Loader/Maskrom 设备，目标不明确，已终止烧写"
    )


def _wait_for_loader_mode(win, timeout: int = 120, device: str = "") -> None:
    """Wait until RKDevTool explicitly shows Loader/Maskrom.

    设备重新枚举期间 RKDevTool 可能一个设备都看不到，不能以「不再含
    ADB」判断；必须看到明确的 Loader/Maskrom/Rockusb 文本才放行，
    超时抛错终止烧写。

    已知限制：GUI 自动化只能确认「有设备处于 Loader/Maskrom」，无法
    直接把 ComboBox 文本绑定到目标 serial（RKDevTool 不暴露 USB 拓扑）。
    多设备并发烧写仍应通过任务队列互斥保证同一时刻只有一台目标设备。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            combo_texts = [
                (c.window_text() or "")
                for c in win.descendants()
                if (c.element_info.class_name or "") == "ComboBox"
            ]
        except Exception:
            continue
        for text in combo_texts:
            lowered = text.casefold()
            if "loader" in lowered or "maskrom" in lowered or "rockusb" in lowered:
                return
    raise RuntimeError(
        f"等待设备进入 Loader/Maskrom 模式超时（{timeout}s），已终止烧写"
    )

def _send_text_to_edit(edit_wrapper, text: str) -> None:
    """Set Edit text via WM_SETTEXT, bypassing pywinauto visibility checks.

    MFC Tab 页中非激活页的 Edit 控件句柄有效但 IsWindowVisible=False，
    pywinauto 的 set_edit_text 会抛 ElementNotVisible；窗口消息不受此限。
    """
    import win32con
    import win32gui
    hwnd = edit_wrapper.handle
    win32gui.SendMessage(
        hwnd, win32con.WM_SETTEXT, 0, text,
    )


def _click_button(button_wrapper) -> None:
    """Click a button via BM_CLICK, bypassing visibility checks."""
    import win32con
    import win32gui
    hwnd = button_wrapper.handle
    win32gui.SendMessage(
        hwnd, win32con.BM_CLICK, 0, 0,
    )


def _select_firmware_tab(win) -> None:
    """Switch the SysTabControl32 to the firmware-upgrade page.

    纯消息级操作：TCM_SETCURSEL 设置选中页，再向父窗口发送
    WM_NOTIFY(TCN_SELCHANGING/TCN_SELCHANGE) 让 MFC 感知页切换。
    不使用任何鼠标/键盘模拟。
    """
    import win32gui

    tabs = list(win.descendants(class_name="SysTabControl32"))
    if not tabs:
        return
    tab = tabs[0]
    tab_hwnd = tab.handle

    # 目标 Tab 索引：1（第二页，即固件升级页）
    target = 1
    current = win32gui.SendMessage(
        tab_hwnd, 0x130B, 0, 0,  # TCM_GETCURSEL
    )
    if current == target:
        return
    # TCM_SETCURFOCUS 触发完整选择流程（含 TCN 通知），不需要鼠标：
    win32gui.SendMessage(
        tab_hwnd, 0x133C, target, 0,  # TCM_SETCURFOCUS
    )
    time.sleep(1.5)


def find_flash_controls(win):
    """Locate the firmware path Edit and the 升级 button on the download page.

    window_text 比较必须忽略空白与全半角差异；MFC 窗口的 Button 文本带
    "&" 助记符（如 "&升级"）时也需剥离后再匹配。同时把按钮清单写入诊断
    文件，便于自动化失败时定位实际控件状态。
    """
    def button_label(control) -> str:
        text = (c.window_text() if (c := control) else "") or ""
        return text.replace("&", "").strip()

    # win32 backend 下 control_type 过滤在该 MFC 窗口上不稳定（实测
    # descendants(control_type="Button") 返回空甚至超时），改用原生窗口
    # 类名过滤：Button/Edit 是 Win32 标准类。
    long_edits, upgrade_buttons = [], []
    button_labels = []
    for c in win.descendants():
        try:
            cls = c.element_info.class_name or ""
            text = (c.window_text() or "").replace("&", "").strip()
        except Exception:
            continue
        if cls == "Button":
            button_labels.append(text)
            if text == "升级":
                # Tab 页隐藏的控件 is_visible() 可能为 False，但点击前
                # 会先切换 Tab 使其可见；这里只按文本匹配。
                upgrade_buttons.append(c)
        elif cls == "Edit":
            try:
                if c.rectangle().width() > 150:
                    long_edits.append(c)
            except Exception:
                pass
    if not upgrade_buttons:
        try:
            # 诊断输出：仅文本，供 btn-diag 文件排查按钮匹配。
            diag_texts = [
                (b.window_text() or "") for b in win.descendants(
                    control_type="Button")
            ]
            with open(os.path.join(QUEUE_DIR, ".btn-diag.txt"),
                      "w", encoding="utf-8") as f:
                f.write("\n".join(diag_texts))
        except Exception:
            pass
    return long_edits, upgrade_buttons


def _wait_device_not_adb(win, timeout: int = 120) -> bool:
    """Wait until the device leaves ADB (reboot loader in progress).

    RKDevTool 的「升级」只在 Loader/Maskrom 模式下生效；设备为 ADB 时
    必须先 `adb reboot loader`。检测方式：主窗口 ComboBox 文本含 "ADB"。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for c in win.descendants():
                if (c.element_info.class_name or "") == "ComboBox":
                    if "ADB" not in (c.window_text() or ""):
                        return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _adb_reboot_loader(device: str = "") -> None:
    """Reboot the target device into loader via ADB.

    必须带 `-s <serial>`：多设备同时在线时裸 `adb reboot loader`
    会报 more than one device。失败抛错终止烧写。
    """
    command = ["adb"]
    if device:
        command += ["-s", device]
    command += ["reboot", "loader"]
    completed = subprocess.run(
        command, capture_output=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.decode("utf-8", errors="replace").strip()
            or completed.stdout.decode("utf-8", errors="replace").strip()
        )
        raise RuntimeError(
            f"adb reboot loader 失败 ({device or 'no -s'}): {detail}"
        )


def _current_log_size(log_path: str) -> int:
    """Record the log offset BEFORE clicking 升级。

    RKDevTool 日志是按天滚动的纯追加文件；点击前取当前大小，
    之后只分析增量，避免把上一轮残留的 "Download Firmware Success"
    误判为本轮结果。
    """
    try:
        return os.path.getsize(log_path)
    except OSError:
        return 0


def poll_flash_log(start_size: int) -> dict:
    """Poll today's RKDevTool log until success/failure/timeout.

    只分析 start_size 之后的增量日志；同一窗口内先查失败标记再查
    成功标记，防止异常残页同时包含两种标记时误报成功。
    """
    today = datetime.now().strftime("Log%Y-%m-%d.txt")
    log_path = os.path.join(RKDEVTOOL_LOG_DIR, today)
    tail = ""
    deadline = time.time() + FLASH_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(5)
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                f.seek(max(0, start_size))
                content = f.read()
        except OSError:
            continue
        tail = content[-4000:]
        if (
            "Start to download image" in content
            and any(
                marker in content
                for marker in (
                    "Download Firmware Fail",
                    "Test Device Fail",
                )
            )
        ):
            return {"status": "FAILED", "log_tail": tail[-2000:]}
        if "Download Firmware Success" in content:
            return {"status": "SUCCESS", "log_tail": tail[-2000:]}
    return {"status": "TIMEOUT", "log_tail": tail[-2000:]}


def run_flash(firmware_path: str, device: str = "") -> dict:
    # 只在找不到可用主窗口时才冷启动；已有实例直接复用。
    win = find_main_window()
    if win is None:
        kill_existing_rkdevtool()
        win = launch_and_connect()
    time.sleep(2)
    # device 贯通到 reboot loader 路径：任务未指定 serial 时拒绝盲烧，
    # 防止多设备在线时 adb more than one device 或状态串台。
    if not device:
        raise RuntimeError(
            "任务未指定目标设备 serial，无法安全执行 adb reboot loader，已终止烧写"
        )
    _wait_rkdevtool_idle(win, device)

    edits, upgrade_buttons = find_flash_controls(win)
    if not upgrade_buttons:
        raise RuntimeError("未找到「升级」按钮")
    if not edits:
        raise RuntimeError("未找到固件路径输入框")
    _select_firmware_tab(win)
    # 「升级固件」组固件路径框：第一个长 Edit 是 Loader 路径，第二个是固件
    firmware_edit = edits[1] if len(edits) > 1 else edits[0]
    _send_text_to_edit(firmware_edit, firmware_path)
    time.sleep(1)
    today = datetime.now().strftime("Log%Y-%m-%d.txt")
    log_path = os.path.join(RKDEVTOOL_LOG_DIR, today)
    # 必须在点击升级之前记录日志偏移。
    start_size = _current_log_size(log_path)
    # 升级按钮同样可能处于"逻辑存在但未激活Tab"状态，用 BM_CLICK 消息
    _click_button(upgrade_buttons[0])
    return poll_flash_log(start_size)


def process_task(task_path: str) -> None:
    result_path = str(task_path).rsplit(".", 1)[0] + ".result.json"
    result = {"status": "FAILED", "log_tail": "", "error": ""}
    device = ""
    try:
        with open(task_path, encoding="utf-8") as f:
            task = json.load(f)
        firmware = str(task.get("firmware") or "")
        device = str(task.get("device") or "").strip()
        # Android serial 是外部设备提供的数据，进入 shell/路径前必须校验。
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", device or ""):
            result["error"] = f"任务缺少合法的 device serial: {device!r}"
            device = ""
        elif not firmware or not os.path.isfile(firmware):
            result["error"] = f"固件不存在: {firmware}"
        else:
            outcome = run_flash(firmware, device=device)
            result.update({
                "status": outcome["status"],
                "log_tail": outcome["log_tail"],
            })
    except Exception:
        result["error"] = traceback.format_exc()[-1500:]
    finally:
        tmp_path = result_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp_path, result_path)


def _claim_task(task_path: str) -> bool:
    """Atomically claim a task file (P0-1, exactly-once).

    os.replace 同一目录下是原子操作：两个 Agent 实例同时扫描时只有一个
    能把 <task>.json 改名为 <task>.claim，另一个必然抛 FileNotFoundError。
    已 claim 的任务不重复执行——这是阻止同一固件被无限重复烧写的关键。
    """
    claim_path = task_path[:-len(".json")] + ".claim"
    try:
        os.replace(task_path, claim_path)
    except OSError:
        return False
    return True


def _archive_claim(claim_path: str, result: dict) -> None:
    """Archive a processed claim next to its result (kept for auditing)."""
    try:
        if result.get("status") == "SUCCESS":
            os.remove(claim_path)
        else:
            failed_dir = os.path.join(QUEUE_DIR, "failed")
            os.makedirs(failed_dir, exist_ok=True)
            os.replace(
                claim_path,
                os.path.join(
                    failed_dir, os.path.basename(claim_path)
                ),
            )
    except OSError:
        pass


def main() -> int:
    os.makedirs(QUEUE_DIR, exist_ok=True)
    heartbeat = Path(QUEUE_DIR) / ".agent-alive"
    print("GMS Source Agent started", flush=True)
    while True:
        try:
            heartbeat.write_text(str(time.time()), encoding="ascii")
            for name in sorted(os.listdir(QUEUE_DIR)):
                if not name.endswith(".json") or name.endswith(
                    ".result.json"
                ) or name.endswith(".claim"):
                    continue
                task_path = os.path.join(QUEUE_DIR, name)
                # 原子认领：改名成功者才能执行，未认领的任务下轮再处理。
                # 认领失败（被其他实例抢先或文件已消失）直接跳过。
                if not _claim_task(task_path):
                    continue
                print(f"processing {name}", flush=True)
                claim_path = task_path[:-len(".json")] + ".claim"
                try:
                    process_task(claim_path)
                except Exception:
                    traceback.print_exc()
                finally:
                    # claim_path 以 ".claim" 结尾；历史上用 [:-len(".json")]
                    # 计算出 "flash-A..result.json"，归档永远不会命中。
                    result_path = claim_path[:-len(".claim")] + ".result.json"
                    try:
                        with open(result_path, encoding="utf-8") as f:
                            _archive_claim(claim_path, json.load(f))
                    except Exception:
                        traceback.print_exc()
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_INTERVAL_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
