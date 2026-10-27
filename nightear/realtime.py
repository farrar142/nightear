"""실시간 채널(/ws/realtime): 대시보드로 레벨 등 이벤트를 방송한다."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


QUEUE_SIZE = 200  # 구독자별 대기 메시지 상한(10Hz 레벨 기준 약 20초). 넘치면 가장 오래된 것부터 버린다.


class _Subscriber:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)

    def offer(self, message: dict[str, Any]) -> None:
        if self.queue.full():
            self.queue.get_nowait()  # 느린 구독자: 오래된 메시지를 버려 수집 경로를 막지 않는다
        self.queue.put_nowait(message)

    async def pump(self) -> None:
        while True:
            await self.ws.send_json(await self.queue.get())


class RealtimeHub:
    def __init__(self) -> None:
        self._subscribers: set[_Subscriber] = set()

    async def broadcast(self, message: dict[str, Any]) -> None:
        """구독자별 큐에 넣기만 하고 즉시 돌아온다(전송은 구독자별 sender 태스크가 한다)."""
        for sub in self._subscribers:
            sub.offer(message)

    async def serve(self, ws: WebSocket, initial_state: Callable[[], dict[str, Any]]) -> None:
        await ws.accept()
        sub = _Subscriber(ws)
        sub.offer(initial_state())  # 첫 메시지는 항상 state
        self._subscribers.add(sub)
        sender = asyncio.create_task(sub.pump())
        try:
            while True:
                await ws.receive_text()  # 클라이언트 메시지는 무시하고 연결 유지만 한다
        except WebSocketDisconnect:
            pass
        finally:
            self._subscribers.discard(sub)
            sender.cancel()


router = APIRouter()


@router.websocket("/ws/realtime")
async def realtime_ws(ws: WebSocket) -> None:
    app = ws.app
    await app.state.hub.serve(ws, app.state.ingest_state.snapshot)
