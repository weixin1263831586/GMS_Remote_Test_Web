"""Semantic header icons and dynamic control labels must match their actions."""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DIALOGS = ROOT / "web/shell/dialogs"


def _header_symbol(file_name: str) -> str:
    source = (DIALOGS / file_name).read_text(encoding="utf-8")
    glyph = source.split('class="tech-title-glyph"', 1)[1].split(
        'class="tech-title-divider"', 1
    )[0]
    found = re.search(r'ui\.svg#([a-z-]+)', glyph)
    assert found is not None, file_name
    return found.group(1)


def test_dialog_icons_follow_current_operation_semantics():
    # Configure schedule is not a refresh operation; choose a scan's scope
    # before execution is a search operation, not a data reload.
    assert _header_symbol("update-radar-settings.html") == "settings"
    assert _header_symbol("mainline-sync.html") == "search"
    assert _header_symbol("ui-control.html") == "layout"


def test_all_declared_icons_resolve_to_existing_svg_symbols():
    sprite = (ROOT / "web/static/icons/ui.svg").read_text(encoding="utf-8")
    symbols = set(re.findall(r'<symbol\s+id="([^"]+)"', sprite))
    assert {"settings", "search", "refresh", "layout"} <= symbols
    for path in sorted(DIALOGS.glob("*.html")):
        source = path.read_text(encoding="utf-8")
        used = set(re.findall(r'ui\.svg#([a-z][a-z0-9-]*)', source))
        assert used <= symbols, (path.name, sorted(used - symbols))


def test_radar_task_count_is_derived_from_current_controls():
    source = (ROOT / "web/static/js/update-radar-settings.js").read_text(encoding="utf-8")
    assert "const total = tasks().length" in source
    assert "count === total && total > 0" in source
    assert "count < total" in source
    assert " / ${total} 项" in source


def test_short_dialog_footers_use_shared_spacing_without_inline_gap():
    for filename in ("mainline-sync.html", "confirm.html"):
        source = (DIALOGS / filename).read_text(encoding="utf-8")
        assert 'class="modal-buttons tech-dialog-actions" style="margin-top:' not in source
        assert 'class="modal-buttons tech-dialog-actions"' in source


def test_file_picker_buttons_use_colored_folder_emoji():
    test_page = (ROOT / "web/shell/pages/test.html").read_text(encoding="utf-8")
    picker = re.search(r'<button[^>]*data-click="browseRemoteFile"[^>]*data-a0="retry"[^>]*>(.*?)</button>', test_page, re.DOTALL)
    assert picker and '📁' in picker.group(1) and '选择报告' in picker.group(1)
    assert 'ui.svg#folder' not in picker.group(1)

    gsi = (DIALOGS / "gsi.html").read_text(encoding="utf-8")
    for action, label in (("browseLocalFileForGsiScript", "选择 GSI 烧写脚本"),
                          ("browseLocalFileForGsiSystem", "选择 System 镜像")):
        picker = re.search(r'<button[^>]*data-click="' + action + r'"[^>]*>(.*?)</button>', gsi, re.DOTALL)
        assert picker and '📁' in picker.group(1)
        assert 'ui.svg#folder' not in picker.group(1)
        assert f'aria-label="{label}"' in picker.group(0)

    report = (ROOT / "web/static/js/pages/report-analysis.js").read_text(encoding="utf-8")
    choice = report.split('data-click="selectReportFolder"', 1)[1].split("</button>", 1)[0]
    assert '📁' in choice and 'ui.svg#folder' not in choice
    assert 'report-choice-folder-icon' in choice


def test_device_connection_dialog_headers_use_original_color_plug():
    for filename, title in (("adb-proxy.html", "ADB设备接入"),
                            ("usbip-attach.html", "USB/IP设备接入")):
        source = (DIALOGS / filename).read_text(encoding="utf-8")
        header = source.split('class="tech-title-group"', 1)[1].split('</div>', 1)[0]
        assert '<span class="tech-title-glyph" aria-hidden="true">🔌</span>' in header
        assert 'ui.svg#plug' not in header
        assert f'<span class="modal-title">{title}</span>' in header


