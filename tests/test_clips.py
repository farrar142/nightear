"""클립 저장과 재생: 수집 WS로 합성 오디오를 넣고 HTTP/실시간 채널로만 검증한다."""
import io
import struct
import time
import wave
from datetime import datetime

import pytest

from .support import audio
from .support.clients import ingest, realtime
from .support.clock import KST

FLOOR = -50.0
LOUD = -35.0


def feed(src, rt, pcm):
    """PCM을 보내고 그 프레임들의 레벨이 방송될 때까지의 실시간 메시지를 모두 모아 돌려준다."""
    src.send_pcm(pcm)
    n_frames = len(pcm) // 3200
    got, levels = [], 0
    while levels < n_frames:
        msg = rt.ws.receive_json()
        got.append(msg)
        if msg["type"] == "level":
            levels += 1
    return got


def set_settings(client, **changes):
    assert client.put("/api/settings", json=changes).status_code == 200


def events(client):
    return client.get("/api/events").json()


def clip_msgs(msgs):
    return [m for m in msgs if m["type"] == "clip"]


def read_wav(client, event_id):
    r = client.get(f"/api/events/{event_id}/clip")
    assert r.status_code == 200, r.text
    with wave.open(io.BytesIO(r.content)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        return w.readframes(w.getnframes())


def rms_db(pcm, start_s, end_s):
    seg = pcm[int(start_s * 16000) * 2 : int(end_s * 16000) * 2]
    n = len(seg) // 2
    samples = struct.unpack(f"<{n}h", seg)
    import math
    return 20 * math.log10(math.sqrt(sum(s * s for s in samples) / n) / 32768.0)


@pytest.mark.parametrize("merge_window", [10.0, 2.0])
def test_수면_모드_버스트는_앞_10초와_뒤_5초를_담은_클립이_된다(client, clock, merge_window):
    set_settings(client, merge_window_s=merge_window)
    t0 = clock.now().timestamp()
    with realtime(client) as rt, ingest(client) as src:
        pcm = audio.noise_with_bursts(FLOOR, 30.0, [(15.0, 1.0, LOUD)])
        msgs = feed(src, rt, pcm)
    assert len(clip_msgs(msgs)) == 1
    [ev] = events(client)
    assert ev["sleep_mode"] is True and ev["has_clip"] is True
    # 앞 10초 + 이벤트 1초 + 뒤 5초
    assert ev["clip_duration_s"] == pytest.approx(16.0, abs=0.01)
    assert ev["clip_start_ts"] == pytest.approx(t0 + 5.0, abs=0.01)
    clip = read_wav(client, ev["id"])
    assert len(clip) / 2 / 16000 == pytest.approx(16.0, abs=0.01)
    assert rms_db(clip, 0.0, 9.5) == pytest.approx(FLOOR, abs=1.5)  # 앞 10초는 소음 바닥
    assert rms_db(clip, 10.0, 11.0) == pytest.approx(LOUD, abs=0.5)  # 이벤트 본체
    assert rms_db(clip, 11.5, 16.0) == pytest.approx(FLOOR, abs=1.5)  # 기준선으로 돌아온 뒤 5초


def test_실시간_채널로_클립_저장이_알려진다(client):
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, audio.noise_with_bursts(FLOOR, 25.0, [(12.0, 1.0, LOUD)]))
    [msg] = clip_msgs(msgs)
    assert msg["event"]["has_clip"] is True
    assert msg["event"]["id"] == events(client)[0]["id"]


def test_수면_모드가_아니면_클립_없이_메타데이터만_남는다(client, clock):
    clock.set(datetime(2025, 1, 15, 14, 0, 0, tzinfo=KST))  # 밤 시간대 밖
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, audio.noise_with_bursts(FLOOR, 30.0, [(15.0, 1.0, LOUD)]))
    assert clip_msgs(msgs) == []
    [ev] = events(client)
    assert ev["sleep_mode"] is False
    assert ev["has_clip"] is False and ev["clip_duration_s"] is None
    assert ev["peak_db"] == pytest.approx(LOUD, abs=0.3)
    assert client.get(f"/api/events/{ev['id']}/clip").status_code == 404


def test_최대_길이에서_클립이_잘린다(client, clock):
    set_settings(client, max_clip_s=20.0, merge_window_s=2.0)
    with realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, audio.noise_with_bursts(FLOOR, 24.0, [(12.0, 8.0, LOUD)]))
        msgs += feed(src, rt, audio.noise(FLOOR, 10.0, seed=1))
    evs = sorted(events(client), key=lambda e: e["start_ts"])
    assert len(evs) == 2 and evs[0]["truncated"] is True
    assert evs[0]["clip_duration_s"] == pytest.approx(20.0, abs=0.01)
    assert len(read_wav(client, evs[0]["id"])) / 2 / 16000 == pytest.approx(20.0, abs=0.01)
    assert evs[1]["clip_duration_s"] <= 20.0 + 0.01  # 이어진 소음은 별개 이벤트·클립


def test_앞_뒤_여유를_설정으로_바꿀_수_있다(client):
    set_settings(client, pre_roll_s=2.0, post_roll_s=1.0, merge_window_s=2.0)
    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, audio.noise_with_bursts(FLOOR, 25.0, [(15.0, 1.0, LOUD)]))
    [ev] = events(client)
    assert ev["clip_duration_s"] == pytest.approx(4.0, abs=0.01)  # 2 + 1 + 1


def test_클립은_Range와_다운로드를_지원한다(client):
    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, audio.noise_with_bursts(FLOOR, 25.0, [(12.0, 1.0, LOUD)]))
    [ev] = events(client)
    url = f"/api/events/{ev['id']}/clip"
    full = client.get(url)
    assert full.headers["content-type"] == "audio/wav"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-disposition"].startswith("inline")
    part = client.get(url, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == full.content[:100]
    down = client.get(url, params={"download": "true"})
    assert down.headers["content-disposition"].startswith("attachment")
    assert client.get("/api/events/9999/clip").status_code == 404


def test_수집이_끊기면_가진_만큼으로_클립을_마무리한다(client):
    with realtime(client) as rt:
        with ingest(client) as src:
            # 버스트 직후 뒤 여유 5초가 차기 전에 끊긴다
            feed(src, rt, audio.noise_with_bursts(FLOOR, 18.0, [(15.0, 1.0, LOUD)]))
        for _ in range(50):  # 연결 종료 처리는 서버 쪽에서 비동기로 일어난다
            evs = events(client)
            if evs and evs[0]["has_clip"]:
                break
            time.sleep(0.1)
    [ev] = events(client)
    assert ev["has_clip"] is True
    assert ev["clip_duration_s"] == pytest.approx(18.0 - 5.0, abs=0.2)  # 5초 시점부터 끊긴 18초까지


def test_백엔드를_재시작해도_클립과_메타데이터가_유지된다(make_app, client, clock):
    from fastapi.testclient import TestClient
    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, audio.noise_with_bursts(FLOOR, 25.0, [(12.0, 1.0, LOUD)]))
    before = events(client)
    with TestClient(make_app()) as c2:
        assert events(c2) == before
        assert len(read_wav(c2, before[0]["id"])) > 0
