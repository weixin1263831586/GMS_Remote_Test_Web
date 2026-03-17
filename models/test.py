"""
测试相关数据模型
"""
from pydantic import BaseModel, Field
from typing import List, Optional


class TestStartRequest(BaseModel):
    """测试启动请求"""
    test_type: str = Field(default="cts", description="测试类型: cts, gts, vts, sts")
    test_module: Optional[str] = Field(default="", description="测试模块")
    test_case: Optional[str] = Field(default="", description="测试用例")
    retry_dir: Optional[str] = Field(default="", description="重试目录")
    test_suite: Optional[str] = Field(default="", description="测试套件路径")
    local_server: Optional[str] = Field(default="", description="本地服务器")
    devices: List[str] = Field(default_factory=list, description="设备列表")


class TestStopRequest(BaseModel):
    """测试停止请求"""
    # 无需参数


class TestStatusResponse(BaseModel):
    """测试状态响应"""
    running: bool
    devices: List[str]
    test_type: Optional[str] = None
    logs: Optional[List[str]] = None
    log_count: Optional[int] = None


class TestSuiteAutocompleteRequest(BaseModel):
    """测试套件自动补全请求"""
    test_type: str = Field(..., description="测试类型")
    base_path: str = Field(..., description="基础路径")
