"""#3/#4/#5 통합 리뷰 반영: 이벤트 종료 보장, sleep_mode 기록, 입력 규칙, 격리, 수명주기, 설정 캐시."""
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from nightear import sleep as sleep_mod

from .support import audio
from .support.clients import ingest, realtime
from .support.clock import KST
from .test_detection import api_events, burst_audio, feed
from .test_sleep_mode import sleep_state, source


def drain_role(*sessions):
    for s in sessions:
        assert s.ws.receive_json()["type"] == "role"


# ---- #3: 이벤트 종료 보장 ----
def test_활성_소스가_사라지면_진행_중_이벤트가_종료된다(client):
    with realtime(client) as rt:
        with ingest(client) as src:
            feed(src, rt, burst_audio([(2.0, 1.0, -35.0)], 4.0))
            assert api_events(client)[0]["ongoing"] is True
        ended = rt.receive_until(lambda m: m["type"] == "event" and m["phase"] == "ended")
    assert ended["event"]["ongoing"] is False
    assert api_events(client)[0]["ongoing"] is False


def test_소스가_바뀌면_이전_소스의_진행_중_이벤트가_종료된다(client):
    with realtime(client) as rt:
        with ingest(client, "a") as a:
            feed(a, rt, burst_audio([(2.0, 1.0, -35.0)], 4.0))
            with ingest(client, "b") as b:
                b.send_pcm(audio.noise(-50.0, 0.2))
                ended = rt.receive_until(lambda m: m["type"] == "event" and m["phase"] == "ended")
    assert ended["event"]["source_id"] == "a"


def test_강등만으로는_이벤트가_끊기지_않고_공백도_없다(client):
    with realtime(client) as rt, ingest(client, "a") as a:
        feed(a, rt, burst_audio([(2.0, 1.0, -35.0)], 4.0))
        with ingest(client, "a"):  # 같은 source_id의 새 연결: 감지기 입장에선 소스 연속
            assert client.get("/api/gaps").json()["gaps"] == []
            assert api_events(client)[0]["ongoing"] is True


# ---- #3: sleep_mode 메타데이터 ----
def test_이벤트는_시작_시점의_수면_모드를_기록한다(client, clock):
    with realtime(client) as rt, ingest(client) as src:  # 밤(23시) + 유휴 미지원 -> 수면 모드
        feed(src, rt, burst_audio([(2.0, 1.0, -35.0)], 4.0))
    assert api_events(client)[0]["sleep_mode"] is True


def test_낮에_시작한_이벤트는_수면_모드가_아니다(client, clock):
    clock.set(datetime(2025, 1, 15, 14, 0, tzinfo=KST))
    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, burst_audio([(2.0, 1.0, -35.0)], 4.0))
    assert api_events(client)[0]["sleep_mode"] is False


# ---- #3: 격리 ----
def test_실패하는_프레임_처리기와_이벤트_구독자가_수집을_막지_않는다(client):
    def boom(*_a):
        raise RuntimeError("boom")

    client.app.state.frames._processors.insert(0, boom)
    client.app.state.events.subscribe(boom)
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_audio([(2.0, 1.0, -35.0)], 4.0))
    assert [m for m in msgs if m["type"] == "event" and m["phase"] == "started"]


def test_설정은_캐시되고_수정하면_즉시_반영된다(client):
    db = client.app.state.db
    store = client.app.state.settings
    store.get_all()
    calls = []
    orig = db.execute
    db.execute = lambda *a, **k: calls.append(a) or orig(*a, **k)
    for _ in range(5):
        store.get_all()
    assert calls == []
    assert client.put("/api/settings", json={"trigger_margin_db": 7.0}).status_code == 200
    assert store.get_all()["trigger_margin_db"] == 7.0


# ---- #4: 수면 입력은 active 연결만 ----
def test_secondary의_hello는_수면_모드_입력을_바꾸지_않고_승격되면_적용된다(client):
    with ingest(client, "a", idle=False) as a:
        a_role = a
        with ingest(client, "b", idle=True) as b:  # b가 active
            drain_role(a_role, a_role, b)  # a: active, secondary / b: active
            assert sleep_state(client)["reasons"]["idle_detection_supported"] is True
            with ingest(client, "c", idle=False) as c:  # c가 active, b는 secondary
                drain_role(b, c)
                assert sleep_state(client)["reasons"]["idle_detection_supported"] is False
                c.ws.close()
                # b가 승격 -> b의 hello(idle 지원)가 수면 입력이 된다
                b.ws.receive_json()
                for _ in range(500):
                    if sleep_state(client)["reasons"]["idle_detection_supported"] is True:
                        break
                assert sleep_state(client)["reasons"]["idle_detection_supported"] is True


