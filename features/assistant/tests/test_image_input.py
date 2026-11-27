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
@pytest.mark.parametrize("capability", ["vision", "image_in"])
def test_visual_gateway_uses_only_enabled_local_vision_provider(fmt, capability):
    config = {"enabled": True, "primary_provider": "remote", "providers": {
        "remote": {"enabled": True, "capabilities": [capability], "base_url": "https://example.test", "model": "remote"},
        "local": {"enabled": True, "capabilities": [capability], "base_url": "http://127.0.0.1:8000",
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
    assert analyzer._post_chat.call_args.kwargs == {"allow_redirects": False, "direct": True}
    block = payload["messages"][-1]["content"][1]
    assert block["type"] == ("image" if fmt == "anthropic" else "image_url")
    config["providers"]["local"]["capabilities"] = []
    assert analyzer.image_provider() is None
    assert not analyzer.generate_image_analysis("Analyze", [image_data()])["success"]
    assert analyzer._post_chat.call_count == 1
    config["providers"]["local"]["capabilities"] = "image_in"
    assert analyzer.image_provider() is None
    config["providers"]["local"]["capabilities"] = ["thinking", "tool_use", "image_in"]
    config["providers"]["local"]["enabled"] = False
    assert analyzer.image_provider() is None


@pytest.mark.asyncio
async def test_image_turn_retains_private_attachments(monkeypatch, tmp_path):
    import json
    import uuid

    from features.assistant import image_input, session_store

    monkeypatch.setattr(session_store, "default_transcripts_dir", lambda: tmp_path)
    monkeypatch.setattr(image_input.users_runtime, "config_manager", Mock())
    monkeypatch.setattr("features.knowledge.retrieve_public_cdd_context", lambda _message: [])
    analyzer = Mock()
    analyzer.image_provider.return_value = "local"
    analyzer.generate_image_analysis.return_value = {"success": True, "content": "Visible result"}
    monkeypatch.setattr(image_input, "UniversalAIAnalyzer", lambda _config: analyzer)
    current = {"session_id": str(uuid.uuid4()), "client_id": "client-a", "owner_id": "owner-a",
               "status": "idle", "messages": [], "steps": []}
    raw = image_data()
    result = await image_input.analyze_image_turn(current, "Analyze", [raw])
    assert result.status_code == 200
    assert analyzer.generate_image_analysis.call_count == 1
    assert current["status"] == "idle"
    assert current["messages"][0]["data"]["image_count"] == 1
    assert current["messages"][-1]["content"] == "Visible result"
    assert current["steps"][-1]["status"] == "done"
    assert "data:image/" not in json.dumps(current)
    assert "agent_image_busy" not in current
    image_id = current["messages"][0]["data"]["image_attachments"][0]["id"]
    record = session_store.load_transcript(current["session_id"])
    assert record["messages"][0]["data"]["image_attachments"][0]["id"] == image_id
    content, mime = session_store.read_session_image(record, image_id, "owner-a")
    assert mime == "image/png" and Image.open(io.BytesIO(content)).size == (12, 12)
    assert session_store.read_session_image(record, image_id, "owner-b") is None
    assert session_store.read_session_image(record, "../file", "owner-a") is None
    assert not any(p.stat().st_mode & 0o077 for p in (tmp_path / current["session_id"]).iterdir())

    analyzer.image_provider.return_value = None
    result = await image_input.analyze_image_turn(current, "Analyze", [raw])
    assert result.status_code == 503
    assert analyzer.generate_image_analysis.call_count == 1
    assert len(current["messages"]) == 2
    assert current["status"] == "idle"

    session_store.drop_transcript(current["session_id"])
    assert not (tmp_path / current["session_id"]).exists()
    assert session_store.read_session_image(record, image_id, "owner-a") is None


@pytest.mark.asyncio
async def test_image_turn_passes_public_context_without_persisting_body(monkeypatch, tmp_path):
    import json
    import uuid

    import features.knowledge as knowledge
    from features.assistant import image_input, session_store

    context = [{"reference": "POL-" + "a" * 24, "snippet": "background-only"}]
    monkeypatch.setattr(knowledge, "retrieve_public_cdd_context", lambda _query: context)
    monkeypatch.setattr(session_store, "default_transcripts_dir", lambda: tmp_path)
    monkeypatch.setattr(image_input.users_runtime, "config_manager", Mock())
    analyzer = Mock()
    analyzer.image_provider.return_value = "local"
    analyzer.generate_image_analysis.return_value = {"success": True, "content": "Visible result"}
    monkeypatch.setattr(image_input, "UniversalAIAnalyzer", lambda _config: analyzer)
    current = {"session_id": str(uuid.uuid4()), "client_id": "client-a", "owner_id": "owner-a",
               "status": "idle", "messages": [], "steps": []}
    assert (await image_input.analyze_image_turn(current, "Analyze CDD", [image_data()])).status_code == 200
    assert analyzer.generate_image_analysis.call_args.kwargs == {"retrieval_context": context}
    meta = current["messages"][-1]["data"]["retrieval_context"]
    assert meta["references"] == [context[0]["reference"]] and not meta["coverage_complete"]
    assert "background-only" not in json.dumps(current)


@pytest.mark.parametrize("base_url", [
    "https://example.test", "http://user:password@127.0.0.1:8000",
    "http://127.0.0.1:8000?redirect=remote", "http://169.254.169.254",
])
def test_private_vision_rejects_non_pinned_routes_even_if_named_local(base_url):
    config = {"enabled": True, "providers": {"local_vision": {
        "enabled": True, "base_url": base_url, "model": "vision",
        "capabilities": ["image_in"], "auth_required": False,
    }}}
    analyzer = UniversalAIAnalyzer(config)
    analyzer._post_chat = Mock()
    assert analyzer.image_provider() is None
    assert not analyzer.generate_image_analysis("Analyze", [image_data()])["success"]
    analyzer._post_chat.assert_not_called()
