"""수집/실시간 WebSocket 테스트 클라이언트 헬퍼."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from fastapi.testclient import TestClient

from . import audio


def hello(source_id: str = "test-source", *, idle: bool = False, **overrides: Any) -> dict:
    msg = {
        "type": "hello",
        "source_id": source_id,
        "sample_rate": 16000,
        "channels": 1,
        "idle_detection_supported": idle,
    }
    msg.update(overrides)
    return msg


class IngestSession:
    """수집 WebSocket에 hello를 보낸 뒤 PCM/활동 메시지를 흘려보내는 소스 흉내."""

    def __init__(self, ws) -> None:
        self.ws = ws

    def send_pcm(self, pcm: bytes, chunk_ms: int = 100) -> None:
        for c in audio.chunks(pcm, chunk_ms):
            self.ws.send_bytes(c)

    def send_activity(self, state: str, idle_seconds: float = 0.0) -> None:
        self.ws.send_json({"type": "activity", "state": state, "idle_seconds": idle_seconds})


@contextmanager
def ingest(client: TestClient, source_id: str = "test-source", **hello_kwargs: Any) -> Iterator[IngestSession]:
    """hello 전송 후 hello_ack까지 받은 수집 세션을 연다."""
    with client.websocket_connect("/ws/ingest") as ws:
        ws.send_json(hello(source_id, **hello_kwargs))
        ack = ws.receive_json()
        assert ack["type"] == "hello_ack", ack
        yield IngestSession(ws)


class RealtimeSession:
    def __init__(self, ws) -> None:
        self.ws = ws

    def receive_until(self, predicate, limit: int = 200) -> dict:
        for _ in range(limit):
            msg = self.ws.receive_json()
            if predicate(msg):
                return msg
        raise AssertionError("기대한 실시간 메시지가 오지 않음")

    def receive_levels(self, n: int) -> list[dict]:
        out: list[dict] = []
        while len(out) < n:
            out.append(self.receive_until(lambda m: m["type"] == "level"))
        return out


@contextmanager
def realtime(client: TestClient) -> Iterator[RealtimeSession]:
    """실시간 채널을 열고 첫 state 메시지까지 받아 구독이 완료된 상태로 돌려준다."""
    with client.websocket_connect("/ws/realtime") as ws:
        first = ws.receive_json()
        assert first["type"] == "state", first
        session = RealtimeSession(ws)
        session.initial_state = first
        yield session
