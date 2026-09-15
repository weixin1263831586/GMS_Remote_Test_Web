"""SSH dependency failures shared by the assets routes."""

from fastapi.responses import JSONResponse

from foundation.error_model import ApiError


def ssh_connection_failed_response() -> JSONResponse:
    """Return an actionable, semantic failure for an unavailable SSH host."""
    return ApiError.upstream_failure(
        "SSH connection failed",
        service="ssh",
        next_actions=[
            {"action": "verify host sshd", "command": "ping <host>"},
            {"action": "retry after fixing SSH access"},
        ],
    ).to_response()
