"""기준선·트리거·이벤트 감지: 수집 WS로 합성 오디오를 넣고 HTTP/실시간 채널로만 검증한다."""
import pytest

from .support import audio
from .support.clients import ingest, realtime

FLOOR = -50.0


def feed(src, rt, pcm):
    """PCM을 보내고, 그 프레임들의 레벨이 방송될 때까지의 실시간 메시지를 모두 모아 돌려준다."""
    src.send_pcm(pcm)
    n_frames = len(pcm) // 3200
    got, levels = [], 0
    while levels < n_frames:
        msg = rt.ws.receive_json()
        got.append(msg)
        if msg["type"] == "level":
            levels += 1
    return got


def events_of(msgs, phase=None):
    return [m for m in msgs if m["type"] == "event" and (phase is None or m["phase"] == phase)]


def api_events(client, **params):
    r = client.get("/api/events", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def set_settings(client, **changes):
    assert client.put("/api/settings", json=changes).status_code == 200


def burst_audio(bursts, total, floor=FLOOR):
    return audio.noise_with_bursts(floor, total, bursts)


def test_바닥_소음_위_10dB_넘는_버스트는_이벤트_1개(client, clock):
    set_settings(client, merge_window_s=2.0)
    t0 = clock.now().timestamp()
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_audio([(3.0, 1.0, -35.0)], 5.0))
        msgs += feed(src, rt, audio.noise(FLOOR, 3.0, seed=1))
    assert [m["phase"] for m in events_of(msgs)] == ["started", "ended"]
    events = api_events(client)
    assert len(events) == 1
    ev = events[0]
    assert ev["start_ts"] == pytest.approx(t0 + 3.0, abs=0.01)
    assert ev["duration_s"] == pytest.approx(1.0, abs=0.01)
    assert ev["peak_db"] == pytest.approx(-35.0, abs=0.2)
    assert ev["baseline_db"] == pytest.approx(FLOOR, abs=1.0)
    assert ev["ongoing"] is False and ev["truncated"] is False


def test_이벤트_시작은_실시간_채널로_즉시_알려진다(client):
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_audio([(2.0, 1.0, -35.0)], 4.0))
    started = events_of(msgs, "started")
    assert len(started) == 1
    assert started[0]["event"]["ongoing"] is True
    assert started[0]["event"]["peak_db"] == pytest.approx(-35.0, abs=0.2)
    assert api_events(client)[0]["ongoing"] is True  # 병합 창이 끝나기 전이라 진행 중


def test_마진_미만_버스트는_이벤트가_없다(client):
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_audio([(3.0, 1.0, -42.0)], 5.0))
    assert events_of(msgs) == []
    assert api_events(client) == []


def test_최소_지속_시간_미만_버스트는_이벤트가_없다(client):
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_audio([(3.0, 0.1, -30.0)], 5.0))
    assert events_of(msgs) == []
    assert api_events(client) == []


def test_최소_지속_시간_이상이면_이벤트(client):
    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, burst_audio([(3.0, 0.2, -30.0)], 5.0))
    assert len(api_events(client)) == 1


def test_절대_하한_이하는_기준선_대비_튀어도_무시(client):
    quiet_floor = -80.0
    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, burst_audio([(3.0, 1.0, -65.0)], 5.0, floor=quiet_floor))
        assert api_events(client) == []
        set_settings(client, absolute_floor_db=-70.0)
        feed(src, rt, burst_audio([(1.0, 1.0, -65.0)], 3.0, floor=quiet_floor))
    assert len(api_events(client)) == 1


def test_병합_창_안의_재발은_같은_이벤트로_확장된다(client, clock):
    t0 = clock.now().timestamp()
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_audio([(2.0, 1.0, -35.0), (5.0, 1.0, -30.0)], 6.0))
        msgs += feed(src, rt, audio.noise(FLOOR, 11.0, seed=1))
    assert [m["phase"] for m in events_of(msgs)] == ["started", "updated", "ended"]
    events = api_events(client)
    assert len(events) == 1
    assert events[0]["start_ts"] == pytest.approx(t0 + 2.0, abs=0.01)
    assert events[0]["end_ts"] == pytest.approx(t0 + 6.0, abs=0.01)
    assert events[0]["peak_db"] == pytest.approx(-30.0, abs=0.2)


