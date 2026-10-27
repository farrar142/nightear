"""수집 안정성: 단일 활성 소스, 승격, 수집 공백, 재시작 복구."""
from fastapi.testclient import TestClient

from nightear.gaps import HEARTBEAT_INTERVAL_S

from .support import audio
from .support.clients import ingest, realtime


def role(session) -> str:
    msg = session.ws.receive_json()
    assert msg["type"] == "role", msg
    return msg["role"]


def test_first_connection_is_active_second_is_secondary(client):
    with ingest(client, "a") as a:
        assert role(a) == "active"
        with ingest(client, "b") as b:
            assert role(b) == "secondary"
            assert client.get("/api/status").json()["active_source_id"] == "a"


def test_secondary_frames_are_not_ingested(client):
    with realtime(client) as rt:
        with ingest(client, "a") as a, ingest(client, "b") as b:
            role(a), role(b)
            b.send_pcm(audio.tone(-20, 0.5))
            a.send_pcm(audio.tone(-40, 0.3))
            levels = rt.receive_levels(3)
    assert {m["source_id"] for m in levels} == {"a"}
    assert all(abs(m["db"] + 40) < 0.5 for m in levels)


def test_secondary_is_promoted_when_active_leaves(client):
    with ingest(client, "a") as a:
        role(a)
        with ingest(client, "b") as b:
            role(b)
            with ingest(client, "c") as c:
                role(c)
                a.ws.close()
                # 가장 최근에 붙은 secondary(c)가 승격된다
                assert role(c) == "active"
                with realtime(client) as rt:
                    c.send_pcm(audio.tone(-30, 0.2))
                    levels = rt.receive_levels(2)
                assert {m["source_id"] for m in levels} == {"c"}


def test_no_gap_when_promotion_happens(client):
    with ingest(client, "a") as a, ingest(client, "b") as b:
        role(a), role(b)
        a.ws.close()
        assert role(b) == "active"
        assert client.get("/api/gaps").json()["gaps"] == []


def test_active_disconnect_records_gap_and_reconnect_closes_it(client, clock):
    with ingest(client, "a"):
        clock.advance(5)
    t1 = clock.now().timestamp()
    gaps = client.get("/api/gaps").json()["gaps"]
    assert gaps == [{"start_ts": t1, "end_ts": None, "ongoing": True}]
    clock.advance(90)
    with ingest(client, "a"):
        pass
    gaps = client.get("/api/gaps").json()["gaps"]
    assert gaps[0]["start_ts"] == t1 and gaps[0]["end_ts"] == t1 + 90


def test_gap_query_filters_by_range(client, clock):
    base = clock.now().timestamp()
    with ingest(client, "a"):
        pass  # 공백1 시작 base
    clock.advance(60)
    with ingest(client, "a"):
        clock.advance(600)  # 공백1 종료 base+60
    # 공백2 시작 base+660 (진행 중)

    def q(**p):
        return client.get("/api/gaps", params=p).json()["gaps"]

    assert len(q()) == 2
    assert len(q(**{"from": base + 700})) == 1  # 진행 중 공백만
    assert len(q(to=base + 10)) == 1
    assert q(**{"from": base + 100, "to": base + 200}) == []


def test_realtime_gap_messages(client):
    with realtime(client) as rt:
        with ingest(client, "a"):
            pass
        started = rt.receive_until(lambda m: m["type"] == "gap")
        assert started["state"] == "started"
        with ingest(client, "a"):
            ended = rt.receive_until(lambda m: m["type"] == "gap")
            assert ended["state"] == "ended"


def test_backend_restart_while_active_creates_gap_from_last_heartbeat(make_app, clock):
    with TestClient(make_app()) as c1:
        with realtime(c1) as rt, ingest(c1, "a") as a:
            role(a)
            a.send_pcm(audio.tone(-30, 0.1))  # 프레임 1개(경쟁 없이 하트비트 1회)
            rt.receive_levels(1)  # 서버가 프레임(=하트비트)을 처리한 뒤에 시계를 움직인다
            last_beat = clock.now().timestamp()
            clock.advance(HEARTBEAT_INTERVAL_S / 2)
            # 연결을 닫기 전에 앱이 죽었다고 가정: 하트비트 상태가 DB에 남은 채 새 앱을 만든다
            clock.advance(300)
            c2 = TestClient(make_app())
            gaps = c2.app.state.gaps.query()
    assert gaps == [{"start_ts": last_beat, "end_ts": None, "ongoing": True}]


def test_restart_during_open_gap_keeps_it(make_app, clock):
    with TestClient(make_app()) as c1:
        with ingest(c1, "a"):
            pass
        start = clock.now().timestamp()
    clock.advance(1000)
    with TestClient(make_app()) as c2:
        assert c2.get("/api/gaps").json()["gaps"] == [{"start_ts": start, "end_ts": None, "ongoing": True}]
        with ingest(c2, "a"):
            pass


def test_fresh_db_has_no_gaps(client):
    assert client.get("/api/gaps").json() == {"gaps": []}
