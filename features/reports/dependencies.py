from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from foundation.files import FileUtils


if TYPE_CHECKING:
    from .failure_analysis import ReportFailureAnalyzer


class SuiteTargetResolver(Protocol):
    def __call__(
        self,
        config: dict[str, Any],
        *,
        test_type: str = '',
        suite_version: str = '',
        module: str = '',
        test_name: str = '',
        class_names: list[str] | None = None,
        suite_path: str = '',
        source_path: str = '',
    ) -> dict[str, Any]: ...


class EmptySuiteTargetFactory(Protocol):
    def __call__(
        self,
        test_type: str = '',
        suite_version: str = '',
        suite_path: str = '',
        suite_root: str = '',
        suite_name: str = '',
        test_name: str = '',
        class_names: list[str] | None = None,
        match_notes: list[str] | None = None,
    ) -> dict[str, Any]: ...


@dataclass
class ReportDependencies:
    file_utils: type[FileUtils] | None = None
    universal_analyzer_factory: Callable[[], ReportFailureAnalyzer] | None = None
    resolve_suite_target: SuiteTargetResolver | None = None
    make_empty_suite_target: EmptySuiteTargetFactory | None = None


dependencies = ReportDependencies()


def configure_report_dependencies(
    *,
    file_utils: type[FileUtils],
    universal_analyzer_factory: Callable[[], ReportFailureAnalyzer],
    resolve_suite_target: SuiteTargetResolver,
    make_empty_suite_target: EmptySuiteTargetFactory,
) -> None:
    dependencies.file_utils = file_utils
    dependencies.universal_analyzer_factory = universal_analyzer_factory
    dependencies.resolve_suite_target = resolve_suite_target
    dependencies.make_empty_suite_target = make_empty_suite_target
