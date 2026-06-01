"""Shared enum definitions for the FastAPI application."""

from enum import Enum


class VerifiedBootState(str, Enum):
    """Device verified boot states."""

    LOCKED = 'green'
    UNLOCKED_ORANGE = 'orange'
    UNLOCKED_YELLOW = 'yellow'

    @property
    def is_locked(self) -> bool:
        return self == self.LOCKED

    @property
    def display_text(self) -> str:
        return {
            'green': '已锁定 (GREEN)',
            'orange': '未锁定 (ORANGE)',
            'yellow': '未锁定 (YELLOW)',
        }[self.value]


class LogLevel(str, Enum):
    """Log levels used by long-running operations."""

    INFO = 'info'
    WARNING = 'warning'
    ERROR = 'error'
    SUCCESS = 'success'


class AnalysisMode(str, Enum):
    """Report analysis request modes."""

    UPLOAD = "upload"
    SAVED = "saved"
    AI = "ai"
