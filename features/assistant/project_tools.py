"""Additional project tool descriptions, shared by model discovery and execution."""


def definitions():
    return [
        {
            "name": "redmine_issue_analyze", "category": "redmine",
            "description": "提交 Redmine 单工单分析（正文、评论、附件和普通日志；异步跟踪结果）",
            "api_path": "/api/redmine-agent/daily-brief/analyze-issue", "method": "POST",
            "params": [
                {"name": "issue_ref", "type": "string", "required": True, "desc": "用户提供的完整工单 URL 或数字单号，工具会转换为 issue_id"},
                {"name": "analysis_mode", "type": "string", "required": False, "desc": "incremental（默认）；明确要求重新全量分析时用 full"},
                {"name": "analysis_hint", "type": "string", "required": False, "desc": "用户指定的分析重点，最多4000字"},
            ],
            "keywords": ["Redmine", "工单分析", "分析工单", "工单附件", "Redmine日志"],
            "is_readonly": False, "is_dangerous": False, "requires_confirm": False,
            "executor_ref": "", "response_type": "status",
        },
        {
            "name": "redmine_issue_analysis_status", "category": "redmine",
            "description": "按 run_id 查询 Redmine 工单分析的进度和结果（只读）",
            "api_path": "/api/redmine-agent/daily-brief/runs/{run_id}", "method": "GET",
            "params": [{"name": "run_id", "type": "string", "required": True, "desc": "提交分析时返回的原始 run_id"}],
            "keywords": ["Redmine", "工单分析进度", "工单分析结果"],
            "is_readonly": True, "is_dangerous": False, "requires_confirm": False,
            "executor_ref": "", "response_type": "detail",
        },
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
