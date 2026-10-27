"""프레임 파이프라인: 수집된 100ms 프레임을 (source_id, ts, db, pcm)으로 등록된 처리기들에 전달한다.

이후 기능(기준선·트리거 감지, 링버퍼/클립 등)은 ingest 루프를 고치지 않고
`app.state.frames.register(fn)` 으로 처리기를 등록해 프레임을 소비한다.
처리기는 동기/비동기 callable 모두 가능하며 등록 순서대로 호출된다.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class Frame:
    source_id: str
    ts: float  # 프레임 시작 시각(epoch 초). 샘플 위치 기준이라 프레임마다 정확히 FRAME_MS 간격
    db: float  # 프레임 RMS dBFS
    pcm: bytes  # 16kHz 모노 s16le, FRAME_MS 길이


FrameProcessor = Callable[[Frame], Awaitable[None] | None]


class FramePipeline:
    def __init__(self) -> None:
        self._processors: list[FrameProcessor] = []

    def register(self, processor: FrameProcessor) -> None:
        self._processors.append(processor)

    async def dispatch(self, frame: Frame) -> None:
        for p in self._processors:
            result: Any = p(frame)
            if inspect.isawaitable(result):
                await result


class FrameClock:
    """샘플 위치로 프레임 시각을 계산한다.

    hello 시점의 시계 값을 anchor로 삼고 ts = anchor + n * frame_s.
    벽시계가 샘플 시각보다 max_lead_s 넘게 앞서면(전송 정지 등) 현재 시각으로 재앵커한다.
    샘플 시각이 벽시계보다 앞서는 것(배치 전송)은 그대로 둔다.
    """

    def __init__(self, anchor_ts: float, frame_s: float, max_lead_s: float = 5.0) -> None:
        self._anchor = anchor_ts
        self._n = 0
        self._frame_s = frame_s
        self._max_lead_s = max_lead_s

    def next_ts(self, now_ts: float) -> float:
        ts = self._anchor + self._n * self._frame_s
        if now_ts - ts > self._max_lead_s:
            self._anchor = now_ts
            self._n = 0
            ts = now_ts
        self._n += 1
        return ts
