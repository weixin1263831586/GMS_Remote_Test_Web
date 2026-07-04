"""Reports HTTP API facade."""

from fastapi import APIRouter

from . import analysis_api, files_api, source_api, weekly_report_api


router = APIRouter()
router.include_router(files_api.router)
router.include_router(source_api.router)
router.include_router(analysis_api.router)
router.include_router(weekly_report_api.router)

list_reports = files_api.list_reports
download_report = files_api.download_report
analyze_report_from_url = source_api.analyze_report_from_url
get_redmine_config = source_api.get_redmine_config
extract_redmine_attachment = source_api.extract_redmine_attachment
analyze_reports = analysis_api.analyze_reports
diagnose_report_failure = analysis_api.diagnose_report_failure
delete_report = analysis_api.delete_report
knowledgebase_search = analysis_api.knowledgebase_search
knowledgebase_stats = analysis_api.knowledgebase_stats
