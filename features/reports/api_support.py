from __future__ import annotations

import re
from collections import OrderedDict
from typing import ClassVar


REDMINE_ISSUE_ID_CACHE: OrderedDict[str, str] = OrderedDict()
REDMINE_ISSUE_ID_CACHE_MAX_SIZE = 1000


def get_client_id_from_request(request) -> str:
    for header in ("x-client-id", "x-gms-client-id"):
        value = request.headers.get(header, "").strip()
        if value:
            return value
    username = request.headers.get("x-username", "unknown").strip() or "unknown"
    host = request.client.host if request.client else "unknown"
    return f"{username}@{host}"


class StackTraceUtils:
    EXCLUDED_CLASSES: ClassVar[set[str]] = {
        "Assert", "TestRunner", "TestCase", "TestUtil", "CtsTestUtil",
        "Mock", "FrameworkMethod", "Failures",
    }
    FAILURE_LOCATION_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(
            r"at\s+([a-z][a-z0-9.]*)\.([A-Z][\w]*)\.(\w+)"
            r"\(([\w.$]+)\.(kt|java):(\d+)\)"
        ),
        re.compile(r"\(([\w.$]+)\.(kt|java):(\d+)\)"),
    ]

    @classmethod
    def extract_failure_location(
        cls,
        stack_trace: str,
    ) -> dict[str, str] | None:
        if not stack_trace:
            return None
        for pattern in cls.FAILURE_LOCATION_PATTERNS:
            for match in pattern.finditer(stack_trace):
                groups = match.groups()
                if len(groups) >= 6:
                    file_name, file_type, line_number = (
                        groups[3], groups[4], groups[5]
                    )
                    if (
                        groups[1] in cls.EXCLUDED_CLASSES
                        or file_name in cls.EXCLUDED_CLASSES
                    ):
                        continue
                else:
                    file_name, file_type, line_number = groups[:3]
                return {
                    "file_name": file_name,
                    "file_type": file_type,
                    "line_number": line_number,
                }
        return None
