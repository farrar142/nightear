"""앱 팩토리. 외부 의존(시계, 이후 분류기·알림기)은 인자로 주입한다."""
from __future__ import annotations

import inspect
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import clips, events, labeling, stats, gaps, ingest, nights, realtime, settings, sleep, summary
from .history import LevelHistory
from .clock import Clock, SystemClock
from .detection import EventDetector
from .frames import Frame, FramePipeline
from .ingest import IngestState
from .levels import FRAME_MS
from .realtime import RealtimeHub
from .settings import SettingsStore
from .sleep import SleepMode
from .storage import Database

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger(__name__)


class Lifecycle:
    """앱 수명주기 훅 목록. 주기 작업(보관 정리, 아침 요약 등)은 `app.state.lifecycle.add(start, stop)` 으로 붙인다.

    start/stop은 동기/비동기 callable. 시작은 등록 순, 종료는 역순이며 하나가 실패해도 나머지는 계속한다.
    """

    def __init__(self) -> None:
        self._hooks: list[tuple[Callable | None, Callable | None]] = []

    def add(self, start: Callable | None = None, stop: Callable | None = None) -> None:
        self._hooks.append((start, stop))

    @staticmethod
    async def _call(fn: Callable | None) -> None:
        if fn is None:
            return
        try:
            res = fn()
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("수명주기 훅 오류(무시하고 계속)")

    async def startup(self) -> None:
        for start, _ in self._hooks:
            await self._call(start)

    async def shutdown(self) -> None:
        for _, stop in reversed(self._hooks):
            await self._call(stop)


def create_app(
    *,
    clock: Clock | None = None,
    db_path: str | Path | None = None,
    clips_dir: str | Path | None = None,
    classifier: labeling.SoundClassifier | None = None,
    notifier: summary.Notifier | None = None,
    static_dir: Path | None = STATIC_DIR,
) -> FastAPI:
    db = Database(db_path or os.environ.get("NIGHTEAR_DB", "data/nightear.db"))
    lifecycle = Lifecycle()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await lifecycle.startup()
        try:
            yield
        finally:
            await lifecycle.shutdown()

    app = FastAPI(title="nightear", lifespan=lifespan)
    app.state.clock = clock or SystemClock()
    app.state.db = db
    app.state.lifecycle = lifecycle
    app.state.settings = SettingsStore(db)
    app.state.hub = RealtimeHub()
    app.state.ingest_state = IngestState()
    app.state.gaps = gaps.GapStore(db)
    app.state.frames = FramePipeline()
    app.state.events = events.EventStore(db)
    app.state.clips_dir = Path(clips_dir or os.environ.get("NIGHTEAR_CLIPS")
                               or db.path.parent / "clips")
    app.state.sleep = SleepMode(app.state)
    app.state.history = LevelHistory(db)  # 1초 해상도 레벨 기록(밤 타임라인용)
    app.state.frames.register(app.state.history)
    lifecycle.add(stop=app.state.history.flush_all)
    app.state.nights = nights.NightService(app.state, app.state.history)
    lifecycle.add(app.state.sleep.start, app.state.sleep.stop)
    # 알림기 포트: 기본은 Discord 웹훅(URL 설정이 비어 있으면 보내지 않는다). 테스트는 가짜를 주입한다.
    app.state.summary = summary.SummaryService(
        app.state, notifier or summary.DiscordWebhookNotifier(app.state.settings))
    lifecycle.add(app.state.summary.start, app.state.summary.stop)

    async def broadcast_event(kind: str, event: dict) -> None:
        await app.state.hub.broadcast({"type": "event", "phase": kind, "event": event})

    app.state.events.subscribe(broadcast_event)
    # 클립 레코더는 감지기보다 먼저 등록한다: 이벤트 시작 알림 시점에 그 프레임까지 링버퍼에 들어 있다.
    app.state.clip_recorder = clips.ClipRecorder(
        app.state.settings, app.state.events, app.state.hub, app.state.clips_dir)
    app.state.frames.register(app.state.clip_recorder)
    # 소리 분류기 포트: 기본은 로컬 YAMNet(오디오가 PC 밖으로 나가지 않는다). 테스트는 가짜를 주입한다.
    app.state.classifier = classifier or labeling.YamnetClassifier()
    app.state.label_worker = labeling.LabelWorker(
        app.state.settings, app.state.events, app.state.hub, app.state.classifier, app.state.clips_dir)
    app.state.clip_recorder.on_clip_saved(app.state.label_worker.enqueue)
    lifecycle.add(app.state.label_worker.start, app.state.label_worker.stop)
    # 감지기는 레벨 방송보다 먼저 등록한다: 레벨을 받았다면 그 프레임의 감지는 끝나 있다.
    app.state.frames.register(EventDetector(
        app.state.settings, app.state.events, app.state.hub,
        sleep_mode=lambda: app.state.sleep.is_sleeping()))

    async def broadcast_level(frame: Frame) -> None:
        await app.state.hub.broadcast({
            "type": "level",
            "source_id": frame.source_id,
            "ts": frame.ts,
            "db": frame.db,
            "frame_ms": FRAME_MS,
        })

    app.state.frames.register(broadcast_level)

    @app.get("/api/status")
    def status() -> dict:
        return app.state.ingest_state.snapshot()

    app.include_router(settings.router)
    app.include_router(gaps.router)
    app.include_router(events.router)
    app.include_router(clips.router)
    app.include_router(labeling.router)
    app.include_router(stats.router)
    app.include_router(sleep.router)
    app.include_router(nights.router)
    app.include_router(ingest.router)
    app.include_router(realtime.router)

    # 정적 대시보드는 마지막에 마운트해야 /api, /ws 경로를 가리지 않는다.
    if static_dir is not None and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app
