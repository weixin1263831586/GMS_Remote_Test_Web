from __future__ import annotations

import os

from foundation.config import config_manager


DEFAULT_ANDROID17_SHEET_URL = "https://docs.qq.com/sheet/DQnVLa3NVeHdISXpy?tab=BB08J2"


def android17_sheet_url() -> str:
    runtime = config_manager.get_runtime_config()
    configured = (runtime.get("weekly_report") or {}).get("android17_sheet_url")
    if not configured:
        configured = (config_manager.load_config().get("weekly_report") or {}).get("android17_sheet_url")
    return str(os.getenv("GMS_ANDROID17_SHEET_URL") or configured or DEFAULT_ANDROID17_SHEET_URL).strip()
