"""Tests router - test execution, suite management, and log APIs."""

import logging
from enum import Enum

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import PlainTextResponse

from features.test_execution.models import (
    TestParseArgsRequest,
    TestParseArgsResponse,
)
from foundation.responses import error_response, success_response


class LogLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


class ApiResponse:
    @staticmethod
    def success(data=None, message="操作成功"):
        return success_response(data=data, message=message)

    @staticmethod
    def error(error, status_code=500, **extra_fields):
        return error_response(
            error,
            status_code=status_code,
            **extra_fields,
        )

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Upload progress constants ---
UPLOAD_PROGRESS_EXPIRATION = 10

# ==================== Parse Args ====================

@router.post("/api/test/parse-args")
async def parse_test_args(
    request: Request,
    h: str | None = Query(None),
    help: bool = Query(False),
    req: TestParseArgsRequest = Body(None),
):
    """Parse test launch arguments - smart recognition of CLI parameters."""
    if help or req is None:
        help_text = """API: /api/test/parse-args

Function: Smart parse test launch command line arguments

Direct test mode params:
  params: ["DEVICE", "TYPE", "MODULE/SUITE", "CASE/SUITE", "SUITE"]

Retry mode params:
  params: ["--retry", "REPORT_TIMESTAMP", "DEVICE", "TYPE", "SUITE"]

Supported Test Types: CTS, GTS, GTS-ROOT, STS, VTS, APTS, GSI
"""
        return PlainTextResponse(content=help_text, headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=300"})

    if req is None or not req.params:
        return error_response("Missing params", 400)

    params = req.params
    first_param = params[0] if params else ""

    result = {
        "success": True,
        "device": "",
        "test_type": "",
        "test_module": "",
        "test_case": "",
        "test_suite": "",
        "retry_dir": "",
        "warnings": [],
    }

    if first_param == "--retry":
        if len(params) < 2:
            return error_response("Report timestamp required for retry mode", 400)
        result["retry_dir"] = params[1]
        if len(params) > 2:
            result["device"] = params[2]
        if len(params) > 3:
            third_param = params[3]
            if "/" in third_param:
                result["test_suite"] = third_param
                result["warnings"].append("Test type will be auto-detected from suite path")
            else:
                result["test_type"] = third_param
                if len(params) > 4:
                    fourth_param = params[4]
                    if "/" in fourth_param:
                        result["test_suite"] = fourth_param
                    else:
                        result["warnings"].append(f"Fourth parameter ignored (expected suite path, got: {fourth_param})")
        else:
            result["warnings"].append("Neither test type nor suite specified")
        return TestParseArgsResponse(**result)

    result["device"] = params[0] if len(params) > 0 else ""
    result["test_type"] = params[1] if len(params) > 1 else ""

    param3 = params[2] if len(params) > 2 else ""
    param4 = params[3] if len(params) > 3 else ""
    param5 = params[4] if len(params) > 4 else ""

    if param3:
        if "/" in param3:
            result["test_suite"] = param3
        else:
            result["test_module"] = param3

    if param4:
        if result["test_suite"]:
            result["test_case"] = param4
        else:
            if "/" in param4:
                result["test_suite"] = param4
            else:
                result["test_case"] = param4

    if param5 and not result["test_suite"]:
        if "/" in param5:
            result["test_suite"] = param5
        else:
            if result["test_case"]:
                result["warnings"].append(f"Fifth parameter ignored (unexpected: {param5})")
            else:
                result["test_case"] = param5

    return TestParseArgsResponse(**result)


