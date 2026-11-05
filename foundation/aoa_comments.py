"""Bounded descriptive annotations; command parsing and HID input stay separate."""
import json

from .aoa_profile import MAX_PROFILE_COMMANDS


MAX_COMMENT_LENGTH = 240
MAX_COMMENTS_BYTES = 65536


def validate_command_comments(comments, command_count):
    if (not isinstance(comments, list) or len(comments) != command_count
            or len(comments) > MAX_PROFILE_COMMANDS):
        raise ValueError("命令注释必须与 commands 逐条对应")
    if any(not isinstance(value, str) or len(value) > MAX_COMMENT_LENGTH for value in comments):
        raise ValueError(f"每条命令注释必须是最多 {MAX_COMMENT_LENGTH} 字的文本")
    try:
        size = len(json.dumps(comments, ensure_ascii=False).encode())
    except UnicodeEncodeError as exc:
        raise ValueError("命令注释包含无效 Unicode 文本") from exc
    if size > MAX_COMMENTS_BYTES:
        raise ValueError("命令注释总大小不能超过 64 KiB")
