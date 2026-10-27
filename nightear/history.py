"""레벨 기록: 100ms 프레임 dB를 1초 해상도로 다운샘플해 영구 저장한다.

집계 정책(초당 2개 값)
- max_db : 그 초에 속한 프레임 dB의 최댓값. 짧은 소음 피크를 놓치지 않으려는 것이 타임라인의 목적이라 주 값이다.
- mean_db: 그 초 프레임 dB의 산술평균(dB 영역). 배경 소음 수준의 지표.
초 경계는 프레임 ts의 floor(epoch 초). 한 초가 소스 전환 등으로 둘로 쪼개져도 같은 키로 덮어쓴다.

쓰기: 완성된 초는 메모리에 모았다가 FLUSH_ROWS개마다 한 번의 트랜잭션으로 쓴다(프레임마다 INSERT 안 함).
조회(`range`) 직전에는 진행 중인 초까지 포함해 flush 하므로 방금 들어온 값도 읽힌다.
소스 소실·종료 시에도 flush 한다. 비정상 종료 시 최대 FLUSH_ROWS초가 유실될 수 있다.
"""
from __future__ import annotations

import math
import threading
from typing import Any

from . import storage
from .frames import Frame

storage.SCHEMA_STATEMENTS.append(
    """
    CREATE TABLE IF NOT EXISTS level_history (
        ts INTEGER PRIMARY KEY,
        max_db REAL NOT NULL,
        mean_db REAL NOT NULL
    )
    """
)

FLUSH_ROWS = 30


class LevelHistory:
    """프레임 처리기. `app.state.frames.register(history)` 로 등록한다."""

    def __init__(self, db: Any) -> None:
        self._db = db
        # 프레임 처리기(이벤트 루프)와 조회(스레드풀의 동기 엔드포인트)가 같은 상태를 만지므로 락으로 보호한다.
        self._lock = threading.RLock()
        self._pending: dict[int, tuple[float, float]] = {}  # 완성된 초: ts -> (max, mean)
        self._cur: int | None = None
        self._max = 0.0
        self._sum = 0.0
        self._n = 0

    def __call__(self, frame: Frame) -> None:
        with self._lock:
            self._add(frame)

    def _add(self, frame: Frame) -> None:
        sec = math.floor(frame.ts)
        if sec != self._cur:
            self._close_bucket()
            self._cur, self._max, self._sum, self._n = sec, frame.db, 0.0, 0
        self._max = max(self._max, frame.db)
        self._sum += frame.db
        self._n += 1
        if len(self._pending) >= FLUSH_ROWS:
            self._write(self._pending)
            self._pending = {}

    def source_lost(self) -> None:
        with self._lock:
            self._close_bucket()
            self.flush()

    def _close_bucket(self) -> None:
        if self._cur is not None and self._n:
            self._pending[self._cur] = (self._max, self._sum / self._n)
        self._cur, self._n = None, 0

    def _write(self, rows: dict[int, tuple[float, float]]) -> None:
        if not rows:
            return
        self._db.executemany(
            "INSERT OR REPLACE INTO level_history(ts, max_db, mean_db) VALUES(?, ?, ?)",
            [(ts, mx, mean) for ts, (mx, mean) in rows.items()])

    def flush(self) -> None:
        """완성된 초를 모두 쓴다(진행 중인 초는 제외)."""
        with self._lock:
            self._write(self._pending)
            self._pending = {}

    def flush_all(self) -> None:
        """진행 중인 초의 현재 집계까지 쓴다. 이후 프레임이 오면 같은 키를 덮어쓴다."""
        with self._lock:
            rows = dict(self._pending)
            if self._cur is not None and self._n:
                rows[self._cur] = (self._max, self._sum / self._n)
            self._write(rows)
            self._pending = {}

    def range(self, start_ts: float, end_ts: float) -> list[tuple[int, float, float]]:
        """[start_ts, end_ts) 의 (ts, max_db, mean_db) 목록(시간순)."""
        self.flush_all()
        rows = self._db.execute(
            "SELECT ts, max_db, mean_db FROM level_history WHERE ts >= ? AND ts < ? ORDER BY ts",
            (math.ceil(start_ts), math.ceil(end_ts)))
        return [(r["ts"], r["max_db"], r["mean_db"]) for r in rows]

    def summary(self, start_ts: float, end_ts: float) -> tuple[int, float | None]:
        """[start_ts, end_ts) 의 (기록된 초 수, 최대 max_db). 행을 가져오지 않고 SQL 집계만 쓴다."""
        self.flush_all()
        r = self._db.execute(
            "SELECT COUNT(*) AS n, MAX(max_db) AS m FROM level_history WHERE ts >= ? AND ts < ?",
            (math.ceil(start_ts), math.ceil(end_ts)))[0]
        return r["n"], r["m"]

    def days_with_data(self) -> list[int]:
        """데이터가 있는 epoch 일(=ts//86400, UTC) 목록. 밤 목록 후보 계산용(PK 인덱스 스캔)."""
        self.flush_all()
        return [r["d"] for r in self._db.execute("SELECT DISTINCT ts / 86400 AS d FROM level_history")]
