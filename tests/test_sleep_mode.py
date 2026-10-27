from datetime import datetime

from contextlib import contextmanager

from .support import audio
from .support.clients import ingest, realtime
from .support.clock import KST


def sleep_state(client):
    return client.get("/api/sleep").json()


class Source:
    """수집 소스 + 실시간 구독. send_activity 뒤에는 settle()로 서버가 처리했음을 보장한다."""

    def __init__(self, rt, src):
        self.rt, self.src = rt, src

    def send_activity(self, state, idle_seconds=0.0):
        self.src.send_activity(state, idle_seconds)
        self.settle()

    def settle(self):
        # 같은 WebSocket의 메시지는 순서대로 처리되므로, 뒤따른 PCM 레벨이 돌아오면 앞선 메시지는 처리된 것이다.
        self.src.send_pcm(audio.tone(-40.0, 0.1))
        self.rt.receive_until(lambda m: m["type"] == "level")


@contextmanager
def source(client, *, idle):
    with realtime(client) as rt, ingest(client, idle=idle) as src:
        s = Source(rt, src)
        s.settle()
        yield s


def test_밤_시간대에_유휴_20분이면_수면_모드에_들어간다(client, clock):
    with realtime(client) as rt, ingest(client, idle=True) as src:
        src.send_activity("idle", 0)
        assert sleep_state(client)["sleeping"] is False
        clock.advance(minutes=19)
        src.send_activity("idle", 19 * 60)
        assert sleep_state(client)["sleeping"] is False
        clock.advance(minutes=1)
        src.send_activity("idle", 20 * 60)
        msg = rt.receive_until(lambda m: m["type"] == "sleep" and m["sleeping"])
    assert msg["reasons"]["in_night_window"] is True
    assert msg["reasons"]["idle_state"] == "idle"
    assert msg["reasons"]["idle_seconds"] == 20 * 60


def test_시간만_흘러도_조회하면_유휴_기준에_도달해_진입한다(client, clock):
    with source(client, idle=True) as src:
        src.send_activity("locked", 0)
        clock.advance(minutes=20)
        assert sleep_state(client)["sleeping"] is True


def test_active_신호가_오면_즉시_해제된다(client, clock):
    with realtime(client) as rt, ingest(client, idle=True) as src:
        src.send_activity("idle", 25 * 60)
        rt.receive_until(lambda m: m["type"] == "sleep" and m["sleeping"])
        src.send_activity("active", 0)
        msg = rt.receive_until(lambda m: m["type"] == "sleep" and not m["sleeping"])
    assert msg["reasons"]["idle_state"] == "active"


def test_낮_시간대_유휴는_수면_모드가_아니다(client, clock):
    clock.set(datetime(2025, 1, 16, 14, 0, tzinfo=KST))
    with source(client, idle=True) as src:
        src.send_activity("idle", 3 * 3600)
        s = sleep_state(client)
    assert s["sleeping"] is False
    assert s["reasons"]["in_night_window"] is False


def test_유휴_미지원_소스는_밤_시간대만으로_판단한다(client, clock):
    with source(client, idle=False):
        s = sleep_state(client)
        assert s["sleeping"] is True
        assert s["reasons"]["idle_detection_supported"] is False
        clock.set(datetime(2025, 1, 16, 12, 0, tzinfo=KST))  # 밤 시간대 끝
        assert sleep_state(client)["sleeping"] is False


def test_유휴_미지원_소스도_낮에는_수면_모드가_아니다(client, clock):
    clock.set(datetime(2025, 1, 16, 15, 0, tzinfo=KST))
    with source(client, idle=False):
        assert sleep_state(client)["sleeping"] is False


def test_수면_모드_도중_밤_시간대가_끝나면_해제된다(client, clock):
    with source(client, idle=True) as src:
        src.send_activity("idle", 30 * 60)
        assert sleep_state(client)["sleeping"] is True
        clock.set(datetime(2025, 1, 16, 12, 0, tzinfo=KST))
        src.send_activity("idle", 13 * 3600)
        assert sleep_state(client)["sleeping"] is False


def test_수면_구간_시작과_끝이_기록된다(client, clock):
    with source(client, idle=True) as src:
        src.send_activity("idle", 20 * 60)
        start = clock.now().timestamp()
        clock.advance(minutes=90)
        src.send_activity("active", 0)
        end = clock.now().timestamp()
        clock.advance(minutes=5)
        src.send_activity("idle", 20 * 60)  # 두 번째 구간(진행 중)
    sessions = client.get("/api/sleep/sessions").json()
    assert sessions[0] == {"start": start, "end": end}
    assert sessions[1]["end"] is None
    seam = client.app.state.sleep
    assert seam.is_sleeping(start + 60) is True
    assert seam.is_sleeping(end + 60) is False


def test_밤_시간대와_유휴_기준을_설정으로_바꿀_수_있다(client, clock):
    client.put("/api/settings", json={"idle_threshold_minutes": 5, "night_start_hour": 23, "night_end_hour": 6})
    with source(client, idle=True) as src:
        src.send_activity("idle", 5 * 60)
        assert sleep_state(client)["sleeping"] is True
        r = sleep_state(client)["reasons"]
        assert (r["night_start_hour"], r["night_end_hour"], r["idle_threshold_seconds"]) == (23, 6, 300)
        clock.set(datetime(2025, 1, 16, 6, 30, tzinfo=KST))
        assert sleep_state(client)["sleeping"] is False


def test_전이_콜백이_진입과_해제에_호출된다(client, clock):
    seen = []
    client.app.state.sleep.on_transition(seen.append)
    with source(client, idle=True) as src:
        src.send_activity("idle", 20 * 60)
        src.send_activity("active", 0)
    assert [t.sleeping for t in seen] == [True, False]
    assert seen[1].start_ts == seen[0].ts
