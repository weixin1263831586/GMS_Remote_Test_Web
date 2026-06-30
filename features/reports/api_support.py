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
            r"at\s+([a-z][a-z0-9.]*\.[A-Z][\w]*)\.([\w]+)"
            r"\(([\w.$]+)\.(kt|java):(\d+)\)"
        ),
        re.compile(r"\(([\w.$]+)\.(kt|java):(\d+)\)"),
    ]

    @classmethod
    def extract_failure_location(
        cls,
        stack_trace: str,
        test_name: str = "",
    ) -> dict[str, str] | None:
        if not stack_trace:
            return None
        test_class = (test_name.split("#")[0] if "#" in test_name else test_name).strip()
        candidates: list[tuple[str, str, str, str]] = []
        for pattern in cls.FAILURE_LOCATION_PATTERNS:
            for match in pattern.finditer(stack_trace):
                groups = match.groups()
                if len(groups) >= 5:
                    # Pattern 0: full_class, method, file_name, file_type, line
                    full_class = groups[0]
                    simple_class = groups[1]
                    file_name = groups[2]
                    file_type = groups[3]
                    line_number = groups[4]
                    if (
                        simple_class in cls.EXCLUDED_CLASSES
                        or file_name in cls.EXCLUDED_CLASSES
                    ):
                        continue
                else:
                    full_class = ""
                    file_name, file_type, line_number = groups[:3]
                candidates.append((full_class, file_name, file_type, line_number))

        # Prefer the actual throw site — the topmost application frame — as the
        # root-cause location. For a CTS TimeoutException the throw site (e.g.
        # ImeEventStreamTestUtils.java:138) is where the failure really happens;
        # the test-class frame (SearchViewTest.java:145) is merely the caller.
        # Stack order is preserved (finditer is left-to-right), so candidates[0]
        # is the deepest throw frame. We return the topmost frame that is NOT
        # the test class under diagnosis itself (which is only the caller),
        # falling back to the test-class frame and finally the raw topmost frame.
        def _is_diagnosed_test(full: str) -> bool:
            full = full or ""
            return bool(test_class) and (full == test_class or full.endswith(f".{test_class}"))

        # Decide where the failure actually originates:
        #   - If the topmost throw frame is the test class itself (a pure test
        #     assertion), that is the location.
        #   - Otherwise the topmost frame is a helper/framework (e.g. mockime's
        #     ImeEventStreamTestUtils), which is the real root-cause site —
        #     prefer it over the test-class caller below it.
        if candidates:
            top_full, top_file, top_type, top_line = candidates[0]
            if not _is_diagnosed_test(top_full) or len(candidates) == 1:
                return {"file_name": top_file, "file_type": top_type, "line_number": top_line}
            # Topmost is the test class but deeper frames exist — prefer the
            # first deeper non-test frame, else keep the test-class top frame.
            for full_class, file_name, file_type, line_number in candidates[1:]:
                if not _is_diagnosed_test(full_class):
                    return {"file_name": file_name, "file_type": file_type, "line_number": line_number}
            return {"file_name": top_file, "file_type": top_type, "line_number": top_line}
        return None
