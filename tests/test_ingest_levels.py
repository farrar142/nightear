import pytest

from .support import audio
from .support.clients import hello, ingest, realtime


def test_합성_PCM을_보내면_실시간_채널에_기대한_dBFS가_나온다(client):
    with realtime(client) as rt, ingest(client) as src:
        src.send_pcm(audio.tone(-20.0, 0.5))
        levels = rt.receive_levels(5)
    assert [round(m["db"], 1) for m in levels] == [-20.0] * 5


def test_레벨은_약_10Hz로_나온다_프레임_100ms당_1개(client):
    with realtime(client) as rt, ingest(client) as src:
        # 소스가 보내는 청크 크기(20ms)와 무관하게 100ms 단위로 재조립된다
        src.send_pcm(audio.tone(-30.0, 1.0), chunk_ms=20)
        levels = rt.receive_levels(10)
    assert len(levels) == 10
    assert all(m["frame_ms"] == 100 for m in levels)


def test_소리_크기_변화가_레벨에_반영된다(client):
    with realtime(client) as rt, ingest(client) as src:
        src.send_pcm(audio.noise(-50.0, 0.3) + audio.tone(-10.0, 0.3))
        dbs = [m["db"] for m in rt.receive_levels(6)]
    assert all(abs(d - -50.0) < 0.5 for d in dbs[:3])
    assert all(abs(d - -10.0) < 0.5 for d in dbs[3:])


def test_무음은_하한값이다(client):
    with realtime(client) as rt, ingest(client) as src:
        src.send_pcm(audio.silence(0.2))
        levels = rt.receive_levels(2)
    assert all(m["db"] == -120.0 for m in levels)


def test_레벨_타임스탬프는_주입한_시계를_따른다(client, clock):
    with realtime(client) as rt, ingest(client) as src:
        src.send_pcm(audio.tone(-20.0, 0.1))
        first = rt.receive_levels(1)[0]
    assert first["ts"] == clock.now().timestamp()
    assert first["source_id"] == "test-source"


def test_연결_상태가_실시간_채널과_API에_나온다(client):
    with realtime(client) as rt:
        assert rt.initial_state["source_connected"] is False
        with ingest(client, "tab-1"):
            msg = rt.receive_until(lambda m: m["type"] == "source")
            assert msg["state"] == "connected" and msg["source_id"] == "tab-1"
            assert client.get("/api/status").json()["source_connected"] is True
        msg = rt.receive_until(lambda m: m["type"] == "source")
        assert msg["state"] == "disconnected"
    assert client.get("/api/status").json()["source_connected"] is False


def test_이미_연결된_소스가_있으면_새_실시간_구독자도_상태를_받는다(client):
    with ingest(client, "tab-1"):
        with realtime(client) as rt:
            assert rt.initial_state["source_connected"] is True
            assert rt.initial_state["source_id"] == "tab-1"


@pytest.mark.parametrize(
    "bad",
    [
        hello(sample_rate=48000),
        hello(channels=2),
        hello(source_id=""),
        {"type": "audio"},
    ],
)
def test_잘못된_hello는_거부되고_연결이_닫힌다(client, bad):
    with client.websocket_connect("/ws/ingest") as ws:
        ws.send_json(bad)
        err = ws.receive_json()
        assert err["type"] == "error"
    assert client.get("/api/status").json()["source_connected"] is False


def test_hello는_유휴_감지_지원_여부를_싣는다(client):
    with realtime(client) as rt, ingest(client, "tab-1", idle=True):
        msg = rt.receive_until(lambda m: m["type"] == "source")
        assert msg["idle_detection_supported"] is True


def test_활동_메시지를_보내도_수집은_계속된다(client):
    with realtime(client) as rt, ingest(client) as src:
        src.send_activity("active", 0)
        src.send_pcm(audio.tone(-20.0, 0.1))
        assert round(rt.receive_levels(1)[0]["db"], 1) == -20.0
