"""Resolve a Tradefed report's plan from its XML metadata."""

from collections.abc import Mapping


_GTS_VARIANTS = {"gts-root", "gts-interactive"}
_PLAN_ATTRIBUTES = ("suite_plan", "test_plan", "suitePlan", "testPlan", "plan")


def _first_attr(attributes: Mapping[str, str], names: tuple[str, ...]) -> str:
    return next((attributes[name] for name in names if attributes.get(name)), "")


def report_test_type(result: Mapping[str, str], build: Mapping[str, str]) -> str:
    """Use explicit GTS variant metadata while preserving other suite names."""
    suite = _first_attr(result, ("suite_name", "suite", "test_type", "testType", "type", "Type")) or _first_attr(
        build, ("test_type", "testType"),
    )
    normalized_suite = suite.strip().lower().replace("_", "-")
    if normalized_suite in _GTS_VARIANTS:
        return normalized_suite.upper()

    plan = _first_attr(result, _PLAN_ATTRIBUTES) or _first_attr(build, _PLAN_ATTRIBUTES)
    normalized_plan = plan.strip().lower().replace("_", "-")
    if normalized_suite in ("", "gts") and normalized_plan in _GTS_VARIANTS:
        return normalized_plan.upper()
    return suite
