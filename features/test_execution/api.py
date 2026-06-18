from fastapi import APIRouter

from .execution_api import router as execution_router
from .execution_api import start_test as start_test
from .execution_api import stop_test as stop_test
from .logs_api import (
    clean_test_logs as clean_test_logs,
)
from .logs_api import (
    download_test_logs as download_test_logs,
)
from .logs_api import (
    get_test_logs as get_test_logs,
)
from .logs_api import (
    list_test_logs as list_test_logs,
)
from .logs_api import router as logs_router
from .logs_api import (
    save_current_log as save_current_log,
)
from .parse_api import parse_test_args as parse_test_args
from .parse_api import router as parse_router
from .status_api import get_status as get_status
from .status_api import router as status_router
from .status_api import stream_test_logs as stream_test_logs
from .suite_helpers import (
    _get_available_test_suites as _get_available_test_suites,
)
from .suite_helpers import (
    _make_empty_suite_target as _make_empty_suite_target,
)
from .suite_helpers import (
    _resolve_suite_diagnosis_target as _resolve_suite_diagnosis_target,
)
from .suites_api import (
    create_suite_apk_analysis_task as create_suite_apk_analysis_task,
)
from .suites_api import (
    diagnose_suite_target as diagnose_suite_target,
)
from .suites_api import (
    download_suite_file as download_suite_file,
)
from .suites_api import (
    list_suite_files as list_suite_files,
)
from .suites_api import (
    list_suites as list_suites,
)
from .suites_api import (
    router as suites_router,
)
from .transfers_api import (
    add_local_test_suite as add_local_test_suite,
)
from .transfers_api import (
    download_test_suite_from_url as download_test_suite_from_url,
)
from .transfers_api import (
    extract_test_suite_archive as extract_test_suite_archive,
)
from .transfers_api import (
    get_test_suite_download_status as get_test_suite_download_status,
)
from .transfers_api import (
    get_test_suite_extract_status as get_test_suite_extract_status,
)
from .transfers_api import (
    list_test_suite_archives as list_test_suite_archives,
)
from .transfers_api import (
    list_tradefed_results as list_tradefed_results,
)
from .transfers_api import (
    router as transfers_router,
)
from .transfers_api import (
    start_test_suite_extract as start_test_suite_extract,
)


router = APIRouter()
for child_router in (
    parse_router,
    execution_router,
    logs_router,
    suites_router,
    transfers_router,
    status_router,
):
    router.include_router(child_router)