def test_secondary의_활동_메시지는_수면_상태를_바꾸지_않는다(client, clock):
    with realtime(client) as rt, ingest(client, "a", idle=True) as a:
        drain_role(a)
        with ingest(client, "b", idle=True) as b:
            drain_role(a, b)
            b.send_activity("locked", 0)
            a.send_activity("active", 0)  # a는 secondary: 무시되어야 한다
            b.send_pcm(audio.tone(-40.0, 0.1))
            rt.receive_until(lambda m: m["type"] == "level")
            assert sleep_state(client)["reasons"]["idle_state"] == "locked"


def test_유한하지_않은_idle_seconds는_무시된다(client):
    with source(client, idle=True) as s:
        s.src.ws.send_text('{"type":"activity","state":"idle","idle_seconds":Infinity}')
        s.settle()
        assert sleep_state(client)["reasons"]["idle_state"] is None
        s.src.ws.send_text('{"type":"activity","state":"idle","idle_seconds":NaN}')
        s.settle()
        assert sleep_state(client)["reasons"]["idle_state"] is None


# ---- #4: 격리 / 재시작 / 수명주기 ----
def test_실패하는_전이_콜백이_수집을_막지_않는다(client):
    def boom(_t):
        raise RuntimeError("boom")

    client.app.state.sleep.on_transition(boom)
    with realtime(client) as rt, ingest(client) as src:  # 밤 + 유휴 미지원 -> 진입 전이
        src.send_pcm(audio.tone(-40.0, 0.1))
        rt.receive_until(lambda m: m["type"] == "level")
    assert client.app.state.sleep.intervals()


def test_틱에서_예외가_나도_루프가_계속_돈다(make_app, monkeypatch):
    monkeypatch.setattr(sleep_mod, "TICK_SECONDS", 0.01)
    app = make_app()
    calls = []

    async def flaky():
        calls.append(1)
        raise RuntimeError("boom")

    app.state.sleep.evaluate = flaky
    with TestClient(app):
        time.sleep(0.3)
    assert len(calls) >= 3


def test_재시작하면_열린_수면_구간은_마지막_평가_시각에서_닫힌다(make_app, clock):
    with TestClient(make_app()) as c1:
        with realtime(c1) as rt, ingest(c1) as src:
            src.send_pcm(audio.tone(-40.0, 0.1))
            rt.receive_until(lambda m: m["type"] == "level")  # hello 처리 완료 보장
            start = clock.now().timestamp()
            clock.advance(100)
            c1.get("/api/sleep")  # 평가 -> last_ts 갱신
            c2 = TestClient(make_app())  # 비정상 종료 후 재시작(구간이 열린 채)
            assert c2.app.state.sleep.intervals() == [(start, start + 100)]


def test_수면_구간_조회는_범위로_걸러진다(client):
    db = client.app.state.db
    for s, e in [(0, 10), (20, 30), (40, None)]:
        db.execute("INSERT INTO sleep_sessions(start_ts, end_ts) VALUES(?, ?)", (s, e))
    sl = client.app.state.sleep
    assert sl.intervals(12, 35) == [(20, 30)]
    assert sl.intervals(35, None) == [(40, None)]
    assert sl.is_sleeping(25) and not sl.is_sleeping(15) and sl.is_sleeping(100)


def test_수명주기_훅이_시작_종료_순서로_호출되고_실패해도_계속된다(make_app):
    app = make_app()
    log = []

    async def bad():
        raise RuntimeError("boom")

    app.state.lifecycle.add(lambda: log.append("start1"), lambda: log.append("stop1"))
    app.state.lifecycle.add(bad, bad)
    app.state.lifecycle.add(None, lambda: log.append("stop3"))
    with TestClient(app):
        assert log == ["start1"]
    assert log == ["start1", "stop3", "stop1"]
