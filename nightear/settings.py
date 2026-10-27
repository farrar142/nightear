"""설정 저장소(기본값 + 조회/수정 API). 값은 SQLite에 JSON으로 저장해 재시작 후에도 유지된다."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from .storage import Database

# 이후 티켓(감지·수면 모드·보관·알림)이 읽는 설정의 기본값. 타입은 기본값의 타입으로 검증한다.
DEFAULTS: dict[str, Any] = {
    "timezone": "Asia/Seoul",
    "night_start_hour": 22,
    "night_end_hour": 12,
    "idle_threshold_minutes": 20,
    "baseline_window_minutes": 5,
    "trigger_margin_db": 10,
    "trigger_min_duration_s": 0.2,
    "absolute_floor_db": -60,
    "merge_window_s": 10,
    "pre_roll_s": 10,
    "post_roll_s": 5,
    "max_clip_s": 120,
    "retention_days": 30,
    "discord_webhook_url": "",
}


class SettingsError(ValueError):
    pass


def _check(key: str, value: Any) -> Any:
    if key not in DEFAULTS:
        raise SettingsError(f"알 수 없는 설정 키: {key}")
    default = DEFAULTS[key]
    if isinstance(default, str):
        if not isinstance(value, str):
            raise SettingsError(f"{key}는 문자열이어야 한다")
        return value
    # 숫자 설정: bool은 int의 하위 타입이므로 명시적으로 거부
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{key}는 숫자여야 한다")
    return value


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_all(self) -> dict[str, Any]:
        result = dict(DEFAULTS)
        for row in self._db.execute("SELECT key, value FROM settings"):
            if row["key"] in DEFAULTS:
                result[row["key"]] = json.loads(row["value"])
        return result

    def get(self, key: str) -> Any:
        return self.get_all()[key]

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        checked = {k: _check(k, v) for k, v in changes.items()}  # 전부 통과해야 하나라도 저장
        for k, v in checked.items():
            self._db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, json.dumps(v)),
            )
        return self.get_all()


router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def read_settings(request: Request) -> dict[str, Any]:
    return request.app.state.settings.get_all()


@router.put("")
def update_settings(request: Request, changes: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return request.app.state.settings.update(changes)
    except SettingsError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