def test_report_copy_action_uses_copy_not_refresh_icon():
    source = (ROOT / "web/shell/pages/test-suites.html").read_text(encoding="utf-8")
    button = source.split('id="btn-copy-test-report"', 1)[1].split("</button>", 1)[0]
    assert 'ui.svg#copy' in button
    assert 'ui.svg#refresh' not in button


def test_aoa_recovery_heading_uses_workflow_icon():
    source = (DIALOGS / "firmware.html").read_text(encoding="utf-8")
    section = source.split('class="modal-fieldset tech-section firmware-recovery-card"', 1)[1]
    heading = section.split("</h3>", 1)[0]
    assert 'ui.svg#workflow' in heading
    assert 'ui.svg#refresh' not in heading


def test_suite_refresh_and_test_toggle_preserve_requested_text_and_icon_contracts():
    source = (ROOT / "web/shell/pages/test.html").read_text(encoding="utf-8")
    suite = re.search(r'<button[^>]*id="refresh-suites-btn"[^>]*>(.*?)</button>', source, re.DOTALL)
    assert suite and suite.group(1).strip() == "刷新套件"
    test_toggle = re.search(r'<button[^>]*id="test-toggle-btn"[^>]*>(.*?)</button>', source, re.DOTALL)
    assert test_toggle and test_toggle.group(1).strip() == "开始测试"
    control = (ROOT / "web/static/js/pages/test-control.js").read_text(encoding="utf-8")
    for label in ("开始测试", "停止测试", "启动中", "停止中"):
        assert f"btn.textContent = '{label}'" in control
    assert "GmsUi.icon('play') + ' 开始测试'" not in control
    assert "GmsUi.icon('stop') + ' 停止测试'" not in control
    suites = (ROOT / "web/static/js/shell/workspace-devices.js").read_text(encoding="utf-8")
    assert "setActionButtonBusy(button, true, '刷新中')" in suites
    assert "await loadTestSuites(true)" in suites
    assert "setActionButtonBusy(button, false)" in suites


def test_test_workspace_action_icons_preserve_color_and_semantics():
    source = (ROOT / "web/shell/pages/test.html").read_text(encoding="utf-8")
    emoji_mapping = {
        "remountDevices": "⚡",
        "selectAllDevices": "✅",
        "factoryResetDevices": "🧹",
        "connectWifi": "🛜",
        "checkDeviceLockStatus": "🔐",
        "collectDeviceInfo": "📋",
        "burnFirmware": "🔥",
        "burnGsiImage": "🔥",
        "showDeviceScreen": "📺",
        "setupAdbPortForward": "🔌",
        "setupUsbipForward": "📱",
        "checkSshd": "🖥️",
        "checkRouting": "🧭",
        "connectVpn": "🔌",
    }
    for action, emoji in emoji_mapping.items():
        button = re.search(r'<button\b[^>]*data-click="' + action +
                           r'"[^>]*>(.*?)</button>', source, re.DOTALL)
        assert button is not None, action
        assert f'<span class="test-action-icon" aria-hidden="true">{emoji}</span>' in button.group(1), action

    # Reboot preserves the original red ⏻ glyph; Remount uses colorful ⚡.
    assert ('data-click="rebootDevices"><span class="reboot-icon" '
            'aria-hidden="true">⏻</span> 重启设备</button>') in source
    css = (ROOT / "web/static/css/ui-foundations.css").read_text(encoding="utf-8")
    assert '#page-test .adb-button-frame .reboot-icon' in css
    assert 'color: var(--danger-color, #f06b78);' in css
    assert ('data-click="remountDevices"' in source
            and 'class="test-action-icon" aria-hidden="true">⚡</span> Remount</button>' in source)
    assert 'ui.svg#mount' not in source
    assert 'USB/IP' in source
    assert '复制部署命令' in source
    plain_actions = {"refreshDevices": "刷新设备", "copyDeployCommand": "复制部署命令"}
    for action, label in plain_actions.items():
        button = re.search(r'<button\b[^>]*data-click="' + action + r'"[^>]*>(.*?)</button>', source, re.DOTALL)
        assert button and button.group(1).strip() == label, action
    for section in ("adb-button-frame", "vnc-button-frame", "vpn-btn-frame"):
        region = source.split('class="' + section + '"', 1)[1].split("</div>", 1)[0]
        buttons = re.findall(r'<button\b[^>]*>.*?</button>', region, re.DOTALL)
        assert buttons, section
        assert all('test-action-icon' in button or 'class="ui-icon"' in button
                   or 'class="reboot-icon" aria-hidden="true">⏻</span>' in button
                   or any(f'data-click="{action}"' in button for action in plain_actions)
                   for button in buttons), section


