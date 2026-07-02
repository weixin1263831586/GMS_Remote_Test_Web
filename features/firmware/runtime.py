from __future__ import annotations

from typing import Any


config_manager: Any = None
ssh_manager: Any = None
global_state: Any = None
safe_websocket_send: Any = None
store_notification: Any = None
generate_help_or_continue: Any = None
get_client_id_from_request: Any = None
project_root: Any = None
apk_upload_dir: Any = None
apk_max_tasks: int = 20
apk_max_file_size: int = 0
apk_max_source_file_size: int = 0
jadx_path: str = "jadx"
jadx_timeout: int = 300
gsi_progress_increment: int = 1
gsi_progress_max: int = 95
gsi_progress_poll_interval: float = 1
lock_firmware_devices: Any = None
release_firmware_devices: Any = None
firmware_share_store: Any = None


def configure_runtime(**values: Any) -> None:
    globals().update(values)
