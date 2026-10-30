"""로컬 AI 라벨링: 가짜 분류기를 주입하고 수집 WS -> 실시간/HTTP 로만 검증한다."""
import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from nightear import labeling

from .support import audio
from .support.classifier import FakeClassifier
from .support.clients import ingest, realtime
from .support.clock import KST

FLOOR = -50.0
LOUD = -35.0


def burst_clip_audio(floor_s=15.0, burst_s=1.0, tail_s=20.0):
    return audio.noise_with_bursts(FLOOR, floor_s + burst_s + tail_s, [(floor_s, burst_s, LOUD)])


def feed(src, rt, pcm):
    src.send_pcm(pcm)
    n_frames = len(pcm) // 3200
    got, levels = [], 0
    while levels < n_frames:
        msg = rt.ws.receive_json()
        got.append(msg)
        if msg["type"] == "level":
            levels += 1
    return got


def events(client, **params):
    return client.get("/api/events", params=params).json()


def wait_label(rt, status, seen=()):
    """이미 받은 메시지(seen)에 없으면 실시간 채널에서 해당 상태의 label 메시지를 기다린다."""
    match = lambda m: m["type"] == "label" and m["event"]["label_status"] == status
    for m in seen:
        if match(m):
            return m
    return rt.receive_until(match)


def make_client(make_app, fake):
    return TestClient(make_app(classifier=fake))


def test_클립이_저장되면_비동기로_라벨이_붙고_실시간으로_알려진다(make_app):
    fake = FakeClassifier()
    with make_client(make_app, fake) as client, realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_clip_audio())
        msg = wait_label(rt, "done", msgs)
    labels = msg["event"]["labels"]
    assert [(l["label"], l["score"]) for l in labels] == [("Door", 0.82), ("Footsteps", 0.64), ("Knock", 0.31)]
    ev = msg["event"]
    assert ev["label_status"] == "done" and ev["has_clip"] is True
    assert fake.calls and fake.calls[0][1] == 16000  # 클립 오디오(16kHz)가 분류기에 전달됐다


def test_분류기에는_클립_전체가_아니라_이벤트_구간만_넘어간다(make_app):
    fake = FakeClassifier()
    with make_client(make_app, fake) as client, realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_clip_audio())
        ev = wait_label(rt, "done", msgs)["event"]
    [pcm] = fake.pcms
    seconds = len(pcm) / 2 / 16000
    assert seconds == pytest.approx(ev["duration_s"] + 2 * labeling.SEGMENT_MARGIN_S, abs=0.11)
    assert seconds < ev["clip_duration_s"] - 10  # 앞 pre_roll(10초)의 조용한 구간이 빠졌다
    # 넘어간 구간의 대부분이 버스트(LOUD)다: 잡음 바닥(FLOOR)보다 훨씬 크다
    assert audio.rms_dbfs(pcm) > LOUD - 6  # 클립 전체(16초 중 1초)였다면 약 LOUD - 12


def test_짧은_이벤트도_YAMNet_한_창_이상은_넘긴다(make_app):
    fake = FakeClassifier()
    pcm = audio.noise_with_bursts(FLOOR, 36.0, [(15.0, 0.3, LOUD)])
    with make_client(make_app, fake) as client, realtime(client) as rt, ingest(client) as src:
        wait_label(rt, "done", feed(src, rt, pcm))
    assert len(fake.pcms[0]) / 2 / 16000 >= labeling.MIN_SEGMENT_S - 0.01


def test_Silence는_라벨_순위에서_빠진다(make_app):
    fake = FakeClassifier(results=[("Silence", 1.0), ("Snoring", 0.7), ("Speech", 0.5), ("Door", 0.2)])
    with make_client(make_app, fake) as client, realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_clip_audio())
        msg = wait_label(rt, "done", msgs)
    assert [l["label"] for l in msg["event"]["labels"]] == ["Snoring", "Speech", "Door"]


def test_이미_라벨이_붙은_클립을_다시_라벨링한다(make_app):
    fake = FakeClassifier(results=[("Silence", 1.0), ("Speech", 0.5)])
    with make_client(make_app, fake) as client, realtime(client) as rt, ingest(client) as src:
        wait_label(rt, "done", feed(src, rt, burst_clip_audio()))
        fake.results = [("Snoring", 0.9), ("Breathing", 0.4)]
        assert client.post("/api/labels/relabel").json() == {"queued": 1}
        wait_label(rt, "pending")
        msg = wait_label(rt, "done")
        assert [l["label"] for l in events(client)[0]["labels"]] == ["Snoring", "Breathing"]
    assert [l["label"] for l in msg["event"]["labels"]] == ["Snoring", "Breathing"]
    assert len(fake.pcms) == 2


