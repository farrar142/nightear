"""「자는 동안」 추정: 수면 모드 구간 안에서 라벨 흐름으로 실제로 잠든 구간을 고른다.

수면 모드는 「밤 시간대 + PC 를 20분 안 썼다」라서, 휴대폰·영상을 보며 깨어 있는 시간도 들어간다.
그래서 통계·아침 요약·라벨 분포는 수면 모드 전체가 아니라 이 추정 구간(자는 동안)으로 센다.
클립 저장·보관은 수면 모드 기준 그대로다(잃는 데이터가 없다).

규칙(수면 모드 구간마다 따로, 구간이 시작될 때는 깨어 있다고 본다: PC 를 썼으니까)
- 깨어 있음 → 잠듦: 1순위가 숨소리(BREATH_LABELS)인 클립 t 에서 [t, t+WINDOW_S) 안에
  숨소리 클립이 MIN_CLIPS 개 이상이고 깸 소리 클립보다 많으면 t 에 잠든 것으로 본다.
- 잠듦 → 깨어 있음: 1순위가 깸 소리(AWAKE_LABELS: 말소리·키보드)인 클립에서 같은 조건(대칭).
- 한 밤에 잠든 구간을 하나도 못 찾으면(숨소리가 거의 없는 밤) 「추정 불가」로 표시하고
  수면 모드 구간 전체를 자는 동안으로 본다(이전 기준).
"""
from __future__ import annotations

from bisect import bisect_left
from typing import Any, Iterable

BREATH_LABELS = frozenset({"Snoring", "Breathing", "Snort"})
AWAKE_LABELS = frozenset({"Speech", "Conversation", "Narration, monologue", "Babbling",
                          "Typing", "Computer keyboard"})
WINDOW_S = 20 * 60
MIN_CLIPS = 5


def _kind(label: str | None) -> str | None:
    if label in BREATH_LABELS:
        return "breath"
    if label in AWAKE_LABELS:
        return "awake"
    return None


def _segment(clips: list[tuple[float, str]], start: float, end: float) -> list[tuple[float, float]]:
    """수면 모드 구간 하나 [start, end) 안의 잠든 구간들. clips 는 그 구간 안의 (시각, 종류), 시각순."""
    times = [t for t, _ in clips]
    out: list[tuple[float, float]] = []
    asleep_from: float | None = None
    for i, (t, kind) in enumerate(clips):
        want = "awake" if asleep_from is not None else "breath"
        if kind != want:
            continue
        window = clips[i:bisect_left(times, t + WINDOW_S)]
        n = sum(1 for _, k in window if k == want)
        other = sum(1 for _, k in window if k is not None and k != want)
        if n < MIN_CLIPS or n <= other:
            continue
        if asleep_from is None:
            asleep_from = t
        else:
            out.append((asleep_from, t))
            asleep_from = None
    if asleep_from is not None:
        out.append((asleep_from, end))
    return out


def estimate(sleep_intervals: Iterable[tuple[float, float]],
             labeled: Iterable[tuple[float, str | None]]) -> dict[str, Any]:
    """수면 모드 구간들과 (이벤트 시작 시각, 1순위 라벨) 목록 -> 자는 동안 추정.

    {"estimated": bool, "asleep_ts": 처음 잠든 시각 | None, "intervals": [{"start_ts","end_ts"}]}
    """
    sleep = sorted(sleep_intervals)
    clips = sorted((t, _kind(label)) for t, label in labeled)
    found: list[tuple[float, float]] = []
    for s, e in sleep:
        found += _segment([c for c in clips if s <= c[0] < e], s, e)
    estimated = bool(found)
    intervals = found if estimated else sleep
    return {
        "estimated": estimated,
        "asleep_ts": found[0][0] if estimated else None,
        "intervals": [{"start_ts": s, "end_ts": e} for s, e in intervals],
    }


def contains(intervals: list[dict[str, float]], ts: float) -> bool:
    return any(iv["start_ts"] <= ts < iv["end_ts"] for iv in intervals)
