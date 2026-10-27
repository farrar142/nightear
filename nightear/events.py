"""이벤트 저장소와 조회 API.

이벤트는 메타데이터(시작·끝 시각, 최대 dB, 기준선)만 가진다.
후속 기능(클립 캡처, 수면 모드 표시, 라벨 부착 등)은 `EventStore.subscribe(listener)` 로
이벤트 수명주기(started / updated / ended)를 구독해 붙는다.
listener는 `listener(kind: str, event: dict)` 형태의 동기/비동기 callable이다.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Literal

from fastapi import APIRouter, Query, Request

from .storage import Database

EventListener = Callable[[str, dict[str, Any]], Any]


def _to_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "start_ts": row["start_ts"],
        "end_ts": row["end_ts"],
        "duration_s": row["end_ts"] - row["start_ts"],
        "peak_db": row["peak_db"],
        "baseline_db": row["baseline_db"],
        "truncated": bool(row["truncated"]),
        "ongoing": not row["ended"],
    }


class EventStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._listeners: list[EventListener] = []
        # 재시작 전에 끝나지 못한 이벤트는 마지막으로 저장된 끝 시각으로 종료 처리한다.
        self._db.execute("UPDATE events SET ended = 1 WHERE ended = 0")

    def subscribe(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    async def _notify(self, kind: str, event: dict[str, Any]) -> None:
        for listener in self._listeners:
            result = listener(kind, event)
            if inspect.isawaitable(result):
                await result

    def get(self, event_id: int) -> dict[str, Any] | None:
        rows = self._db.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        return _to_dict(rows[0]) if rows else None

    async def start(self, *, source_id: str, start_ts: float, end_ts: float,
                    peak_db: float, baseline_db: float) -> dict[str, Any]:
        rows = self._db.execute(
            "INSERT INTO events(source_id, start_ts, end_ts, peak_db, baseline_db) "
            "VALUES(?, ?, ?, ?, ?) RETURNING id",
            (source_id, start_ts, end_ts, peak_db, baseline_db),
        )
        event = self.get(rows[0]["id"])
        await self._notify("started", event)
        return event

    async def update(self, event_id: int, *, end_ts: float, peak_db: float) -> dict[str, Any]:
        self._db.execute("UPDATE events SET end_ts = ?, peak_db = ? WHERE id = ?",
                         (end_ts, peak_db, event_id))
        event = self.get(event_id)
        await self._notify("updated", event)
        return event

    async def finish(self, event_id: int, *, end_ts: float, peak_db: float,
                     truncated: bool = False) -> dict[str, Any]:
        self._db.execute(
            "UPDATE events SET end_ts = ?, peak_db = ?, truncated = ?, ended = 1 WHERE id = ?",
            (end_ts, peak_db, int(truncated), event_id),
        )
        event = self.get(event_id)
        await self._notify("ended", event)
        return event

    def list(self, *, sort: str = "time", order: str = "desc", limit: int | None = None,
             since: float | None = None, until: float | None = None) -> list[dict[str, Any]]:
        column = {"time": "start_ts", "size": "peak_db"}[sort]
        direction = "ASC" if order == "asc" else "DESC"
        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("start_ts >= ?")
            params.append(since)
        if until is not None:
            where.append("start_ts < ?")
            params.append(until)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {column} {direction}, id {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_to_dict(r) for r in self._db.execute(sql, params)]


router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("")
def list_events(
    request: Request,
    sort: Literal["time", "size"] = "time",
    order: Literal["asc", "desc"] = "desc",
    limit: int | None = Query(None, ge=1, le=10000),
    since: float | None = None,
    until: float | None = None,
) -> list[dict[str, Any]]:
    return request.app.state.events.list(sort=sort, order=order, limit=limit, since=since, until=until)
