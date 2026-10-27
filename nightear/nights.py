"""밤(night) 단위 조회: 밤 목록 API와 밤 상세 API.

정의
- 밤 = 설정의 밤 시간대 창 하나의 인스턴스(설정 시간대 기준). 예: 22:00 → 다음날 12:00.
- 밤의 id = 창이 시작하는 로컬 날짜 "YYYY-MM-DD" (2025-01-15 밤 = 1/15 22:00 ~ 1/16 12:00).
- 창은 현재 설정으로 계산한다(설정을 바꾸면 과거 밤의 경계도 새 설정으로 다시 해석된다).
- 시작·끝 시간이 같으면 하루 종일(24h) 창으로 본다.

다른 기능(#10 아침 요약 등)을 위한 seam
- night_window(night_id, cfg) -> (start_ts, end_ts)
- night_id_for(ts, cfg) -> 그 시각이 속한 밤 id (창 밖이면 None)
- app.state.nights.detail(night_id) / .list()
- 대시보드 경로: /#night=<night_id>
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

MAX_SPAN_DAYS = 400  # 아주 긴 공백이 있어도 후보 밤을 이 이상 만들지 않는다
_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def night_window(night_id: str, cfg: dict[str, Any]) -> tuple[float, float]:
    """밤 id의 [시작, 끝) epoch 초."""
    try:
        day = date.fromisoformat(night_id)
    except ValueError:
        raise ValueError(f"잘못된 밤 id: {night_id!r}") from None
    tz = ZoneInfo(cfg["timezone"])
    s, e = cfg["night_start_hour"], cfg["night_end_hour"]
    start = datetime.combine(day, time(s), tz)
    end_day = day + timedelta(days=1) if e <= s else day
    end = datetime.combine(end_day, time(e), tz)
    return start.timestamp(), end.timestamp()


def night_id_for(ts: float, cfg: dict[str, Any]) -> str | None:
    local = datetime.fromtimestamp(ts, ZoneInfo(cfg["timezone"]))
    s, e = cfg["night_start_hour"], cfg["night_end_hour"]
    if s == e:
        day = local.date() if local.hour >= s else local.date() - timedelta(days=1)
        return day.isoformat()
    if s < e:
        return local.date().isoformat() if s <= local.hour < e else None
    if local.hour >= s:
        return local.date().isoformat()
    if local.hour < e:
        return (local.date() - timedelta(days=1)).isoformat()
    return None


def _overlap(start: float, end: float | None, w0: float, w1: float) -> tuple[float, float] | None:
    """[start,end)를 창 [w0,w1)로 자른다. 겹침이 없거나 길이가 0이면 None. end=None은 창 끝까지."""
    a = max(start, w0)
    b = w1 if end is None else min(end, w1)
    return (a, b) if b > a else None


class NightService:
    def __init__(self, app_state: Any, history: Any) -> None:
        self._s = app_state
        self._history = history

    def _now(self) -> float:
        return self._s.clock.now().timestamp()

    def _cfg(self) -> dict[str, Any]:
        return self._s.settings.get_all()

    def _intervals(self, w0: float, w1: float) -> tuple[list[dict], list[dict]]:
        # 진행 중인 구간의 끝은 "지금"(창이 아직 안 끝났다면)으로 잘라 길이를 센다.
        live_end = min(w1, max(self._now(), w0))
        sleep = []
        for s, e in self._s.sleep.intervals(w0, w1):
            ov = _overlap(s, e, w0, live_end)
            if ov:
                sleep.append({"start_ts": ov[0], "end_ts": ov[1], "ongoing": e is None})
        gaps = []
        for g in self._s.gaps.query(w0, w1):
            ov = _overlap(g["start_ts"], g["end_ts"], w0, live_end)
            if ov:
                gaps.append({"start_ts": ov[0], "end_ts": ov[1], "ongoing": g["ongoing"]})
        return sleep, gaps

    def _summary(self, w0: float, w1: float, sleep: list[dict], gaps: list[dict],
                 level_seconds: int, peak_db: float | None, event_count: int) -> dict[str, Any]:
        return {
            "level_seconds": level_seconds,
            "event_count": event_count,
            "sleep_seconds": sum(x["end_ts"] - x["start_ts"] for x in sleep),
            "gap_count": len(gaps),
            "gap_seconds": sum(x["end_ts"] - x["start_ts"] for x in gaps),
            "peak_db": peak_db,
        }

    def _event_count(self, w0: float, w1: float) -> int:
        return self._s.db.execute(
            "SELECT COUNT(*) AS n FROM events WHERE start_ts >= ? AND start_ts < ?", (w0, w1))[0]["n"]

    def detail(self, night_id: str) -> dict[str, Any]:
        w0, w1 = night_window(night_id, self._cfg())
        sleep, gaps = self._intervals(w0, w1)
        events = self._s.events.list(sort="time", order="asc", since=w0, until=w1)
        level_rows = self._history.range(w0, w1)
        summary = self._summary(w0, w1, sleep, gaps, len(level_rows),
                                max((r[1] for r in level_rows), default=None), len(events))
        return {
            "id": night_id, "start_ts": w0, "end_ts": w1, "summary": summary,
            "sleep_intervals": sleep, "gaps": gaps, "events": events,
            "levels": {"columns": ["ts", "max_db", "mean_db"], "step_s": 1,
                       "rows": [list(r) for r in level_rows]},
        }

    def _candidate_ids(self, cfg: dict[str, Any]) -> set[str]:
        """데이터가 있을 수 있는 밤 id. 레벨·이벤트는 UTC 일 단위 DISTINCT 로 싸게 모은다."""
        tz = ZoneInfo(cfg["timezone"])
        now = self._now()

        def day_of(ts: float) -> date:
            return datetime.fromtimestamp(ts, tz).date()

        days: set[date] = set()
        utc_days = set(self._history.days_with_data())
        utc_days.update(r["d"] for r in self._s.db.execute(
            "SELECT DISTINCT CAST(start_ts / 86400 AS INTEGER) AS d FROM events"))
        for d in utc_days:  # 한 UTC 일은 로컬 날짜 둘에 걸칠 수 있다
            days.add(day_of(d * 86400))
            days.add(day_of(d * 86400 + 86399))

        def add_span(a: float, b: float | None) -> None:
            d0, d1 = day_of(a), day_of(b if b is not None else now)
            n = min((d1 - d0).days, MAX_SPAN_DAYS)
            days.update(d0 + timedelta(days=i) for i in range(max(n, 0) + 1))

        for s, e in self._s.sleep.intervals():
            add_span(s, e)
        for g in self._s.gaps.query():
            add_span(g["start_ts"], g["end_ts"])
        # 창이 자정을 넘으므로 전날 시작 밤도 후보다
        return {d.isoformat() for day in days for d in (day, day - timedelta(days=1))}

    def list(self) -> list[dict[str, Any]]:
        """밤 목록. 요약은 SQL 집계로만 구해 레벨/이벤트 행을 읽지 않는다."""
        cfg = self._cfg()
        nights = []
        for nid in self._candidate_ids(cfg):
            w0, w1 = night_window(nid, cfg)
            sleep, gaps = self._intervals(w0, w1)
            n_levels, peak = self._history.summary(w0, w1)
            n_events = self._event_count(w0, w1)
            if n_levels or n_events or sleep or gaps:
                nights.append({"id": nid, "start_ts": w0, "end_ts": w1,
                               "summary": self._summary(w0, w1, sleep, gaps, n_levels, peak, n_events)})
        nights.sort(key=lambda n: n["id"], reverse=True)
        return nights


router = APIRouter(prefix="/api/nights", tags=["nights"])


@router.get("")
def list_nights(request: Request) -> dict:
    return {"nights": request.app.state.nights.list()}


@router.get("/{night_id}")
def night_detail(night_id: str, request: Request) -> dict:
    if not _ID_RE.match(night_id):
        raise HTTPException(404, "밤 id는 YYYY-MM-DD 형식이어야 한다")
    try:
        return request.app.state.nights.detail(night_id)
    except ValueError:
        raise HTTPException(404, "잘못된 날짜") from None
