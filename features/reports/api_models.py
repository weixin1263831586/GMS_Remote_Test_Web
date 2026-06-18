from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class AnalysisMode(str, Enum):
    """Report analysis request modes."""

    UPLOAD = "upload"
    SAVED = "saved"
    AI = "ai"


class ReportDiagnosisRequest(BaseModel):
    test_name: str
    error_message: str = ""
    stack_trace: str = ""
    report_name: str = ""
    failure_index: int = 0
    source_path: str = ""
    source_code: str = ""
    test_type: str = ""
    suite_version: str = ""
    module: str = ""
    class_names: list[str] = Field(default_factory=list)
