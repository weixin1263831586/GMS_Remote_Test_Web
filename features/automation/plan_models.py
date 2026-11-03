"""Validated schedules and bounded retry policies for human-owned test plans."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_retries: int = Field(default=0, ge=0, le=5, strict=True)
    delay_seconds: int = Field(default=60, ge=10, le=3600, strict=True)


class PlanSchedule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['manual', 'interval', 'daily', 'weekly'] = 'manual'
    timezone: str = Field(default='Asia/Shanghai', max_length=64)
    time: str = '02:00'
    weekdays: list[int] = Field(default_factory=lambda: [0], max_length=7)
    interval_minutes: int = Field(default=1440, ge=5, le=10080, strict=True)

    @model_validator(mode='after')
    def validate_wall_clock(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError('Unknown schedule timezone') from exc
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', self.time):
            raise ValueError('Schedule time must be HH:MM')
        if any(type(day) is not int or not 0 <= day <= 6 for day in self.weekdays):
            raise ValueError('Weekdays must be integers from 0 (Monday) to 6')
        if self.kind == 'weekly' and not self.weekdays:
            raise ValueError('Select at least one weekday')
        self.weekdays = sorted(set(self.weekdays))
        return self

    def next_after(self, now: datetime) -> str:
        if now.tzinfo is None:
            raise ValueError('Schedule requires a timezone-aware clock')
        if self.kind == 'manual':
            return ''
        if self.kind == 'interval':
            return utc_text(now + timedelta(minutes=self.interval_minutes))
        zone = ZoneInfo(self.timezone)
        local = now.astimezone(zone)
        hour, minute = map(int, self.time.split(':'))
        for offset in range(9):
            day = local.date() + timedelta(days=offset)
            if self.kind == 'weekly' and day.weekday() not in self.weekdays:
                continue
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
            utc_candidate = candidate.astimezone(timezone.utc)
            # Nonexistent spring-forward times are skipped; fall-back uses fold 0 once.
            if utc_candidate.astimezone(zone).replace(tzinfo=None) != candidate.replace(tzinfo=None):
                continue
            if utc_candidate > now:
                return utc_text(utc_candidate)
        raise ValueError('No schedule occurrence found')


class PlanSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = False
    stop_on_failure: bool = True
    schedule: PlanSchedule = Field(default_factory=PlanSchedule)
    version: int = Field(default=0, ge=0)
    labels: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('labels')
    @classmethod
    def valid_labels(cls, values):
        if any(not value.strip() or len(value) > 64 or any(ord(c) < 32 for c in value) for value in values):
            raise ValueError('Labels must be nonempty text of at most 64 characters')
        return sorted(set(value.strip() for value in values))


class TestPlanRequest(PlanSettingsRequest):
    runs: list[dict[str, Any]] = Field(min_length=1, max_length=20)
