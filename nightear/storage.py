"""SQLite 저장소. 기능별 모듈이 자신의 스키마를 SCHEMA_STATEMENTS에 등록해 쓴다."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

# 각 기능이 필요한 테이블을 여기에 CREATE TABLE IF NOT EXISTS 문으로 추가한다.
SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        start_ts REAL NOT NULL,
        end_ts REAL NOT NULL,
        peak_db REAL NOT NULL,
        baseline_db REAL NOT NULL,
        truncated INTEGER NOT NULL DEFAULT 0,
        ended INTEGER NOT NULL DEFAULT 0,
        sleep_mode INTEGER NOT NULL DEFAULT 0
    )
    """,
]

# (테이블, 컬럼, 정의): CREATE TABLE에 나중에 추가된 컬럼. 기존 DB에 ALTER로 보강한다.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("events", "sleep_mode", "INTEGER NOT NULL DEFAULT 0"),
]


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            for stmt in SCHEMA_STATEMENTS:
                self._conn.execute(stmt)
            self._conn.commit()
            self._migrate()

    def _migrate(self) -> None:
        """이미 만들어진 DB에 뒤늦게 추가된 컬럼을 보강한다."""
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self._conn.commit()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    def close(self) -> None:
        with self._lock:
            self._conn.close()
