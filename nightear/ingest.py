"""수집 WebSocket(/ws/ingest).

프로토콜 (소스 -> 백엔드):
  1. 연결 직후 텍스트(JSON) hello:
       {"type":"hello","source_id":str,"sample_rate":16000,"channels":1,"idle_detection_supported":bool}
     백엔드는 {"type":"hello_ack","source_id":...} 로 응답하고, 이어 {"type":"role","role":"active|secondary"} 를 보낸다.
  2. 이후 바이너리 프레임: 16kHz 모노 16-bit 리틀엔디언 PCM (길이 자유, 백엔드가 100ms 프레임으로 재조립)
  3. 텍스트 활동 메시지: {"type":"activity","state":"active|idle|locked","idle_seconds":float}
     활성(active) 연결의 hello/활동만 수면 모드의 입력이 된다(secondary는 기록만 해 둔다).

단일 활성 소스 정책
  - 가장 최근에 붙은 연결이 active, 이전 active는 secondary(표시 전용)로 강등된다(role 메시지로 알림).
    (반쯤 끊긴 옛 연결이 새 재연결을 막지 않도록 새 연결이 이긴다.)
  - active가 떠나면 남은 연결 중 가장 최근 것이 승격된다. 승격 대상이 이미 죽어 있으면 건너뛴다.
  - 강등/승격은 수집 공백을 만들지 않는다. 남은 연결이 없을 때만 공백이 열린다.
  - active가 바뀌면 새 active가 마지막으로 보낸 hello/활동이 수면 모드 입력이 된다.
"""
from __future__ import annotations

import itertools
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, WebSocket

from .frames import Frame, FrameClock
from .levels import CHANNELS, FRAME_MS, SAMPLE_RATE, FrameSplitter, rms_dbfs

WS_POLICY_VIOLATION = 1008
log = logging.getLogger(__name__)


@dataclass
class IngestState:
    """현재 붙어 있는 수집 소스 상태(대시보드 표시용). 정책은 모듈 docstring 참고."""

    # 연결(connection) id -> source_id. 같은 source_id의 탭이 둘이어도 연결별로 구분한다. 삽입 순서 = 접속 순서.
    connections: dict[int, str] = field(default_factory=dict)
    sockets: dict[int, Any] = field(default_factory=dict, repr=False)
    hellos: dict[int, bool] = field(default_factory=dict, repr=False)  # idle_detection_supported
    activities: dict[int, tuple[dict, float]] = field(default_factory=dict, repr=False)  # (메시지, 수신 시각)
    active_id: int | None = None
    _ids: Any = field(default_factory=itertools.count, repr=False)

    def add(self, source_id: str, ws: Any = None, idle_supported: bool = False) -> int:
        """연결을 등록하고 active로 만든다. 이전 active는 호출 전 `active_id`로 알 수 있다."""
        conn_id = next(self._ids)
        self.connections[conn_id] = source_id
        self.sockets[conn_id] = ws
        self.hellos[conn_id] = idle_supported
        self.active_id = conn_id
        return conn_id

    def remove(self, conn_id: int) -> int | None:
        """연결을 제거한다. active였다면 승격된 연결 id를 돌려준다(없으면 None)."""
        for d in (self.connections, self.sockets, self.hellos, self.activities):
            d.pop(conn_id, None)
        if self.active_id != conn_id:
            return None
        self.active_id = next(reversed(self.connections), None)
        return self.active_id

    def is_active(self, conn_id: int) -> bool:
        return self.active_id == conn_id

    def snapshot(self) -> dict[str, Any]:
        active = self.connections.get(self.active_id) if self.active_id is not None else None
        return {"type": "state", "source_connected": self.active_id is not None, "source_id": active,
                "active_source_id": active, "source_count": len(self.connections)}


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


async def _safe(coro, what: str) -> None:
    try:
        await coro
    except Exception:
        log.exception("%s 처리 중 오류(무시하고 계속)", what)


