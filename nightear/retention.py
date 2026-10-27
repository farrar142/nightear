"""별표와 보관 정리.

주기 작업이 retention_days(기본 30일)가 지난 비별표 클립의 오디오 파일을 지운다.
이벤트 행(시각·크기·라벨)은 남고 audio_deleted=1 로 표시된다. 별표 클립은 지우지 않는다.
나이는 이벤트 시작 시각과 주입된 시계의 현재 시각으로 잰다.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import clips

CHECK_INTERVAL_S = 3600.0
log = logging.getLogger(__name__)


class Retention:
    def __init__(self, state, interval_s: float = CHECK_INTERVAL_S) -> None:
        self._state = state
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None

    def run_once(self) -> int:
        """보관 기간이 지난 비별표 클립을 지우고 지운 개수를 돌려준다."""
        st = self._state
        cutoff = st.clock.now().timestamp() - st.settings.get("retention_days") * 86400.0
        rows = st.db.execute(
            "SELECT id FROM events WHERE clip_path IS NOT NULL AND starred = 0 AND start_ts < ?",
            (cutoff,))
        return sum(1 for r in rows if clips.delete_clip(st, r["id"]))

    async def _loop(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception("보관 정리 오류(다음 주기에 다시 시도)")
            await asyncio.sleep(self._interval_s)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


class StarBody(BaseModel):
    starred: bool


router = APIRouter(prefix="/api/events", tags=["retention"])


@router.put("/{event_id}/star")
def set_star(request: Request, event_id: int, body: StarBody) -> dict:
    event = request.app.state.events.set_starred(event_id, body.starred)
    if event is None:
        raise HTTPException(404, "이벤트가 없다")
    return event
