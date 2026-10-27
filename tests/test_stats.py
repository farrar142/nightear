"""패턴 통계: 여러 날에 걸친 합성 이벤트를 수집 WS로 만들고 통계 API로 검증한다."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from nightear.clips import delete_clip

from .support import audio
from .support.classifier import FakeClassifier
from .support.clients import ingest, realtime
from .support.clock import KST

FLOOR, LOUD = -50.0, -35.0


def night(client, rt, fake, when, label):
    """when(KST) 시각에 수집을 시작해 6초 뒤 1초짜리 이벤트를 만든다(수면 모드 -> 클립 -> 라벨)."""
    client.app.state.clock.set(when)
    fake.results = [(label, 0.9)]
    pcm = audio.noise_with_bursts(FLOOR, 12.0, [(6.0, 1.0, LOUD)])
    with ingest(client) as src:
        src.send_pcm(pcm)
        n = len(pcm) // 3200
        done, labeled = 0, False
        while done < n:
            m = rt.ws.receive_json()
            done += m["type"] == "level"
            labeled |= m["type"] == "label" and m["event"]["label_status"] == "done"
    if not labeled:
        rt.receive_until(lambda m: m["type"] == "label" and m["event"]["label_status"] == "done")


@pytest.fixture
def stats_client(make_app):
    fake = FakeClassifier()
    with TestClient(make_app(classifier=fake)) as client, realtime(client) as rt:
        client.put("/api/settings", json={"merge_window_s": 1.0, "post_roll_s": 1.0})
        # 2025-01-15 / 22 는 수요일, 16 은 목요일
        night(client, rt, fake, datetime(2025, 1, 15, 23, 0, 0, tzinfo=KST), "Door")
        night(client, rt, fake, datetime(2025, 1, 16, 3, 0, 0, tzinfo=KST), "Door")
        night(client, rt, fake, datetime(2025, 1, 22, 23, 0, 0, tzinfo=KST), "Speech")
        yield client


def test_시간대_요일_라벨별_빈도를_집계한다(stats_client):
    s = stats_client.get("/api/stats").json()
    assert s["total"] == 3
    assert s["by_hour"][23] == 2 and s["by_hour"][3] == 1 and sum(s["by_hour"]) == 3
    assert s["by_weekday"][2] == 2 and s["by_weekday"][3] == 1 and sum(s["by_weekday"]) == 3  # 수, 목
    assert s["heatmap"][2][23] == 2 and s["heatmap"][3][3] == 1
    assert s["by_label"] == [{"label": "Door", "count": 2}, {"label": "Speech", "count": 1}]
    assert s["unlabeled"] == 0


def test_기간으로_제한한다(stats_client):
    since = datetime(2025, 1, 16, 0, 0, tzinfo=KST).timestamp()
    until = datetime(2025, 1, 20, 0, 0, tzinfo=KST).timestamp()
    s = stats_client.get("/api/stats", params={"since": since, "until": until}).json()
    assert s["total"] == 1 and s["heatmap"][3][3] == 1
    assert s["by_label"] == [{"label": "Door", "count": 1}]
    only_after = stats_client.get("/api/stats", params={"since": until}).json()
    assert only_after["total"] == 1 and only_after["by_label"] == [{"label": "Speech", "count": 1}]


def test_클립이_삭제된_이벤트도_통계에_포함된다(stats_client):
    events = stats_client.get("/api/events").json()
    assert all(e["has_clip"] for e in events)
    for e in events:
        assert delete_clip(stats_client.app.state, e["id"])
    after = stats_client.get("/api/events").json()
    assert all(e["audio_deleted"] and not e["has_clip"] for e in after)
    assert [e["labels"][0]["label"] for e in after if e["labels"]]  # 라벨 메타데이터도 남는다
    s = stats_client.get("/api/stats").json()
    assert s["total"] == 3 and s["by_label"][0] == {"label": "Door", "count": 2}


def test_시간대_설정을_따른다(stats_client):
    stats_client.put("/api/settings", json={"timezone": "UTC"})
    s = stats_client.get("/api/stats").json()
    assert s["timezone"] == "UTC"
    assert s["by_hour"][14] == 2 and s["by_hour"][18] == 1  # KST 23시 = UTC 14시, KST 03시 = 전날 UTC 18시
    assert s["by_weekday"][2] == 3  # UTC로는 03시 이벤트가 수요일로 넘어간다


def test_데이터가_없으면_0으로_채운다(client):
    s = client.get("/api/stats").json()
    assert s["total"] == 0 and s["by_hour"] == [0] * 24 and s["by_weekday"] == [0] * 7
    assert len(s["heatmap"]) == 7 and s["by_label"] == []
