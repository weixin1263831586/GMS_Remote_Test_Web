"""Clipboard image validation and visual provider isolation."""

import base64
import io
from unittest.mock import Mock

import pytest
from PIL import Image

from features.assistant.image_input import prepare_images
from features.kkagent import UniversalAIAnalyzer


def image_data(fmt="PNG", mime="png"):
    buffer = io.BytesIO()
    Image.new("RGB", (12, 12), "red").save(buffer, format=fmt)
    return f"data:image/{mime};base64," + base64.b64encode(buffer.getvalue()).decode()


def test_clipboard_image_validates_real_content():
    result = prepare_images([image_data()])
    assert result[0].startswith("data:image/png;base64,")
    with pytest.raises(ValueError):
        prepare_images([image_data("JPEG", "png")])
    with pytest.raises(ValueError):
        prepare_images(["https://example.test/image.png"])
    with pytest.raises(ValueError):
        prepare_images(["data:image/png;base64,aaaa"])
    with pytest.raises(ValueError):
        prepare_images([image_data()] * 4)


@pytest.mark.parametrize("fmt", ["openai", "anthropic"])
def test_visual_gateway_uses_only_enabled_local_vision_provider(fmt):
    config = {"enabled": True, "primary_provider": "remote", "providers": {
        "remote": {"enabled": True, "capabilities": ["vision"], "base_url": "https://example.test", "model": "remote"},
        "local": {"enabled": True, "capabilities": ["vision"], "base_url": "http://127.0.0.1:8000",
                  "model": "vision", "api_format": fmt, "auth_required": False},
    }}
    analyzer = UniversalAIAnalyzer(config)
    assert analyzer.image_provider() == "local"
    response = ({"content": [{"type": "text", "text": "visible"}]} if fmt == "anthropic"
                else {"choices": [{"message": {"content": "visible"}}]})
    analyzer._post_chat = Mock(return_value=Mock(status_code=200, json=lambda: response))
    result = analyzer.generate_image_analysis("Analyze", [image_data()])
    assert result == {"success": True, "content": "visible"}
    url, _, payload, actual_format = analyzer._post_chat.call_args.args
    assert url.startswith("http://127.0.0.1:8000/")
    assert actual_format == fmt
    block = payload["messages"][-1]["content"][1]
    assert block["type"] == ("image" if fmt == "anthropic" else "image_url")
    config["providers"]["local"]["capabilities"] = []
    assert analyzer.image_provider() is None
    assert not analyzer.generate_image_analysis("Analyze", [image_data()])["success"]
    assert analyzer._post_chat.call_count == 1
