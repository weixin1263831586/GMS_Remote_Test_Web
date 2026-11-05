"""Annotations persist with their owning command and cannot desynchronize rows."""
import pytest

from features.devices.aoa_service import AoaProfileStore
from foundation.error_model import ApiError


def test_comment_roundtrip_preserves_commands_and_owner_isolation(tmp_path):
    store = AoaProfileStore(tmp_path)
    commands = ["click 180 320", "sleep 800"]
    comments = ["点击 Start <b>说明</b>", "等待页面加载"]
    profile = store.create_profile("owner", "Annotated", "", commands, metadata={"command_comments": comments})
    assert profile["commands"] == commands
    assert store.get_profile(profile["id"], "owner")["metadata"]["command_comments"] == comments
    with pytest.raises(ApiError):
        store.get_profile(profile["id"], "other")
    with pytest.raises(ApiError):
        store.update_profile(profile["id"], "owner", commands=[commands[0]])
    assert store.get_profile(profile["id"], "owner")["commands"] == commands
    updated = store.update_profile(profile["id"], "owner", commands=[commands[1]], metadata={"command_comments": [comments[1]]})
    assert updated["metadata"]["command_comments"] == [comments[1]]


@pytest.mark.parametrize("comments", [[], ["one"], {"0": "note"}, "note", [1, ""], ["字" * 241, ""]])
def test_invalid_comment_shape_length_and_size_never_persist(tmp_path, comments):
    store = AoaProfileStore(tmp_path)
    with pytest.raises(ApiError) as error:
        store.create_profile("owner", "Invalid", "", ["home", "back"], metadata={"command_comments": comments})
    assert error.value.status_code == 422
    assert store.list_profiles("owner") == []


def test_comment_budget_is_separate_from_other_metadata(tmp_path):
    store = AoaProfileStore(tmp_path)
    commands, comments = ["home"] * 128, ["说明" * 30] * 128
    profile = store.create_profile("owner", "Many comments", "", commands, metadata={"command_comments": comments})
    assert profile["metadata"]["command_comments"] == comments
    with pytest.raises(ApiError):
        store.update_profile(profile["id"], "owner", metadata={"command_comments": comments, "padding": "x" * 8193})
    with pytest.raises(ApiError):
        store.create_profile("owner", "Too large", "", ["sleep 1"] * 256, metadata={"command_comments": ["字" * 240] * 256})
