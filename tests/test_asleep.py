"""「자는 동안」 추정: 수면 모드 구간 + 라벨 흐름 -> 밤 상세·이벤트 목록·통계·아침 요약."""
import asyncio
from datetime import datetime

from .support.clock import KST
from .test_summary import add_event, add_sleep, at, check, configure, notifier

NIGHT = "2025-01-15"


def every(client, start, minutes, label, step_min=2):
    """start 부터 minutes 분 동안 step_min 분마다 1순위가 label 인 이벤트."""
    for i in range(0, minutes, step_min):
        add_event(client, start + i * 60, -40, [(label, 0.9)])


def night(client):
    return client.get(f"/api/nights/{NIGHT}").json()


def test_말소리_뒤에_숨소리가_이어지면_그때_잠든_것으로_본다(client, clock):
    add_sleep(client, at(2025, 1, 15, 23, 0), at(2025, 1, 16, 8, 0))
    every(client, at(2025, 1, 15, 23, 10), 120, "Speech")  # 23:10~01:10 폰·영상
    every(client, at(2025, 1, 16, 2, 0), 60, "Snoring")  # 02:00~ 코골이
    clock.set(datetime(2025, 1, 16, 12, 30, tzinfo=KST))
    d = night(client)
    assert d["asleep"]["estimated"] is True
    assert d["asleep"]["asleep_ts"] == at(2025, 1, 16, 2, 0)
    assert d["asleep"]["intervals"] == [{"start_ts": at(2025, 1, 16, 2, 0), "end_ts": at(2025, 1, 16, 8, 0)}]
    assert d["summary"]["asleep_event_count"] == 30
    assert d["summary"]["asleep_seconds"] == 6 * 3600
    assert [e["asleep"] for e in d["events"]].count(True) == 30
    assert all(not e["asleep"] for e in d["events"] if e["labels"][0]["label"] == "Speech")


def test_잠든_뒤_말소리가_이어지면_깨어_있음_구간으로_뺀다(client, clock):
    add_sleep(client, at(2025, 1, 15, 23, 0), at(2025, 1, 16, 10, 0))
    every(client, at(2025, 1, 16, 1, 0), 60, "Breathing")
    every(client, at(2025, 1, 16, 4, 0), 30, "Speech")  # 새벽에 잠깐 깼다
    every(client, at(2025, 1, 16, 5, 0), 60, "Snoring")
    clock.set(datetime(2025, 1, 16, 12, 30, tzinfo=KST))
    assert night(client)["asleep"]["intervals"] == [
        {"start_ts": at(2025, 1, 16, 1, 0), "end_ts": at(2025, 1, 16, 4, 0)},
        {"start_ts": at(2025, 1, 16, 5, 0), "end_ts": at(2025, 1, 16, 10, 0)},
    ]


def test_드문_말소리나_숨소리는_구간을_바꾸지_않는다(client, clock):
    add_sleep(client, at(2025, 1, 15, 23, 0), at(2025, 1, 16, 8, 0))
    every(client, at(2025, 1, 16, 1, 0), 60, "Snoring")
    every(client, at(2025, 1, 16, 3, 0), 20, "Speech", step_min=5)  # 20분에 4건: 잠꼬대 정도
    every(client, at(2025, 1, 16, 4, 0), 60, "Snoring")
    clock.set(datetime(2025, 1, 16, 12, 30, tzinfo=KST))
    assert night(client)["asleep"]["intervals"] == [
        {"start_ts": at(2025, 1, 16, 1, 0), "end_ts": at(2025, 1, 16, 8, 0)}]


def test_숨소리가_없는_밤은_추정_불가로_수면_모드_전체를_쓴다(client, clock):
    add_sleep(client, at(2025, 1, 15, 23, 0), at(2025, 1, 16, 8, 0))
    every(client, at(2025, 1, 16, 1, 0), 60, "Speech")
    clock.set(datetime(2025, 1, 16, 12, 30, tzinfo=KST))
    d = night(client)
    assert d["asleep"]["estimated"] is False and d["asleep"]["asleep_ts"] is None
    assert d["asleep"]["intervals"] == [{"start_ts": at(2025, 1, 15, 23, 0), "end_ts": at(2025, 1, 16, 8, 0)}]
    assert d["summary"]["asleep_event_count"] == 30
    nights = {n["id"]: n for n in client.get("/api/nights").json()["nights"]}
    assert nights[NIGHT]["summary"]["asleep_estimated"] is False


def test_이벤트_목록과_통계도_자는_동안을_따른다(client, clock):
    add_sleep(client, at(2025, 1, 15, 23, 0), at(2025, 1, 16, 8, 0))
    every(client, at(2025, 1, 15, 23, 10), 60, "Speech")
    every(client, at(2025, 1, 16, 2, 0), 20, "Snoring")
    add_event(client, at(2025, 1, 16, 15, 0), -40, [("Door", 0.9)])  # 낮(밤 창 밖)
    clock.set(datetime(2025, 1, 16, 16, 0, tzinfo=KST))
    evs = client.get("/api/events").json()
    assert sum(e["asleep"] for e in evs) == 10 and len(evs) == 41
    all_ = client.get("/api/stats").json()
    only = client.get("/api/stats", params={"asleep": True}).json()
    assert all_["total"] == 41 and all_["asleep"] is False
    assert only["total"] == 10 and only["by_label"] == [{"label": "Snoring", "count": 10}]


def test_아침_요약은_잠든_시각과_자는_동안_이벤트만_센다(client, clock):
    configure(client)
    add_sleep(client, at(2025, 1, 15, 23, 0), at(2025, 1, 16, 8, 0))
    every(client, at(2025, 1, 15, 23, 10), 60, "Speech")
    every(client, at(2025, 1, 16, 2, 0), 20, "Snoring")
    clock.set(datetime(2025, 1, 16, 12, 5, tzinfo=KST))
    assert check(client) == [NIGHT]
    [text] = notifier(client).sent
    assert "잠든 시각(추정) 02:00, 자는 동안 6시간" in text
    assert "자는 동안 소리 이벤트 10건" in text
    assert "Snoring 10건" in text and "Speech" not in text
