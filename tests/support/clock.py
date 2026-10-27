"""테스트용 가짜 시계. advance()로 시간을 실제로 기다리지 않고 진행시킨다."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        # 기본값: 서울 기준 2025-01-15 23:00 (밤 시간대 안)
        self._now = start or datetime(2025, 1, 15, 23, 0, 0, tzinfo=KST)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0.0, *, minutes: float = 0.0, days: float = 0.0) -> None:
        self._now += timedelta(seconds=seconds, minutes=minutes, days=days)

    def set(self, value: datetime) -> None:
        self._now = value if value.tzinfo else value.replace(tzinfo=KST)
