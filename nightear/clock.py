"""시계 포트. 시간에 의존하는 모든 코드는 이 포트를 통해서만 현재 시각을 얻는다."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """타임존이 있는(aware) 현재 시각."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
