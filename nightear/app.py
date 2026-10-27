"""앱 팩토리. 외부 의존(시계, 이후 분류기·알림기)은 인자로 주입한다."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import events, gaps, ingest, realtime, settings
from .clock import Clock, SystemClock
from .detection import EventDetector
from .frames import Frame, FramePipeline
from .ingest import IngestState
from .levels import FRAME_MS
from .realtime import RealtimeHub
from .settings import SettingsStore
from .storage import Database

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    *,
    clock: Clock | None = None,
    db_path: str | Path | None = None,
    static_dir: Path | None = STATIC_DIR,
) -> FastAPI:
    db = Database(db_path or os.environ.get("NIGHTEAR_DB", "data/nightear.db"))
    app = FastAPI(title="nightear")
    app.state.clock = clock or SystemClock()
    app.state.db = db
    app.state.settings = SettingsStore(db)
    app.state.hub = RealtimeHub()
    app.state.ingest_state = IngestState()
    app.state.gaps = gaps.GapStore(db)
    app.state.frames = FramePipeline()
    app.state.events = events.EventStore(db)

    async def broadcast_event(kind: str, event: dict) -> None:
        await app.state.hub.broadcast({"type": "event", "phase": kind, "event": event})

    app.state.events.subscribe(broadcast_event)
    # 감지기는 레벨 방송보다 먼저 등록한다: 레벨을 받았다면 그 프레임의 감지는 끝나 있다.
    app.state.frames.register(EventDetector(app.state.settings, app.state.events, app.state.hub))

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
    app.include_router(ingest.router)
    app.include_router(realtime.router)

    # 정적 대시보드는 마지막에 마운트해야 /api, /ws 경로를 가리지 않는다.
    if static_dir is not None and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app
