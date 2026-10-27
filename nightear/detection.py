"""기준선 계산과 이벤트 감지. 프레임 처리기로 `app.state.frames` 에 등록해 쓴다.

- 기준선: 최근 N분 프레임 레벨의 중앙값(이벤트·수면 모드와 무관하게 계속 갱신).
- 트리거: 레벨 >= 기준선 + 마진 이고 레벨 >= 절대 하한 인 상태가 최소 지속 시간 이상 이어질 때.
- 병합: 마지막 소음이 끝난 뒤 병합 창 안에 다시 트리거되면 같은 이벤트를 확장한다.
- 최대 길이: 이벤트 길이가 (max_clip_s - pre_roll_s - post_roll_s)에 이르면 잘라서 종료한다.
이벤트 수명주기는 EventStore가 구독자(실시간 방송, 후속 클립 캡처 등)에게 알린다.
"""
from __future__ import annotations

import bisect
from collections import deque
from typing import Any

from .frames import Frame
from .levels import FRAME_MS

FRAME_S = FRAME_MS / 1000.0
MIN_BASELINE_FRAMES = 10  # 이만큼(1초) 쌓이기 전에는 기준선이 없어 감지하지 않는다
BASELINE_BROADCAST_EVERY = 5  # 프레임 N개마다(0.5초) 기준선·트리거선을 방송한다
EPS = 1e-9


class BaselineWindow:
    """최근 구간 프레임 레벨의 중앙값을 정렬 리스트로 유지한다."""

    def __init__(self) -> None:
        self._items: deque[tuple[float, float]] = deque()
        self._sorted: list[float] = []

    def __len__(self) -> int:
        return len(self._items)

    def add(self, ts: float, db: float) -> None:
        self._items.append((ts, db))
        bisect.insort(self._sorted, db)

    def trim(self, oldest_ts: float) -> None:
        while self._items and self._items[0][0] < oldest_ts:
            _, db = self._items.popleft()
            del self._sorted[bisect.bisect_left(self._sorted, db)]

    def median(self) -> float | None:
        n = len(self._sorted)
        if not n:
            return None
        mid = n // 2
        if n % 2:
            return self._sorted[mid]
        return (self._sorted[mid - 1] + self._sorted[mid]) / 2.0


class EventDetector:
    def __init__(self, settings, store, hub) -> None:
        self._settings = settings
        self._store = store
        self._hub = hub
        self._reset(None)

    def _reset(self, source_id: str | None) -> None:
        self._source_id = source_id
        self._window = BaselineWindow()
        self._frames = 0
        self._run_n = 0
        self._run_start = 0.0
        self._run_peak = 0.0
        self._run_baseline = 0.0
        self._run_joined = False
        self._event: dict[str, Any] | None = None  # 진행 중인 이벤트(메모리 상태)

    async def __call__(self, frame: Frame) -> None:
        cfg = self._settings.get_all()
        if frame.source_id != self._source_id:
            self._reset(frame.source_id)
        ts, db = frame.ts, frame.db
        margin = cfg["trigger_margin_db"]
        floor = cfg["absolute_floor_db"]

        self._window.trim(ts - cfg["baseline_window_minutes"] * 60.0)
        baseline = self._window.median() if len(self._window) >= MIN_BASELINE_FRAMES else None
        self._window.add(ts, db)
        self._frames += 1

        if baseline is None:
            return
        if self._frames % BASELINE_BROADCAST_EVERY == 0:
            await self._hub.broadcast({
                "type": "baseline",
                "source_id": frame.source_id,
                "ts": ts,
                "baseline_db": baseline,
                "trigger_db": baseline + margin,
                "floor_db": floor,
            })

        over = db >= baseline + margin and db >= floor
        if over:
            if self._run_n == 0:
                self._run_start, self._run_peak, self._run_baseline = ts, db, baseline
                self._run_joined = False
            self._run_n += 1
            self._run_peak = max(self._run_peak, db)
        else:
            self._run_n = 0
        qualified = over and self._run_n * FRAME_S >= cfg["trigger_min_duration_s"] - EPS
        frame_end = ts + FRAME_S
        ev = self._event

        if ev is None:
            if qualified:
                created = await self._store.start(
                    source_id=frame.source_id, start_ts=self._run_start, end_ts=frame_end,
                    peak_db=self._run_peak, baseline_db=self._run_baseline)
                self._event = {"id": created["id"], "start": self._run_start,
                               "end": frame_end, "peak": self._run_peak}
                self._run_joined = True
        elif qualified:
            ev["end"] = frame_end
            ev["peak"] = max(ev["peak"], self._run_peak)
            if not self._run_joined:  # 병합 창 안의 재트리거: 한 번만 확장 알림
                self._run_joined = True
                await self._store.update(ev["id"], end_ts=ev["end"], peak_db=ev["peak"])
        elif not over and ts - ev["end"] >= cfg["merge_window_s"] - EPS:
            await self._store.finish(ev["id"], end_ts=ev["end"], peak_db=ev["peak"])
            self._event = None
            return

        ev = self._event
        if ev is not None:
            max_event_s = max(cfg["max_clip_s"] - cfg["pre_roll_s"] - cfg["post_roll_s"], FRAME_S)
            if ev["end"] - ev["start"] >= max_event_s - EPS:
                ev["end"] = ev["start"] + max_event_s
                await self._store.finish(ev["id"], end_ts=ev["end"], peak_db=ev["peak"], truncated=True)
                self._event = None
                self._run_n = 0  # 이어지는 소음은 새 이벤트로 다시 트리거된다