def test_병합_창_밖의_재발은_별개_이벤트(client):
    set_settings(client, merge_window_s=2.0)
    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, burst_audio([(2.0, 1.0, -35.0), (6.0, 1.0, -35.0)], 8.0))
        feed(src, rt, audio.noise(FLOOR, 3.0, seed=1))
    assert len(api_events(client)) == 2


def test_최대_길이에_이르면_잘려_종료된다(client):
    # 클립 최대 20초 = 앞 10초 + 이벤트 + 뒤 5초  ->  이벤트 최대 5초
    set_settings(client, max_clip_s=20.0, merge_window_s=2.0)
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_audio([(12.0, 8.0, -35.0)], 21.0))
        msgs += feed(src, rt, audio.noise(FLOOR, 3.0, seed=1))
    events = sorted(api_events(client), key=lambda e: e["start_ts"])
    assert len(events) == 2
    assert events[0]["duration_s"] == pytest.approx(5.0, abs=0.01)
    assert events[0]["truncated"] is True
    assert events[1]["truncated"] is False
    first_ended = events_of(msgs, "ended")[0]["event"]
    assert first_ended["truncated"] is True


def test_기준선과_트리거선이_실시간_채널로_나간다(client):
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, audio.noise(FLOOR, 3.0))
    base = [m for m in msgs if m["type"] == "baseline"]
    assert base
    last = base[-1]
    assert last["baseline_db"] == pytest.approx(FLOOR, abs=0.5)
    assert last["trigger_db"] == pytest.approx(FLOOR + 10.0, abs=0.5)
    assert last["floor_db"] == -60.0
    assert last["source_id"] == "test-source"


def test_기준선은_최근_구간의_중앙값으로_따라간다(client):
    set_settings(client, baseline_window_minutes=0.1)  # 6초
    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, audio.noise(-50.0, 3.0))
        msgs = feed(src, rt, audio.noise(-30.0, 12.0, seed=2))
    last = [m for m in msgs if m["type"] == "baseline"][-1]
    assert last["baseline_db"] == pytest.approx(-30.0, abs=0.5)


def test_설정으로_마진을_바꾸면_감지가_달라진다(client):
    with realtime(client) as rt, ingest(client) as src:
        set_settings(client, trigger_margin_db=20.0)
        feed(src, rt, burst_audio([(2.0, 1.0, -35.0)], 4.0))  # +15dB < 20dB
        assert api_events(client) == []
        set_settings(client, trigger_margin_db=10.0)
        feed(src, rt, burst_audio([(1.0, 1.0, -35.0)], 3.0, floor=FLOOR))
    assert len(api_events(client)) == 1


def test_이벤트_목록은_시간순_크기순_정렬을_지원한다(client):
    set_settings(client, merge_window_s=1.0)
    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, burst_audio(
            [(2.0, 0.5, -38.0), (6.0, 0.5, -30.0), (10.0, 0.5, -34.0)], 13.0))
        feed(src, rt, audio.noise(FLOOR, 2.0, seed=1))

    def peaks(**params):
        return [round(e["peak_db"]) for e in api_events(client, **params)]

    assert peaks() == [-34, -30, -38]  # 기본: 최신순
    assert peaks(sort="time", order="asc") == [-38, -30, -34]
    assert peaks(sort="size") == [-30, -34, -38]
    assert peaks(sort="size", order="asc") == [-38, -34, -30]
    assert peaks(sort="size", limit=1) == [-30]
    starts = [e["start_ts"] for e in api_events(client, sort="time", order="asc")]
    assert len(api_events(client, since=starts[1])) == 2
    assert client.get("/api/events", params={"sort": "bogus"}).status_code == 422


def test_백엔드를_재시작해도_이벤트가_유지된다(make_app, client):
    from fastapi.testclient import TestClient

    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, burst_audio([(2.0, 1.0, -35.0)], 4.0))
    before = api_events(client)
    assert len(before) == 1
    with TestClient(make_app()) as c2:
        after = c2.get("/api/events").json()
    assert [e["id"] for e in after] == [before[0]["id"]]
    assert after[0]["ongoing"] is False
