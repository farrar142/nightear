"""수면 모드 자동 판단.

수면 모드 = 밤 시간대 AND 유휴 조건.
- 유휴 조건: 소스가 보고한 상태가 idle/locked 이고 유휴 기준(기본 20분) 이상 지속.
  active 신호가 오면 즉시 해제한다.
- 유휴 감지를 지원하지 않는 소스는 밤 시간대 조건만 쓴다.

평가(evaluate) 시점: hello / 활동 메시지 수신 시, GET /api/sleep 조회 시, 그리고 서버의 1초 주기 틱.
시간 경과만으로 바뀌는 전이(유휴 20분 도달, 밤 시간대 종료)는 위 시점에 시계를 읽어 판정한다.

다른 기능을 위한 조회/구독 seam:
- SleepMode.is_sleeping(at=None): 현재 또는 과거 시각에 수면 모드였는지
- SleepMode.intervals(start, end): 기록된 수면 구간 [(start_ts, end_ts|None)]
- SleepMode.on_transition(cb): 진입/해제 시 cb(SleepTransition) 호출(sync 또는 async)
- 실시간 채널 {"type":"sleep", ...} 메시지, HTTP GET /api/sleep, GET /api/sleep/sessions
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from .storage import MIGRATIONS, SCHEMA_STATEMENTS

SCHEMA_STATEMENTS.append(
    """
    CREATE TABLE IF NOT EXISTS sleep_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_ts REAL NOT NULL,
        end_ts REAL,
        last_ts REAL
    )
    """
)
MIGRATIONS.append(("sleep_sessions", "last_ts", "REAL"))
log = logging.getLogger(__name__)

IDLE_STATES = ("idle", "locked")
ACTIVITY_STATES = ("active",) + IDLE_STATES
TICK_SECONDS = 1.0
PERSIST_SECONDS = 10.0  # 수면 중 마지막 평가 시각을 DB에 남기는 간격(재시작 시 구간 종료 시각)


@dataclass(frozen=True)
class SleepTransition:
    sleeping: bool  # True: 수면 모드 진입, False: 해제
    ts: float  # 전이 시각(epoch 초)
    start_ts: float  # 해당 수면 구간의 시작
    end_ts: float | None  # 해제일 때만 값이 있다


def in_night_window(local: datetime, start_hour: int, end_hour: int) -> bool:
    h = local.hour
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour  # 자정을 넘는 창(예: 22~12)


class SleepMode:
    def __init__(self, app_state: Any) -> None:
        self._s = app_state  # clock, db, settings, hub를 쓴다
        self._idle_supported: bool | None = None  # None: 아직 소스 hello가 없었다
        self._activity_state: str | None = None
        self._idle_start: float | None = None  # 유휴가 시작된 시각(epoch)
        self._open_id: int | None = None
        self._open_start: float | None = None
        self._callbacks: list[Callable[[SleepTransition], Awaitable[None] | None]] = []
        self._last_key: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._last_persist = 0.0
        # 재시작 전에 열린 채 남은 구간은 마지막으로 평가된 시각(last_ts)에서 닫는다.
        self._s.db.execute(
            "UPDATE sleep_sessions SET end_ts = COALESCE(last_ts, start_ts) WHERE end_ts IS NULL")

    # ---- 입력 ----
    def on_transition(self, cb: Callable[[SleepTransition], Awaitable[None] | None]) -> None:
        self._callbacks.append(cb)

    async def handle_hello(self, idle_detection_supported: bool) -> None:
        self._idle_supported = idle_detection_supported
        self._activity_state = None
        self._idle_start = None
        await self.evaluate()

    async def handle_activity(self, msg: dict[str, Any]) -> None:
        state = msg.get("state")
        secs = msg.get("idle_seconds", 0.0)
        if state not in ACTIVITY_STATES:
            return
        if isinstance(secs, bool) or not isinstance(secs, (int, float)) or not math.isfinite(secs):
            return  # inf/NaN/비숫자는 잘못된 메시지로 보고 무시한다
        if secs < 0:
            secs = 0.0
        self._activity_state = state
        self._idle_start = None if state == "active" else self._now_ts() - float(secs)
        await self.evaluate()

    # ---- 판정 ----
    def _now_ts(self) -> float:
        return self._s.clock.now().timestamp()

    def _reasons(self, now: float) -> dict[str, Any]:
        cfg = self._s.settings.get_all()
        local = datetime.fromtimestamp(now, ZoneInfo(cfg["timezone"]))
        night = in_night_window(local, cfg["night_start_hour"], cfg["night_end_hour"])
        supported = self._idle_supported
        threshold = cfg["idle_threshold_minutes"] * 60.0
        idle_seconds = None if self._idle_start is None else max(0.0, now - self._idle_start)
        if supported is None:
            idle_ok = False
        elif not supported:
            idle_ok = True  # 유휴 미지원: 밤 시간대만으로 판단
        else:
            idle_ok = self._activity_state in IDLE_STATES and idle_seconds is not None and idle_seconds >= threshold
        return {
            "in_night_window": night,
            "night_start_hour": cfg["night_start_hour"],
            "night_end_hour": cfg["night_end_hour"],
            "timezone": cfg["timezone"],
            "idle_detection_supported": supported,
            "idle_state": self._activity_state,
            "idle_seconds": idle_seconds,
            "idle_threshold_seconds": threshold,
            "idle_satisfied": idle_ok,
        }

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = self._now_ts() if now is None else now
        return {
            "type": "sleep",
            "sleeping": self._open_id is not None,
            "since": self._open_start,
            "reasons": self._reasons(now),
        }

    async def evaluate(self) -> dict[str, Any]:
        async with self._lock:
            now = self._now_ts()
            reasons = self._reasons(now)
            want = reasons["in_night_window"] and reasons["idle_satisfied"]
            transition: SleepTransition | None = None
            if want and self._open_id is None:
                rows = self._s.db.execute(
                    "INSERT INTO sleep_sessions(start_ts, last_ts) VALUES(?, ?) RETURNING id", (now, now))
                self._open_id, self._open_start = rows[0]["id"], now
                self._last_persist = now
                transition = SleepTransition(True, now, now, None)
            elif not want and self._open_id is not None:
                self._s.db.execute("UPDATE sleep_sessions SET end_ts = ? WHERE id = ?", (now, self._open_id))
                transition = SleepTransition(False, now, self._open_start, now)
                self._open_id = self._open_start = None
            elif self._open_id is not None and now - self._last_persist >= PERSIST_SECONDS:
                self._s.db.execute("UPDATE sleep_sessions SET last_ts = ? WHERE id = ?", (now, self._open_id))
                self._last_persist = now
            snap = self.snapshot(now)
            # idle_seconds는 계속 변하므로 방송 여부는 상태·근거의 이산 값으로만 판단한다
            key = {"sleeping": snap["sleeping"], **{k: v for k, v in reasons.items() if k != "idle_seconds"}}
            changed = key != self._last_key
            self._last_key = key
        if changed:
            await self._s.hub.broadcast(snap)
        if transition:
            for cb in self._callbacks:
                try:  # 전이 콜백 하나의 예외가 틱·수집 경로로 번지지 않게 격리한다
                    res = cb(transition)
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    log.exception("수면 전이 콜백 오류(무시하고 계속): %r", cb)
        return snap

    # ---- 조회 seam ----
    def intervals(self, start: float | None = None, end: float | None = None) -> list[tuple[float, float | None]]:
        """[start, end]와 겹치는 수면 구간. 진행 중인 구간의 끝은 None."""
        rows = self._s.db.execute(
            "SELECT start_ts, end_ts FROM sleep_sessions"
            " WHERE (? IS NULL OR start_ts <= ?) AND (? IS NULL OR end_ts IS NULL OR end_ts >= ?)"
            " ORDER BY start_ts", (end, end, start, start))
        return [(r["start_ts"], r["end_ts"]) for r in rows]

    def is_sleeping(self, at: float | datetime | None = None) -> bool:
        """at 없이 부르면 마지막으로 평가된 현재 상태, 있으면 그 시각이 기록된 구간 안인지."""
        if at is None:
            return self._open_id is not None
        ts = at.timestamp() if isinstance(at, datetime) else at
        rows = self._s.db.execute(
            "SELECT 1 FROM sleep_sessions WHERE start_ts <= ? AND (end_ts IS NULL OR ? < end_ts) LIMIT 1", (ts, ts))
        return bool(rows)

    # ---- 주기 틱 ----
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._tick())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await self.evaluate()
            except Exception:
                log.exception("수면 모드 틱 오류(다음 틱에 계속)")


router = APIRouter(prefix="/api/sleep", tags=["sleep"])


@router.get("")
async def read_sleep(request: Request) -> dict[str, Any]:
    return await request.app.state.sleep.evaluate()


@router.get("/sessions")
def read_sessions(request: Request) -> list[dict[str, Any]]:
    return [{"start": s, "end": e} for s, e in request.app.state.sleep.intervals()]
