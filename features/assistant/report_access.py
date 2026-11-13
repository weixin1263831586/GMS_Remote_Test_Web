"""Bind report navigation to its actual source and supported browser routes."""

from __future__ import annotations

from urllib.parse import urlencode


def bind_report_access(report):
    registered = report.get("registered") is True
    access = {"report_management": registered, "saved_report_analysis": registered,
              "failure_details_loaded": False, "failure_diagnosis_ready": False}
    report["access"] = access
    if registered:
        action = {"label": "打开已登记报告列表", "page": "reports",
                  "params": {"worker_id": report.get("worker_id", "")}}
        access.update(open_action=action, instruction="已登记，可在报告管理列表查看；本次查询只提供摘要。")
        return action

    # A native run timestamp is not a saved-report identifier. Neither a
    # management-page shortcut nor a guessed HTML API can open this record.
    report.pop("report_url", None)
    report.pop("browse_url", None)
    access["instruction"] = "未登记的原生结果，请在测试套件浏览器查看；报告管理列表不展示此项，失败分析需先读取用例明细或日志。"
    worker = str(report.get("worker_id") or "")
    suite = str(report.get("suite_path") or "")
    directory = str(report.get("result_directory") or "")
    if not worker or not suite or directory in {"", ".", ".."} or any(c in directory for c in "/\\"):
        return None
    params = {"worker_id": worker, "suite_path": suite, "path": f"results/{directory}"}
    action = {"label": "查看原生报告目录", "page": "test-suites", "params": params}
    access["open_action"] = action
    report["browse_url"] = "#test-suites?" + urlencode(params)
    return action
