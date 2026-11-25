"""Daily scan configuration and timezone-aware scheduling."""

from datetime import datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from features.email import normalize_email_addresses


UTC = timezone.utc
CATEGORIES = {
    'requirements': 'GMS要求', 'packages': 'GMS包更新',
    'mainline': 'Mainline包更新', 'artifacts': '测试套件更新',
    'bulletins': 'Critical_Patch', 'exemptions': 'Mainline包豁免项',
    'security_patches': '安全补丁（CVE）',
}


def utc_now() -> datetime:
    return datetime.now(UTC)


class RadarConfig(BaseModel):
    enabled: bool = False
    scan_time: str = Field(default='04:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    timezone: str = Field(default='Asia/Shanghai', max_length=100)
    recipients: list[str] = Field(default_factory=list, max_length=50)
    notify: str = Field(default='changes', pattern='^(always|changes)$')
    dashboard_url: HttpUrl | None = None
    tasks: list[Literal['requirements', 'packages', 'mainline', 'artifacts', 'bulletins', 'exemptions', 'security_patches']] = Field(
        default_factory=lambda: list(CATEGORIES), min_length=1, max_length=7,
    )

    @field_validator('tasks')
    @classmethod
    def unique_tasks(cls, value):
        return [key for key in CATEGORIES if key in value]

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError('请选择有效时区，例如 Asia/Shanghai') from exc
        return value

    @field_validator('recipients')
    @classmethod
    def valid_recipients(cls, value):
        addresses = normalize_email_addresses(value)
        if addresses is None or any(len(item) > 254 for item in addresses):
            raise ValueError('收件人地址格式不正确')
        addresses = list(dict.fromkeys(addresses))
        if len(addresses) > 50:
            raise ValueError('最多设置 50 个收件人')
        return addresses

    @model_validator(mode='after')
    def schedule_requires_recipients(self):
        if self.enabled and not self.recipients:
            raise ValueError('开启每日扫描前请填写收件人')
        return self


def next_scan(config: dict, now: datetime) -> str | None:
    if not config['enabled']:
        return None
    zone = ZoneInfo(config['timezone'])
    now_utc = now.astimezone(UTC)
    local_date = now_utc.astimezone(zone).date()
    hour, minute = map(int, config['scan_time'].split(':'))
    # Compare actual instants, not datetimes with the same ZoneInfo object:
    # wall-time comparison silently ignores fold during a DST fallback.
    for days_ahead in range(3):
        day = local_date + timedelta(days=days_ahead)
        wall_time = datetime.combine(day, time(hour, minute))
        candidates = [
            wall_time.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            for fold in (0, 1)
        ]
        valid = [
            instant for instant in candidates
            if instant.astimezone(zone).replace(tzinfo=None) == wall_time
        ]
        if valid:
            # A repeated hour appears twice; choose its first occurrence so
            # a daily schedule is never enqueued twice on the same local date.
            scheduled = min(valid)
        else:
            # A spring-forward gap has no such wall time. Resolve it to the
            # first forward-mapped time (e.g. 02:30 -> 03:30).
            future = [
                instant for instant in candidates
                if instant.astimezone(zone).replace(tzinfo=None) > wall_time
            ]
            scheduled = min(future) if future else max(candidates)
        if scheduled > now_utc:
            return scheduled.isoformat()
    raise RuntimeError('无法计算下一个每日执行时间')
