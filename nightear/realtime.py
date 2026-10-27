"""실시간 채널(/ws/realtime): 대시보드로 레벨 등 이벤트를 방송한다."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


class RealtimeHub:
    def __init__(self) -> None:
        self._subscribers: set[WebSocket] = set()

    async def broadcast(self, message: dict[str, Any]) -> None:
        for ws in list(self._subscribers):
            try:
                await ws.send_json(message)
            except Exception:
                self._subscribers.discard(ws)

    async def serve(self, ws: WebSocket, initial_state: Callable[[], dict[str, Any]]) -> None:
        await ws.accept()
        await ws.send_json(initial_state())
        self._subscribers.add(ws)
        try:
            while True:
                await ws.receive_text()  # 클라이언트 메시지는 무시하고 연결 유지만 한다
        except WebSocketDisconnect:
            pass
        finally:
            self._subscribers.discard(ws)


router = APIRouter()


@router.websocket("/ws/realtime")
async def realtime_ws(ws: WebSocket) -> None:
    app = ws.app
    await app.state.hub.serve(ws, app.state.ingest_state.snapshot)
