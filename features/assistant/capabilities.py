"""Build the Agent capability catalog independently of the chat router."""

from foundation.config import config_manager

from .test_runs import MAX_AGENT_RETRIES
from .tools import registry
from .workflow_execution import WORKFLOW_PLAN_TYPE as _WORKFLOW_PLAN_TYPE


def capabilities_payload(bridge: dict) -> dict:
    """List supported Agent capabilities with full tool catalog."""
    config = config_manager.load_config()
    categories = registry.get_all_categories()
    tools_by_category = {}
    for cat, tools in categories.items():
        tools_by_category[cat] = [
            {
                "name": t.name,
                "description": t.description,
                "api_path": t.api_path,
                "method": t.method,
                "readonly": t.is_readonly,
                "dangerous": t.is_dangerous,
            }
            for t in tools
        ]

    return {
        "tools": [
            "list_devices", "list_test_suites", "start_test", "monitor_test",
            "retry_test", "find_latest_report", "analyze_report", "diagnose_failure",
            "analyze_suite_apk", "read_decompiled_source",
            "automation_dashboard", "automation_runs", "automation_run_cancel",
            "automation_run_retry", "cluster_status", "cluster_workers",
            "cluster_devices", "cluster_jobs", "cluster_job_cancel",
            "build_jobs", "knowledge_search",
            "knowledge_ask", "knowledge_create",
            "redmine_issue_analyze", "redmine_issue_analysis_status",
        ],
        "tool_catalog": tools_by_category,
        "total_tools": len(registry),
        "limits": {"max_retries": MAX_AGENT_RETRIES},
        "suite_source": "local" if config_manager.is_config_host_local(config) else "ssh",
        # kkagent 会话桥接（P1）：规划/澄清/诊断走 kkagent，执行仍走状态机。
        "kkagent_bridge": bridge,
        "workflow_draft": {
            "plan_type": _WORKFLOW_PLAN_TYPE,
            "actions": ["plan.create", "plan.update", "plan.preflight",
                        "plan.confirm", "plan.cancel", "plan.status"],
        },
    }
