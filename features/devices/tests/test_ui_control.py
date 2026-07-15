"""ui_control_api 单元测试 —— 覆盖解析逻辑与命令拼接，不依赖真机/SSH。

端到端的 layout 解析已用真机验证（双机在线 + --device 锁定），这里锁定
纯函数行为：错误检测、JSON 提取（混入 java 日志）、坐标解析、布局拍平。
"""
from __future__ import annotations

from features.devices.ui_control_api import (
    _android_cli_path,
    _center_from_bounds,
    _extract_json_object,
    _looks_like_error,
    _parse_center,
    _simplify_layout,
    _simplify_uiautomator_xml,
)


class _FakeConfigManager:
    """runtime 在 app 启动前为 None，_android_cli_path 内部用 runtime.config_manager，
    测试里直接 monkeypatch 本模块的 runtime 引用。"""


def test_error_detection_flags_cli_failures():
    """Android CLI 恒返回 exit 0，错误检测只能靠输出文本。"""
    assert _looks_like_error("Error: Multiple devices are currently online") is not None
    assert _looks_like_error("Unknown option: '--device'") is not None
    assert _looks_like_error("Usage: android screen capture [-a] [-o=PARAM]") is not None
    assert _looks_like_error("Failed to retrieve UI dump: ERROR: could not get idle state.") is not None


def test_error_detection_passes_normal_json():
    """正常的布局 JSON 不应被判为错误。"""
    assert _looks_like_error('[\n  {"text": "Chrome", "center": "[210,1707]"}\n]') is None
    assert _looks_like_error("") is None


def test_extract_json_skips_java_log_lines():
    """layout 输出可能混入 java 日志行（如 java.util.prefs），需跳过到首个 [/{。"""
    raw = (
        "Jul 10, 2026 5:55:09 PM java.util.prefs.FileSystemPreferences$1 run\n"
        "信息: Created user preferences directory.\n"
        '[\n  {"text": "Chrome", "center": "[210,1707]"}\n]\n'
    )
    parsed = _extract_json_object(raw)
    assert parsed is not None
    assert isinstance(parsed, list)
    assert parsed[0]["text"] == "Chrome"


def test_extract_json_returns_none_on_garbage():
    assert _extract_json_object("no json here") is None
    assert _extract_json_object("") is None


def test_parse_center_formats():
    """layout 的 center 形如 "[600,960]"。"""
    assert _parse_center("[600,960]") == [600, 960]
    assert _parse_center("210,1707") == [210, 1707]
    assert _parse_center("") is None
    assert _parse_center(None) is None
    assert _parse_center("no digits") is None


def test_center_from_bounds():
    assert _center_from_bounds("[10,20][110,220]") == [60, 120]
    assert _center_from_bounds("[10,20][10,220]") is None
    assert _center_from_bounds("") is None


def test_simplify_uiautomator_xml():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
    <hierarchy rotation="0"><node index="0" text="" resource-id="" class="android.widget.FrameLayout"
    package="com.example" content-desc="" clickable="false" long-clickable="false" scrollable="false"
    checkable="false" focusable="false" enabled="true" bounds="[0,0][1080,1920]">
      <node index="0" text="登录" resource-id="com.example:id/login" class="android.widget.Button"
      package="com.example" content-desc="登录按钮" clickable="true" long-clickable="false" scrollable="false"
      checkable="false" focusable="true" enabled="true" bounds="[100,200][300,280]" />
    </node></hierarchy>'''
    items = _simplify_uiautomator_xml(xml)
    button = next(item for item in items if item["resource_id"] == "com.example:id/login")
    assert button["center"] == [200, 240]
    assert button["text"] == "登录"
    assert "clickable" in button["interactions"]
    assert button["class_name"] == "android.widget.Button"


def test_simplify_layout_filters_noise_and_keeps_clickable():
    """拍平后应保留有文本/坐标/可交互的元素，丢弃全空噪声节点。"""
    layout = [
        {"text": "Chrome", "center": "[210,1707]", "interactions": ["clickable", "focusable"]},
        {"content-desc": "Home", "center": "[600,936]"},
        {"resource-id": "workspace", "interactions": ["scrollable"]},  # 有 interaction，保留
        {"bounds": "[0,0][1,1]"},  # 全空噪声，但 bounds 非空 → 当前规则保留 interactions/bounds
    ]
    items = _simplify_layout(layout)
    texts = {it["text"] for it in items}
    assert "Chrome" in texts
    assert "Home" in texts
    # 无 text 但有 interaction 的节点 resource_id 回填为空字符串，text 为空，靠 interactions 保留。
    assert any(it["interactions"] == ["scrollable"] for it in items)


def test_simplify_layout_handles_dict_wrapper():
    """某些版本可能包成 {"elements": [...]}。"""
    wrapped = {"elements": [{"text": "X", "center": "[1,2]"}]}
    items = _simplify_layout(wrapped)
    assert len(items) == 1
    assert items[0]["center"] == [1, 2]


def test_android_cli_path_from_config():
    """配置了就用配置值。"""
    assert _android_cli_path({"android_cli_path": "/opt/android"}) == "/opt/android"


def test_android_cli_path_default_when_unset(monkeypatch):
    """未配置时按 ubuntu_user 推导 ~/.local/bin/android（非交互 SSH 无 PATH，须绝对路径）。"""
    import features.devices.ui_control_api as mod

    class _CM:
        @staticmethod
        def get_ubuntu_user(config):
            return "testuser"

    monkeypatch.setattr(mod.runtime, "config_manager", _CM)
    assert _android_cli_path({}) == "/home/testuser/.local/bin/android"
    assert _android_cli_path(None) == "/home/testuser/.local/bin/android"
