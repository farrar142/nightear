"""수집 WebSocket(/ws/ingest).

프로토콜 (소스 -> 백엔드):
  1. 연결 직후 텍스트(JSON) hello:
       {"type":"hello","source_id":str,"sample_rate":16000,"channels":1,"idle_detection_supported":bool}
     백엔드는 {"type":"hello_ack","source_id":...} 로 응답한다.
  2. 이후 바이너리 프레임: 16kHz 모노 16-bit 리틀엔디언 PCM (길이 자유, 백엔드가 100ms 프레임으로 재조립)
  3. 텍스트 활동 메시지: {"type":"activity","state":"active|idle|locked","idle_seconds":float}
     (이 티켓에서는 수신만 하고 소비하지 않는다. 수면 모드는 별도 티켓)
"""
from __future__ import annotations

import json
import itertools
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, WebSocket

from .frames import Frame, FrameClock
from .levels import CHANNELS, FRAME_MS, SAMPLE_RATE, FrameSplitter, rms_dbfs

WS_POLICY_VIOLATION = 1008


@dataclass
class IngestState:
    """현재 붙어 있는 수집 소스 상태(대시보드 표시용)."""

    # 연결(connection) id -> source_id. 같은 source_id의 탭이 둘이어도 연결별로 구분한다.
    connections: dict[int, str] = field(default_factory=dict)
    _ids: Any = field(default_factory=itertools.count, repr=False)

    def add(self, source_id: str) -> int:
        conn_id = next(self._ids)
        self.connections[conn_id] = source_id
        return conn_id

    def remove(self, conn_id: int) -> None:
        self.connections.pop(conn_id, None)

    def snapshot(self) -> dict[str, Any]:
        last = list(self.connections.values())[-1] if self.connections else None
        return {"type": "state", "source_connected": bool(self.connections), "source_id": last}


class HelloError(ValueError):
    pass


def parse_hello(raw: str) -> dict[str, Any]:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HelloError("hello는 JSON이어야 한다") from e
    if not isinstance(msg, dict) or msg.get("type") != "hello":
        raise HelloError("첫 메시지는 hello여야 한다")
    source_id = msg.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise HelloError("source_id가 필요하다")
    if msg.get("sample_rate") != SAMPLE_RATE:
        raise HelloError(f"sample_rate는 {SAMPLE_RATE}이어야 한다")
    if msg.get("channels") != CHANNELS:
        raise HelloError("모노(channels=1)만 지원한다")
    if not isinstance(msg.get("idle_detection_supported"), bool):
        raise HelloError("idle_detection_supported(bool)가 필요하다")
    return msg


router = APIRouter()


@router.websocket("/ws/ingest")
async def ingest_ws(ws: WebSocket) -> None:
    app = ws.app
    clock = app.state.clock
    hub = app.state.hub
    state: IngestState = app.state.ingest_state

    await ws.accept()
    try:
        first = await ws.receive()
        if first["type"] == "websocket.disconnect":
            return
        hello = parse_hello(first.get("text") or "")
    except HelloError as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close(code=WS_POLICY_VIOLATION)
        return

    source_id: str = hello["source_id"]
    conn_id = state.add(source_id)
    await ws.send_json({"type": "hello_ack", "source_id": source_id})
    await hub.broadcast({"type": "source", "state": "connected", "source_id": source_id,
                         "idle_detection_supported": hello["idle_detection_supported"]})
    await app.state.sleep.handle_hello(hello["idle_detection_supported"])

    splitter = FrameSplitter()
    frame_clock = FrameClock(clock.now().timestamp(), FRAME_MS / 1000.0)
    try:
        while True:
            event = await ws.receive()
            if event["type"] == "websocket.disconnect":
                break
            data = event.get("bytes")
            if data is not None:
                for frame in splitter.push(data):
                    ts = frame_clock.next_ts(clock.now().timestamp())
                    await app.state.frames.dispatch(
                        Frame(source_id=source_id, ts=ts, db=rms_dbfs(frame), pcm=frame)
                    )
            elif event.get("text"):
                try:
                    msg = json.loads(event["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "activity":
                    await app.state.sleep.handle_activity(msg)
    finally:
        state.remove(conn_id)
        await hub.broadcast({"type": "source", "state": "disconnected", "source_id": source_id})