def test_다시_라벨링은_기간을_고를_수_있다(make_app):
    with make_client(make_app, FakeClassifier()) as client, realtime(client) as rt, ingest(client) as src:
        wait_label(rt, "done", feed(src, rt, burst_clip_audio()))
        [ev] = events(client)
        assert client.post("/api/labels/relabel", params={"since": ev["start_ts"] + 1}).json() == {"queued": 0}
        assert client.post("/api/labels/relabel", params={"until": ev["start_ts"] + 1}).json() == {"queued": 1}


def test_이벤트_목록에_상위_라벨_후보가_보인다(make_app):
    with make_client(make_app, FakeClassifier()) as client, realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_clip_audio())
        wait_label(rt, "done", msgs)
        [ev] = events(client)
    assert ev["label_status"] == "done"
    assert [l["label"] for l in ev["labels"]] == ["Door", "Footsteps", "Knock"]
    assert ev["labels"][0]["score"] == pytest.approx(0.82)


def test_후보_개수는_설정으로_바꾼다(make_app):
    with make_client(make_app, FakeClassifier()) as client, realtime(client) as rt, ingest(client) as src:
        client.put("/api/settings", json={"label_top_k": 2})
        msgs = feed(src, rt, burst_clip_audio())
        msg = wait_label(rt, "done", msgs)
    assert [l["label"] for l in msg["event"]["labels"]] == ["Door", "Footsteps"]


def test_라벨_필터가_동작한다(make_app):
    fake = FakeClassifier(results=[("Door", 0.9)])
    with make_client(make_app, fake) as client, realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_clip_audio())
        wait_label(rt, "done", msgs)
        fake.results = [("Speech", 0.7), ("Knock", 0.2)]
        feed(src, rt, audio.noise_with_bursts(FLOOR, 17.0, [(0.0, 1.0, LOUD)]) + audio.noise(FLOOR, 20.0, seed=3))
        deadline = time.time() + 5
        while time.time() < deadline and len([e for e in events(client) if e["label_status"] == "done"]) < 2:
            time.sleep(0.05)
        assert len(events(client)) == 2
        assert [e["labels"][0]["label"] for e in events(client, label="Door")] == ["Door"]
        assert [e["labels"][0]["label"] for e in events(client, label="Knock")] == ["Speech"]  # 2순위 후보도 일치
        assert len(events(client, label=["Door", "Speech"])) == 2
        assert events(client, label="Cat") == []
        counts = {c["label"]: c["count"] for c in client.get("/api/labels").json()}
        assert counts == {"Door": 1, "Speech": 1, "Knock": 1}


def test_분류기가_실패하면_failed_이고_클립은_남는다(make_app):
    with make_client(make_app, FakeClassifier(fail=True)) as client, realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_clip_audio())
        msg = wait_label(rt, "failed", msgs)
        [ev] = events(client)
        assert client.get(f"/api/events/{ev['id']}/clip").status_code == 200
    assert msg["event"]["labels"] == []
    assert ev["label_status"] == "failed" and ev["has_clip"] is True


def test_라벨링_중인_클립도_목록에_보인다(make_app):
    gate = threading.Event()
    with make_client(make_app, FakeClassifier(gate=gate)) as client, realtime(client) as rt, ingest(client) as src:
        try:
            msgs = feed(src, rt, burst_clip_audio())
            wait_label(rt, "pending", msgs)
            [ev] = events(client)
            assert ev["label_status"] == "pending" and ev["labels"] == [] and ev["has_clip"] is True
        finally:
            gate.set()
        wait_label(rt, "done", msgs)
        assert events(client)[0]["label_status"] == "done"


def test_수면_모드가_아니면_라벨링하지_않는다(make_app, clock):
    clock.set(datetime(2025, 1, 15, 14, 0, 0, tzinfo=KST))
    fake = FakeClassifier()
    with make_client(make_app, fake) as client, realtime(client) as rt, ingest(client) as src:
        msgs = feed(src, rt, burst_clip_audio())
        [ev] = events(client)
    assert ev["has_clip"] is False and ev["label_status"] is None and ev["labels"] == []
    assert fake.calls == []
