"""패턴 통계: 기간 안 이벤트의 시간대별·요일별·라벨별 빈도.

이벤트 행만 집계한다(클립 파일·audio_deleted 여부와 무관). 그래서 오디오가 삭제된 오래된 이벤트도 포함된다.
시간대·요일은 설정의 timezone 기준이다. 요일은 월요일=0 ... 일요일=6.
라벨별 건수는 이벤트의 1순위 라벨 기준이다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/stats", tags=["stats"])


def compute_stats(db, timezone: str, since: float | None = None, until: float | None = None) -> dict[str, Any]:
    tz = ZoneInfo(timezone)
    where, params = [], []
    if since is not None:
        where.append("e.start_ts >= ?")
        params.append(since)
    if until is not None:
        where.append("e.start_ts < ?")
        params.append(until)
    sql = ("SELECT e.start_ts AS start_ts, l.label AS label FROM events e "
           "LEFT JOIN event_labels l ON l.event_id = e.id AND l.rank = 0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    by_hour = [0] * 24
    by_weekday = [0] * 7
    heatmap = [[0] * 24 for _ in range(7)]
    labels: dict[str, int] = {}
    unlabeled = 0
    rows = db.execute(sql, params)
    for r in rows:
        local = datetime.fromtimestamp(r["start_ts"], tz)
        by_hour[local.hour] += 1
        by_weekday[local.weekday()] += 1
        heatmap[local.weekday()][local.hour] += 1
        if r["label"] is None:
            unlabeled += 1
        else:
            labels[r["label"]] = labels.get(r["label"], 0) + 1
    return {
        "since": since,
        "until": until,
        "timezone": timezone,
        "total": len(rows),
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "heatmap": heatmap,  # [요일 0..6][시 0..23]
        "by_label": [{"label": k, "count": v} for k, v in sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))],
        "unlabeled": unlabeled,  # 라벨이 없는(클립 없음·라벨링 중·실패) 이벤트 수
    }


@router.get("")
def stats(request: Request, since: float | None = None, until: float | None = None) -> dict[str, Any]:
    s = request.app.state
    return compute_stats(s.db, s.settings.get("timezone"), since, until)