async def apply_sleep_input(app, conn_id: int) -> None:
    """active 연결이 마지막으로 보낸 hello/활동을 수면 모드 입력으로 적용한다."""
    state: IngestState = app.state.ingest_state
    if conn_id not in state.connections:
        return
    sleep = app.state.sleep
    await _safe(sleep.handle_hello(state.hellos[conn_id]), "sleep hello")
    last = state.activities.get(conn_id)
    if last is not None:
        msg, received = last
        msg = dict(msg)
        secs = msg.get("idle_seconds")
        if msg.get("state") != "active" and isinstance(secs, (int, float)) and not isinstance(secs, bool) \
                and math.isfinite(secs):
            msg["idle_seconds"] = secs + max(0.0, app.state.clock.now().timestamp() - received)
        await _safe(sleep.handle_activity(msg), "sleep activity")


@router.websocket("/ws/ingest")
async def ingest_ws(ws: WebSocket) -> None:
    app = ws.app
    clock = app.state.clock
    hub = app.state.hub
    state: IngestState = app.state.ingest_state
    gaps = app.state.gaps

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
    previous = state.active_id
    conn_id = state.add(source_id, ws, hello["idle_detection_supported"])
    is_active = True
    try:
        await ws.send_json({"type": "hello_ack", "source_id": source_id})
        await ws.send_json({"type": "role", "role": "active"})
        if previous is not None and previous in state.sockets:
            try:  # 강등: 새 연결이 이긴다. 죽은 연결이면 그 연결의 정리 경로에 맡긴다.
                await state.sockets[previous].send_json({"type": "role", "role": "secondary"})
            except Exception:
                pass
        now_ts = clock.now().timestamp()
        if gaps.source_active(now_ts):
            await hub.broadcast({"type": "gap", "state": "ended", "ts": now_ts})
        await hub.broadcast({"type": "source", "state": "connected", "source_id": source_id,
                             "role": "active", "idle_detection_supported": hello["idle_detection_supported"]})
        await hub.broadcast(state.snapshot())
        await apply_sleep_input(app, conn_id)

        splitter = FrameSplitter()
        frame_clock = FrameClock(clock.now().timestamp(), FRAME_MS / 1000.0)
        while True:
            event = await ws.receive()
            if event["type"] == "websocket.disconnect":
                break
            data = event.get("bytes")
            if data is not None:
                if not state.is_active(conn_id):
                    # secondary: 표시 전용. 프레임은 파이프라인에 넣지 않는다(이중 수집 방지).
                    is_active = False
                    continue
                if not is_active:
                    # (재)승격 직후: secondary 시절의 잔여 샘플/시각 앵커를 버리고 지금부터 다시 앵커한다.
                    is_active = True
                    splitter = FrameSplitter()
                    frame_clock = FrameClock(clock.now().timestamp(), FRAME_MS / 1000.0)
                for frame in splitter.push(data):
                    now_ts = clock.now().timestamp()
                    ts = frame_clock.next_ts(now_ts)
                    gaps.heartbeat(now_ts)
                    await app.state.frames.dispatch(
                        Frame(source_id=source_id, ts=ts, db=rms_dbfs(frame), pcm=frame)
                    )
            elif event.get("text"):
                try:
                    msg = json.loads(event["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "activity":
                    state.activities[conn_id] = (msg, clock.now().timestamp())
                    if state.is_active(conn_id):  # secondary의 활동은 수면 모드에 반영하지 않는다
                        await _safe(app.state.sleep.handle_activity(msg), "sleep activity")
    finally:
        was_active = state.is_active(conn_id)
        promoted = state.remove(conn_id)
        now_ts = clock.now().timestamp()
        # 승격 대상이 이미 죽어 있으면(역할 전송 실패) 건너뛰고 다음 후보를 승격한다.
        while promoted is not None:
            try:
                await state.sockets[promoted].send_json({"type": "role", "role": "active"})
                break
            except Exception:
                promoted = state.remove(promoted)
        if was_active:
            if promoted is None:
                await _safe(app.state.frames.source_lost(), "프레임 처리기 source_lost")
                if gaps.source_lost(now_ts):
                    await hub.broadcast({"type": "gap", "state": "started", "ts": now_ts})
            else:
                gaps.heartbeat(now_ts)
                await apply_sleep_input(app, promoted)
        await hub.broadcast({"type": "source", "state": "disconnected", "source_id": source_id})
        await hub.broadcast(state.snapshot())
