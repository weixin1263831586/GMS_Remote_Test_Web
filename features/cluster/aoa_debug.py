"""Durable single-step input queue for a fenced, local continuous HID session."""
from foundation.aoa_profile import parse_profile_commands, required_hid_channels
from foundation.error_model import ApiError


def prepare_debug_request(repository, body, payload, devices, owner_id, *, is_local):
    if (not is_local or body.action != "aoa_sequence" or len(devices) != 1
            or body.automation_run_id or body.wait_adb_seconds
            or body.aoa_hid_session_mode != "continuous"
            or body.playback_control_id != body.aoa_debug_session_id
            or not body.aoa_debug_request_id):
        raise ApiError.conflict("保持 HID 的单条调试仅支持本机 Worker、连续连接和单台设备")
    try:
        selected = parse_profile_commands([body.aoa_debug_command])
        frozen = parse_profile_commands(body.aoa_commands)
    except ValueError as exc:
        raise ApiError.invalid_semantics(str(exc)) from exc
    operation_id = f"aoa-debug:{body.aoa_debug_session_id}"
    existing = repository.get_command_by_operation(body.worker_id, operation_id)
    if existing:
        current = existing.get("payload") or {}
        if current.get("owner_id") != owner_id:
            raise ApiError.not_found("AOA debug session not found")
        for key in ("aoa_hid_touch_mode", "aoa_hid_settle_seconds", "aoa_hid_session_mode"):
            if current.get(key) != payload.get(key):
                raise ApiError.conflict("调试会话的输入模式已固定，请结束调试后重新连接")
        if current.get("devices") != devices:
            raise ApiError.conflict("调试会话已绑定其他设备")
        frozen = parse_profile_commands(current["aoa_commands"])
    if not required_hid_channels(selected).issubset(required_hid_channels(frozen)):
        raise ApiError.conflict("此命令需要新的 HID 通道，请结束调试后重新连接")
    if existing:
        from .device_action_operations import fenced_local_payload

        try:
            fenced_local_payload(repository, existing["payload"])
        except RuntimeError as exc:
            raise ApiError.conflict(str(exc)) from exc
        sequence = repository.enqueue_aoa_debug_command(
            existing["id"], owner_id, body.aoa_debug_request_id, body.aoa_debug_command)
        return operation_id, {"success": True, "accepted": True, "command_id": existing["id"],
                              "debug_sequence": sequence, "operation_timeout_seconds": 960}
    payload["aoa_debug_queue"] = [{"sequence": 1, "request_id": body.aoa_debug_request_id,
                                   "command": body.aoa_debug_command}]
    return operation_id, None


def debug_command_source(repository, command_id):
    cursor = 0

    def read(*, close=False):
        nonlocal cursor
        entry = repository.read_aoa_debug_command(command_id, cursor, close=close)
        if entry:
            cursor = entry["sequence"]
        return entry

    return read
