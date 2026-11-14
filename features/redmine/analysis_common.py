"""Shared helpers for the RedmineAgent analysis mixin modules.

Constants and helpers here are imported by several ``analysis_*.py``
mixin modules; module-specific definitions stay in their owning modules.
"""


AI_MODEL_TIMEOUT = 120          # seconds for AI model HTTP request
AI_MODEL_MAX_TOKENS = 2400      # max tokens for AI model response
MAX_FAILURE_LINES = 30          # max error lines to extract


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"
