"""클립 캡처와 재생 API.

ClipRecorder는 프레임 처리기다. 항상 직전 pre_roll_s 분량의 PCM을 링버퍼로 들고 있다가,
수면 모드 중 시작된 이벤트마다 캡처를 열어 "앞 pre_roll + 이벤트 + 소리가 기준선으로 돌아온 뒤 post_roll"
을 모아 WAV 파일로 저장한다. 이벤트 종료 알림은 병합 창만큼 늦게 오지만 프레임은 이미 우리가 들고 있으므로
이벤트 end_ts 기준으로 자른다. 수면 모드가 아닌 이벤트는 오디오를 만들지 않는다(메타데이터만).

저장 위치: 클립 디렉터리(NIGHTEAR_CLIPS, 기본 DB 옆 `clips/`)의 `event-<id>.wav`.
이벤트 행에는 파일명(clip_path), 길이, 시작 시각(clip_start_ts)을 기록한다.
보관 정리(#9)는 `delete_clip(app.state, event_id)` 로 파일을 지우고 audio_deleted=1 로 표시한다.
클립이 저장되면 실시간 채널로 {"type":"clip","event":{...}} 를 방송한다.
"""
from __future__ import annotations

import logging
import wave
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .frames import Frame
from .levels import FRAME_MS, SAMPLE_RATE

FRAME_S = FRAME_MS / 1000.0
EPS = 1e-6
log = logging.getLogger(__name__)


class _Capture:
    def __init__(self, event_id: int, frames: list[tuple[float, bytes]]) -> None:
        self.event_id = event_id
        self.frames = frames
        self.end_ts: float | None = None  # 이벤트 종료 후에야 알 수 있다


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    tmp.replace(path)


class ClipRecorder:
    def __init__(self, settings, store, hub, clips_dir: Path) -> None:
        self._settings = settings
        self._store = store
        self._hub = hub
        self.clips_dir = Path(clips_dir)
        self._buffer: deque[tuple[float, bytes]] = deque()  # 링버퍼: (프레임 시작 ts, pcm)
        self._captures: dict[int, _Capture] = {}
        self._source_id: str | None = None
        self._last_end = 0.0  # 마지막으로 본 프레임의 끝 시각
        self._clip_listeners: list = []
        store.subscribe(self.on_event)

    # ---- 프레임 ----
    async def __call__(self, frame: Frame) -> None:
        if frame.source_id != self._source_id:
            await self._finalize_all()
            self._buffer.clear()
            self._source_id = frame.source_id
        cfg = self._settings.get_all()
        for cap in self._captures.values():
            cap.frames.append((frame.ts, frame.pcm))
        self._buffer.append((frame.ts, frame.pcm))
        self._last_end = frame.ts + FRAME_S
        keep_from = frame.ts - cfg["pre_roll_s"] - cfg["trigger_min_duration_s"] - 1.0
        while self._buffer and self._buffer[0][0] < keep_from:
            self._buffer.popleft()
        await self._flush_ready()

    async def source_lost(self) -> None:
        """소스가 끊겼다: 가진 만큼으로 클립을 마무리한다."""
        await self._finalize_all()
        self._buffer.clear()
        self._source_id = None

    # ---- 이벤트 ----
    async def on_event(self, kind: str, event: dict[str, Any]) -> None:
        if kind == "started":
            if not event.get("sleep_mode"):
                return  # 깨어 있는 동안의 소리는 오디오를 남기지 않는다
            from_ts = event["start_ts"] - self._settings.get("pre_roll_s") - EPS
            frames = [(ts, pcm) for ts, pcm in self._buffer if ts >= from_ts]
            self._captures[event["id"]] = _Capture(event["id"], frames)
        elif kind == "ended":
            cap = self._captures.get(event["id"])
            if cap is not None:
                cap.end_ts = event["end_ts"]
                await self._flush_ready()

    # ---- 마무리 ----
    async def _flush_ready(self) -> None:
        cfg = self._settings.get_all()
        max_frames = int(cfg["max_clip_s"] / FRAME_S + EPS)
        for cap in list(self._captures.values()):
            if cap.end_ts is not None and self._last_end >= cap.end_ts + cfg["post_roll_s"] - EPS:
                await self._finalize(cap, cap.end_ts + cfg["post_roll_s"] + EPS)
            elif len(cap.frames) >= max_frames:  # 안전장치: 어떤 경우에도 최대 길이를 넘기지 않는다
                await self._finalize(cap, None)

    async def _finalize_all(self) -> None:
        for cap in list(self._captures.values()):
            await self._finalize(cap, None)

    async def _finalize(self, cap: _Capture, cutoff_ts: float | None) -> None:
        self._captures.pop(cap.event_id, None)
        frames = cap.frames
        if cutoff_ts is not None:
            frames = [(ts, p) for ts, p in frames if ts + FRAME_S <= cutoff_ts + EPS]
        frames = frames[: int(self._settings.get("max_clip_s") / FRAME_S + EPS)]
        if not frames:
            return
        filename = f"event-{cap.event_id}.wav"
        try:
            write_wav(self.clips_dir / filename, b"".join(p for _, p in frames))
        except Exception:
            log.exception("클립 저장 실패(이벤트 %s)", cap.event_id)
            return
        event = self._store.set_clip(cap.event_id, path=filename,
                                     duration_s=len(frames) * FRAME_S, start_ts=frames[0][0])
        await self._hub.broadcast({"type": "clip", "event": event})
        for listener in self._clip_listeners:
            try:
                res = listener(event)
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                log.exception("클립 구독자 오류(무시하고 계속)")

    def on_clip_saved(self, listener) -> None:
        """클립 저장 뒤 호출될 구독자(예: 라벨링 큐)를 등록한다. listener(event_dict)."""
        self._clip_listeners.append(listener)


def delete_clip(state, event_id: int) -> bool:
    """클립 파일을 지우고 이벤트에 오디오 삭제를 표시한다(메타데이터는 유지). 보관 정리(#9)용 seam."""
    rows = state.db.execute("SELECT clip_path FROM events WHERE id = ?", (event_id,))
    if not rows or not rows[0]["clip_path"]:
        return False
    try:
        (state.clips_dir / rows[0]["clip_path"]).unlink(missing_ok=True)
    except OSError:
        log.exception("클립 파일 삭제 실패(이벤트 %s)", event_id)
        return False
    state.events.clear_clip(event_id)
    return True


router = APIRouter(prefix="/api/events", tags=["clips"])


@router.get("/{event_id}/clip")
def get_clip(request: Request, event_id: int, download: bool = False):
    """클립 오디오(WAV). Range 요청을 지원해 브라우저가 스트리밍·탐색할 수 있다."""
    state = request.app.state
    rows = state.db.execute("SELECT clip_path FROM events WHERE id = ?", (event_id,))
    if not rows:
        raise HTTPException(404, "이벤트가 없다")
    if not rows[0]["clip_path"]:
        raise HTTPException(404, "클립이 없다")
    path = state.clips_dir / rows[0]["clip_path"]
    if not path.is_file():
        raise HTTPException(404, "클립 파일이 없다")
    return FileResponse(path, media_type="audio/wav", filename=f"nightear-event-{event_id}.wav",
                        content_disposition_type="attachment" if download else "inline")
