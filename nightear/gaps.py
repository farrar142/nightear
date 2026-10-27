"""수집 공백(capture gap) 기록.

공백 = 활성 수집 소스가 하나도 없던 구간. 야간 타임라인(#8)이 "소음 없음"과 "녹음 안 됨"을 구분하는 데 쓴다.

정책
- 활성 소스가 끊기면(승격할 보조 소스가 없을 때) 그 시각(clock.now())부터 공백을 연다(end_ts=NULL).
- 활성 소스가 다시 나타나면 열린 공백을 닫는다.
- 백엔드 재시작/크래시: 활성 수집 중에는 하트비트(last_seen)를 HEARTBEAT_INTERVAL_S 간격으로 DB에 남긴다.
  기동 시 열린 공백이 이미 있으면 그대로 이어가고, 없고 last_seen이 있으면 last_seen부터 공백을 연다
  (정상 종료도 같은 경로: 종료 후 다음 기동까지가 공백). 수집 이력이 전혀 없는 새 DB는 공백을 만들지 않는다.
- 활성 소스가 끊겨 공백이 열리면 last_seen은 지운다(이미 공백 중이므로).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from . import storage

storage.SCHEMA_STATEMENTS.extend([
    """
    CREATE TABLE IF NOT EXISTS capture_gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_ts REAL NOT NULL,
        end_ts REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS capture_heartbeat (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_seen REAL NOT NULL
    )
    """,
])

HEARTBEAT_INTERVAL_S = 10.0


class GapStore:
    def __init__(self, db: Any) -> None:
        self._db = db
        self._last_beat: float | None = None
        self._recover()

    def _recover(self) -> None:
        if self._open_gap_id() is not None:
            return
        rows = self._db.execute("SELECT last_seen FROM capture_heartbeat WHERE id = 1")
        if rows:
            self._db.execute("INSERT INTO capture_gaps (start_ts, end_ts) VALUES (?, NULL)", (rows[0]["last_seen"],))
            self._db.execute("DELETE FROM capture_heartbeat")

    def _open_gap_id(self) -> int | None:
        rows = self._db.execute("SELECT id FROM capture_gaps WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1")
        return rows[0]["id"] if rows else None

    def source_lost(self, now_ts: float) -> bool:
        """활성 소스가 사라졌다. 새 공백을 열었으면 True."""
        self._last_beat = None
        self._db.execute("DELETE FROM capture_heartbeat")
        if self._open_gap_id() is not None:
            return False
        self._db.execute("INSERT INTO capture_gaps (start_ts, end_ts) VALUES (?, NULL)", (now_ts,))
        return True

    def source_active(self, now_ts: float) -> bool:
        """활성 소스가 나타났다. 열린 공백을 닫았으면 True."""
        gap_id = self._open_gap_id()
        closed = False
        if gap_id is not None:
            start = self._db.execute("SELECT start_ts FROM capture_gaps WHERE id = ?", (gap_id,))[0]["start_ts"]
            self._db.execute("UPDATE capture_gaps SET end_ts = ? WHERE id = ?", (max(now_ts, start), gap_id))
            closed = True
        self._beat(now_ts)
        return closed

    def heartbeat(self, now_ts: float) -> None:
        if self._last_beat is None or now_ts - self._last_beat >= HEARTBEAT_INTERVAL_S:
            self._beat(now_ts)

    def _beat(self, now_ts: float) -> None:
        self._last_beat = now_ts
        self._db.execute("INSERT OR REPLACE INTO capture_heartbeat (id, last_seen) VALUES (1, ?)", (now_ts,))

    def query(self, start_ts: float | None = None, end_ts: float | None = None) -> list[dict[str, Any]]:
        """[start_ts, end_ts] 와 겹치는 공백(시작순). 진행 중 공백은 end_ts=None, ongoing=True. #8의 조회 seam."""
        rows = self._db.execute(
            "SELECT start_ts, end_ts FROM capture_gaps"
            " WHERE (? IS NULL OR end_ts IS NULL OR end_ts >= ?)"
            " AND (? IS NULL OR start_ts <= ?) ORDER BY start_ts, id",
            (start_ts, start_ts, end_ts, end_ts),
        )
        return [{"start_ts": r["start_ts"], "end_ts": r["end_ts"], "ongoing": r["end_ts"] is None} for r in rows]


router = APIRouter()


@router.get("/api/gaps")
def list_gaps(request: Request,
              from_: float | None = Query(None, alias="from"),
              to: float | None = None) -> dict:
    """from/to: epoch 초(생략 가능). 범위와 겹치는 수집 공백 목록."""
    return {"gaps": request.app.state.gaps.query(from_, to)}
