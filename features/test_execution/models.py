from __future__ import annotations

from pydantic import BaseModel, Field


class TestStartRequest(BaseModel):
    # 空值表示部署配置中的本地 Worker。
    worker_id: str = ""
    test_type: str = ""
    test_module: str = ""
    test_case: str = ""
    retry_dir: str = ""
    test_suite: str = ""
    local_server: str = ""
    devices: list[str] = Field(default_factory=list)
    client_id: str = "test_client"
    automation_run_id: str = ""
    device_reservation_id: str = ""
    build_id: str = ""
    build_artifact_id: str = ""
    gerrit_change_id: str = ""
    gerrit_patchset: str = ""
    redmine_issue_id: str = ""


class TestParseArgsRequest(BaseModel):
    params: list[str] = Field(
        default_factory=list,
        description="命令行参数列表",
    )


class TestParseArgsResponse(BaseModel):
    success: bool = True
    device: str = ""
    test_type: str = ""
    test_module: str = ""
    test_case: str = ""
    test_suite: str = ""
    retry_dir: str = ""
    warnings: list[str] = Field(default_factory=list)
    help_text: str = ""


class SuiteApkAnalyzeRequest(BaseModel):
    suite_path: str
    path: str


class TradefedListResultsRequest(BaseModel):
    suite_path: str
    tradefed_bin: str | None = None


class TestSuiteDownloadRequest(BaseModel):
    url: str = Field(..., description="测试套件下载地址")
    save_dir: str | None = Field(
        default=None,
        description="保存目录（默认：~/GMS-Suite）",
    )


class TestSuiteExtractRequest(BaseModel):
    archive_path: str = Field(..., description="压缩包文件路径")
    extract_dir: str | None = Field(
        default=None,
        description="解压目录（默认：~/GMS-Suite）",
    )
    target_dir_name: str | None = Field(
        default=None,
        description="解压后的文件夹名称",
    )


class TestSuiteAddLocalRequest(BaseModel):
    path: str = Field(..., description="本地测试套件路径")


class SuiteDiagnosisTargetRequest(BaseModel):
    test_type: str = ""
    suite_version: str = ""
    module: str = ""
    class_names: list[str] = Field(default_factory=list)
    test_name: str = ""
    suite_path: str = ""
