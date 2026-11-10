"""Additional project tool descriptions, shared by model discovery and execution."""


def definitions():
    return [
        {
            "name": "firmware_shares", "category": "burn", "description": "列出当前有效的固件分享（需管理员提权）",
            "api_path": "/api/firmware-shares", "method": "GET", "params": [],
            "keywords": ["可用固件", "分享固件", "firmware"], "is_readonly": True,
            "is_dangerous": False, "requires_confirm": False,
            "executor_ref": "features.firmware:list_firmware_shares", "response_type": "list",
        },
        {
            "name": "aoa_profiles", "category": "device", "description": "列出当前账号的可用 AOA 配置",
            "api_path": "/api/devices/aoa/profiles", "method": "GET", "params": [],
            "keywords": ["AOA配置", "AOA profiles"], "is_readonly": True,
            "is_dangerous": False, "requires_confirm": False,
            "executor_ref": "features.devices:list_aoa_profiles", "response_type": "list",
        },
    ]
