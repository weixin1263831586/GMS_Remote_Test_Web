"""Public conversation payloads omit operational secrets."""

from dataclasses import asdict, is_dataclass
from typing import Any


def jsonable(value: Any) -> Any:
    """Return a JSON-serializable copy of Agent session data."""
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump())
    return str(value)


def session_payload(session: dict[str, Any]) -> dict[str, Any]:
    def public(value):
        if isinstance(value, dict):
            return {key: ("***" if item else "") if key in {
                "password", "wifi_password", "token", "api_token", "device_password",
            } else public(item) for key, item in value.items()}
        if isinstance(value, list):
            return [public(item) for item in value]
        return value

    return public(jsonable(session))


