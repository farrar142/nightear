"""이벤트 저장소와 조회 API.

이벤트는 메타데이터(시작·끝 시각, 최대 dB, 기준선)만 가진다.
후속 기능(클립 캡처, 수면 모드 표시, 라벨 부착 등)은 `EventStore.subscribe(listener)` 로
이벤트 수명주기(started / updated / ended)를 구독해 붙는다.
listener는 `listener(kind: str, event: dict)` 형태의 동기/비동기 callable이다.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Literal

from fastapi import APIRouter, Query, Request

from .storage import Database

log = logging.getLogger(__name__)
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
        "sleep_mode": bool(row["sleep_mode"]),  # 이벤트 시작 시점에 수면 모드였는가
        "ongoing": not row["ended"],
        "has_clip": bool(row["clip_path"]),
        "clip_duration_s": row["clip_duration_s"] if row["clip_path"] else None,
        "clip_start_ts": row["clip_start_ts"] if row["clip_path"] else None,
        "audio_deleted": bool(row["audio_deleted"]),  # 보관 기간이 지나 오디오만 지워진 이벤트
        "starred": bool(row["starred"]),  # 별표 이벤트의 클립은 보관 정리에서 제외된다
        "label_status": row["label_status"],  # None(클립 없음) / pending / done / failed
        "labels": [],  # [{"label","score"}] 확신도 내림차순. EventStore가 채운다
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
            try:  # 구독자 하나의 예외가 감지·수집을 막지 않게 격리한다
                result = listener(kind, event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("이벤트 구독자 오류(무시하고 계속): %r", listener)

    def _attach_labels(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not events:
            return events
        ids = [e["id"] for e in events]
        marks = ",".join("?" * len(ids))
        by_event: dict[int, list[dict[str, Any]]] = {}
        for r in self._db.execute(
                f"SELECT event_id, label, score FROM event_labels WHERE event_id IN ({marks}) "
                "ORDER BY event_id, rank", ids):
            by_event.setdefault(r["event_id"], []).append({"label": r["label"], "score": r["score"]})
        for e in events:
            e["labels"] = by_event.get(e["id"], [])
        return events

    def get(self, event_id: int) -> dict[str, Any] | None:
        rows = self._db.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        return self._attach_labels([_to_dict(rows[0])])[0] if rows else None

    # ---- 라벨링 ----
    def set_label_status(self, event_id: int, status: str) -> dict[str, Any]:
        self._db.execute("UPDATE events SET label_status = ? WHERE id = ?", (status, event_id))
        return self.get(event_id)

    def set_labels(self, event_id: int, labels: list[tuple[str, float]]) -> dict[str, Any]:
        """상위 라벨 후보를 (순위 순으로) 저장하고 상태를 done 으로 둔다."""
        self._db.execute("DELETE FROM event_labels WHERE event_id = ?", (event_id,))
        for rank, (label, score) in enumerate(labels):
            self._db.execute("INSERT INTO event_labels(event_id, rank, label, score) VALUES(?, ?, ?, ?)",
                             (event_id, rank, label, score))
        return self.set_label_status(event_id, "done")

    def pending_label_ids(self) -> list[int]:
        return [r["id"] for r in self._db.execute(
            "SELECT id FROM events WHERE label_status = 'pending' AND clip_path IS NOT NULL ORDER BY id")]

    def clip_file(self, event_id: int, clips_dir) -> Any:
        rows = self._db.execute("SELECT clip_path FROM events WHERE id = ?", (event_id,))
        if not rows or not rows[0]["clip_path"]:
            return None
        path = clips_dir / rows[0]["clip_path"]
        return path if path.is_file() else None

    def label_counts(self) -> list[dict[str, Any]]:
        """라벨별 이벤트 수(상위 후보 어디에든 들어 있는 이벤트 기준), 많은 순."""
        return [{"label": r["label"], "count": r["n"]} for r in self._db.execute(
            "SELECT label, COUNT(*) AS n FROM event_labels GROUP BY label ORDER BY n DESC, label")]

    def set_clip(self, event_id: int, *, path: str, duration_s: float, start_ts: float) -> dict[str, Any]:
        self._db.execute("UPDATE events SET clip_path = ?, clip_duration_s = ?, clip_start_ts = ? WHERE id = ?",
                         (path, duration_s, start_ts, event_id))
        return self.get(event_id)

    def set_starred(self, event_id: int, starred: bool) -> dict[str, Any] | None:
        self._db.execute("UPDATE events SET starred = ? WHERE id = ?", (int(starred), event_id))
        return self.get(event_id)

    def clear_clip(self, event_id: int) -> None:
        """클립 파일이 지워졌음을 기록한다. 이벤트 메타데이터(시각·크기·라벨)는 남는다."""
        self._db.execute("UPDATE events SET clip_path = NULL, audio_deleted = 1 WHERE id = ?", (event_id,))

    async def start(self, *, source_id: str, start_ts: float, end_ts: float,
                    peak_db: float, baseline_db: float,
                    sleep_mode: bool = False) -> dict[str, Any]:
        rows = self._db.execute(
            "INSERT INTO events(source_id, start_ts, end_ts, peak_db, baseline_db, sleep_mode) "
            "VALUES(?, ?, ?, ?, ?, ?) RETURNING id",
            (source_id, start_ts, end_ts, peak_db, baseline_db, int(sleep_mode)),
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
             since: float | None = None, until: float | None = None,
             labels: list[str] | None = None) -> list[dict[str, Any]]:
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
        if labels:  # 상위 후보 중 하나라도 일치하는 이벤트
            where.append("id IN (SELECT event_id FROM event_labels WHERE label IN (%s))"
                         % ",".join("?" * len(labels)))
            params.extend(labels)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {column} {direction}, id {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self._attach_labels([_to_dict(r) for r in self._db.execute(sql, params)])


router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("")
def list_events(
    request: Request,
    sort: Literal["time", "size"] = "time",
    order: Literal["asc", "desc"] = "desc",
    limit: int | None = Query(None, ge=1, le=10000),
    since: float | None = None,
    until: float | None = None,
    label: list[str] | None = Query(None),
) -> list[dict[str, Any]]:
    return request.app.state.events.list(sort=sort, order=order, limit=limit, since=since, until=until,
                                         labels=label)
