"""
报告相关数据模型
"""
from pydantic import BaseModel, Field
from typing import Optional


class ReportListResponse(BaseModel):
    """报告列表响应"""
    timestamp: str
    test_type: str
    client_id: str
    devices: list
    result_dir: str
    suite_path: str
    status: str
    pass_count: Optional[int] = None
    fail_count: Optional[int] = None
    total_count: Optional[int] = None
    pass_rate: Optional[str] = None
    device: Optional[str] = None
    start_time: Optional[str] = None


class ReportFileRequest(BaseModel):
    """报告文件请求"""
    report_timestamp: str = Field(..., description="报告时间戳")


class ReportAnalysisResponse(BaseModel):
    """报告分析响应"""
    summary: dict
    device_info: dict
    test_info: dict
    failures: list
    failures_html: Optional[dict] = None
    host_log_errors: Optional[dict] = None
    device_log_errors: Optional[dict] = None


class ReportUploadRequest(BaseModel):
    """报告上传请求"""
    # 这个模型用于API文档说明
    # 实际文件上传通过multipart/form-data处理
    pass