def test_dynamic_device_button_labels_preserve_color_icons():
    source = (ROOT / "web/shell/pages/test.html").read_text(encoding="utf-8")
    refresh = re.search(r'<button[^>]*id="refresh-devices-btn"[^>]*>(.*?)</button>', source, re.DOTALL)
    assert refresh and refresh.group(1).strip() == "刷新设备"
    devices = (ROOT / "web/static/js/pages/test-suite-browser-devices.js").read_text(encoding="utf-8")
    assert "setActionButtonBusy(button, true, '刷新中')" in devices
    assert "await loadDevices(true, {source: 'manual'})" in devices
    assert "setActionButtonBusy(button, false)" in devices
    sources = {
        "web/static/js/pages/firmware-burn-usbip.js": ("USB/IP", "连接中..."),
        "web/static/js/pages/vpn-control.js": ("连接 VPN", "断开设备"),
        "web/static/js/pages/auth-elevation.js": ("USB/IP", "断开设备"),
    }
    for path, expected in sources.items():
        script = (ROOT / path).read_text(encoding="utf-8")
        assert "GmsUi.testEmoji(" in script, path
        assert all(text in script for text in expected), path
    ui = (ROOT / "web/static/js/shell/ui-primitives.js").read_text(encoding="utf-8")
    assert 'class="test-action-icon"' in ui
    assert 'escape(glyph)' in ui
    vpn = (ROOT / "web/static/js/pages/vpn-control.js").read_text(encoding="utf-8")
    assert "btn.textContent = '🔌" not in vpn
    usbip = (ROOT / "web/static/js/pages/firmware-burn-usbip.js").read_text(encoding="utf-8")
    assert "GmsUi.icon('network') + ' USB/IP'" not in usbip


def test_ordinary_action_labels_do_not_reintroduce_decorative_icons():
    """Check static HTML and literal JS button templates across all workspaces."""
    roots = (ROOT / "web/shell", ROOT / "web/static/js", ROOT / "features")
    labels = {
        "刷新", "刷新设备", "刷新套件", "刷新列表", "刷新状态", "刷新端口",
        "刷新历史邮件", "刷新附件元数据", "刷新页面重试", "搜索", "查询",
        "添加", "编辑", "删除", "保存", "重置", "清空", "导出", "上传",
        "下载", "复制", "同步", "下载套件", "解压套件", "复制部署命令",
    }
    glyph = re.compile(r"^[\U0001F300-\U0001FAFF\uFE0F\u2600-\u27BF＋+↻⟳\s]+")
    failures = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".html", ".js"} or not path.is_file():
                continue
            if root.name == "features" and "ui" not in path.relative_to(root).parts:
                continue
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"<button\b([^>]*?)>(.*?)</button>", source, re.S):
                attrs, body = match.groups()
                if re.search(r'role="tab"|workspace-tab|filter-btn|aria-pressed|modal-close', attrs):
                    continue
                visible = re.sub(r"<[^>]+>", "", body).strip()
                visible = glyph.sub("", visible).strip()
                if visible not in labels:
                    continue
                if re.search(r"<svg\b|test-action-icon", body) or glyph.match(re.sub(r"<[^>]+>", "", body).strip()):
                    failures.append(f"{path.relative_to(ROOT)}:{source.count(chr(10), 0, match.start()) + 1}: {visible}")
    assert not failures, "Ordinary actions must use plain labels:\n" + "\n".join(failures)


def test_download_button_does_not_restore_a_legacy_glyph():
    source = (ROOT / "web/static/js/pages/test-suite-browser-transfer.js").read_text(encoding="utf-8")
    labels = re.findall(r"(?:downloadBtn|extractBtn)\.textContent\s*=\s*'([^']+)'", source)
    assert "下载套件" in labels and "下载中" in labels and "解压套件" in labels
    assert all(re.fullmatch(r"(?:下载|解压)[\u4e00-\u9fff. …]+", label) for label in labels), labels
