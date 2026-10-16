"""Public conversation payloads omit operational secrets."""

from dataclasses import asdict, is_dataclass
from typing import Any

from foundation.redaction import redact_sensitive_data


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
    return redact_sensitive_data(jsonable(session))


def display_record(session: dict[str, Any], record: dict[str, Any], private_params=None) -> dict[str, Any]:
    """Sanitize public history without replacing the internal execution plan."""
    safe = session_payload({
        'record': record, 'messages': session.get('messages', []), 'steps': session.get('steps', []),
        'pending_plan': session.get('pending_plan'),
        'context': session.get('context'), 'private_params': private_params,
    })
    session['messages'] = safe['messages']
    session['steps'] = safe['steps']
    if 'context' in session:
        session['context'] = safe['context']
    return safe['record']


