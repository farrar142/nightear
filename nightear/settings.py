"""설정 저장소(기본값 + 조회/수정 API). 값은 SQLite에 JSON으로 저장해 재시작 후에도 유지된다."""
from __future__ import annotations

import json
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, HTTPException, Request

from .storage import Database

# 이후 티켓(감지·수면 모드·보관·알림)이 읽는 설정의 기본값. 타입은 기본값의 타입으로 검증한다.
DEFAULTS: dict[str, Any] = {
    "timezone": "Asia/Seoul",
    "night_start_hour": 22,
    "night_end_hour": 12,
    "idle_threshold_minutes": 20,
    "baseline_window_minutes": 5.0,
    "trigger_margin_db": 10.0,
    "trigger_min_duration_s": 0.2,
    "absolute_floor_db": -60.0,
    "merge_window_s": 10.0,
    "pre_roll_s": 10.0,
    "post_roll_s": 5.0,
    "max_clip_s": 120.0,
    "retention_days": 30,
    "discord_webhook_url": "",
}

# 키별 (최솟값, 최댓값, 최솟값 포함 여부). None은 제한 없음.
RANGES: dict[str, tuple[float | None, float | None, bool]] = {
    "night_start_hour": (0, 23, True),
    "night_end_hour": (0, 23, True),
    "idle_threshold_minutes": (0, None, False),
    "baseline_window_minutes": (0, None, False),
    "trigger_margin_db": (0, None, True),
    "trigger_min_duration_s": (0, None, True),
    "absolute_floor_db": (-120, 0, True),
    "merge_window_s": (0, None, True),
    "pre_roll_s": (0, None, True),
    "post_roll_s": (0, None, True),
    "max_clip_s": (0, None, False),
    "retention_days": (0, None, False),
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
        if key == "timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError, OSError) as e:
                raise SettingsError(f"timezone이 올바른 IANA 이름이 아니다: {value}") from e
        return value
    # 숫자 설정: bool은 int의 하위 타입이므로 명시적으로 거부
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{key}는 숫자여야 한다")
    if isinstance(value, float) and not math.isfinite(value):
        raise SettingsError(f"{key}는 유한한 숫자여야 한다")
    if isinstance(default, int):  # 정수 설정은 정수값만 허용(2.0은 2로 받는다)
        if isinstance(value, float):
            if not value.is_integer():
                raise SettingsError(f"{key}는 정수여야 한다")
            value = int(value)
    lo, hi, lo_inclusive = RANGES.get(key, (None, None, True))
    if lo is not None and (value < lo or (value == lo and not lo_inclusive)):
        raise SettingsError(f"{key}는 {lo}{' 이상' if lo_inclusive else ' 초과'}이어야 한다")
    if hi is not None and value > hi:
        raise SettingsError(f"{key}는 {hi} 이하여야 한다")
    return value


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[str, Any] | None = None  # 프레임마다 읽히므로 메모리에 캐시하고 update 때 갱신한다

    def _load(self) -> dict[str, Any]:
        result = dict(DEFAULTS)
        for row in self._db.execute("SELECT key, value FROM settings"):
            if row["key"] in DEFAULTS:
                result[row["key"]] = json.loads(row["value"])
        return result

    def get_all(self) -> dict[str, Any]:
        if self._cache is None:
            self._cache = self._load()
        return dict(self._cache)

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
        self._cache = self._load()
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
