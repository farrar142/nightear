import pytest
from fastapi.testclient import TestClient

from nightear.levels import FRAME_BYTES, FRAME_MS

from .support import audio
from .support.clients import ingest, realtime


def test_한_청크로_보낸_1초도_프레임_위치_기준_0_1초_간격_ts를_가진다(client, clock):
    t0 = clock.now().timestamp()
    with realtime(client) as rt, ingest(client) as src:
        src.send_pcm(audio.tone(-20.0, 1.0), chunk_ms=1000)  # 시계는 진행시키지 않는다
        levels = rt.receive_levels(10)
    assert [m["ts"] for m in levels] == pytest.approx([t0 + 0.1 * i for i in range(10)])


def test_프레임_처리기를_등록하면_ts_db_pcm을_받는다(make_app):
    app = make_app()
    seen = []
    app.state.frames.register(seen.append)
    with TestClient(app) as c:
        with realtime(c) as rt, ingest(c) as src:
            src.send_pcm(audio.tone(-20.0, 0.3))
            rt.receive_levels(3)
    assert len(seen) == 3
    assert all(len(f.pcm) == FRAME_BYTES and f.source_id == "test-source" for f in seen)
    assert seen[1].ts - seen[0].ts == pytest.approx(FRAME_MS / 1000)
    assert round(seen[0].db, 1) == -20.0


def test_전송이_멈췄다_재개되면_벽시계로_재앵커한다(client, clock):
    with realtime(client) as rt, ingest(client) as src:
        src.send_pcm(audio.tone(-20.0, 0.1))
        first = rt.receive_levels(1)[0]
        clock.advance(60)
        src.send_pcm(audio.tone(-20.0, 0.1))
        second = rt.receive_levels(1)[0]
    assert second["ts"] == pytest.approx(first["ts"] + 60)


def test_같은_source_id의_탭_두_개_중_하나가_닫혀도_연결_상태가_유지된다(client):
    with ingest(client, "tab"):
        with ingest(client, "tab"):
            pass
        assert client.get("/api/status").json()["source_connected"] is True
    assert client.get("/api/status").json()["source_connected"] is False


def test_느린_구독자가_있어도_수집은_막히지_않는다(client):
    from nightear.realtime import QUEUE_SIZE

    with realtime(client), ingest(client) as src:  # 아무도 읽지 않는 구독자
        src.send_pcm(audio.silence((QUEUE_SIZE + 50) / 10), chunk_ms=1000)
        assert client.get("/api/status").json()["source_connected"] is True
