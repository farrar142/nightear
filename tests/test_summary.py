"""아침 Discord 요약: 가짜 알림기와 가짜 시계로 판단·본문·중복 방지를 검증한다(실제 발송 없음)."""
import asyncio
from datetime import datetime

from fastapi.testclient import TestClient

from nightear import summary

from .support.clock import KST

HOOK = "https://discord.invalid/api/webhooks/test"


def at(*args):
    return datetime(*args, tzinfo=KST).timestamp()


def check(client):
    return asyncio.run(client.app.state.summary.check())


def notifier(client):
    return client.app.state.summary.notifier


def configure(client, **extra):
    client.put("/api/settings", json={"discord_webhook_url": HOOK, **extra})


def add_event(client, start_ts, peak_db, labels=()):
    store = client.app.state.events

    async def go():
        ev = await store.start(source_id="s", start_ts=start_ts, end_ts=start_ts + 1,
                               peak_db=peak_db, baseline_db=-60)
        await store.finish(ev["id"], end_ts=start_ts + 1, peak_db=peak_db)
        if labels:
            store.set_labels(ev["id"], list(labels))

    asyncio.run(go())


def add_sleep(client, start_ts, end_ts):
    client.app.state.db.execute("INSERT INTO sleep_sessions(start_ts, end_ts) VALUES(?, ?)", (start_ts, end_ts))


def add_gap(client, start_ts, end_ts):
    client.app.state.db.execute("INSERT INTO capture_gaps(start_ts, end_ts) VALUES(?, ?)", (start_ts, end_ts))


def test_밤이_끝나면_요약이_한_번_발송된다(client, clock):
    configure(client, dashboard_base_url="http://pc.local:8000/")
    add_event(client, at(2025, 1, 16, 1, 0), -30, [("Snoring", 0.9)])
    add_event(client, at(2025, 1, 16, 3, 12), -12.3, [("Door", 0.8)])
    add_event(client, at(2025, 1, 16, 4, 0), -40, [("Snoring", 0.7)])
    clock.set(datetime(2025, 1, 16, 11, 59, tzinfo=KST))
    assert check(client) == [] and notifier(client).sent == []  # 아직 밤 중
    clock.set(datetime(2025, 1, 16, 12, 1, tzinfo=KST))
    assert check(client) == ["2025-01-15"]
    [text] = notifier(client).sent
    assert "2025-01-15" in text
    assert "3건" in text
    assert "Snoring 2건" in text and "Door 1건" in text
    assert "03:12" in text  # 가장 큰 소리 시각(로컬 시간대)
    assert "http://pc.local:8000/#night=2025-01-15" in text
    clock.advance(minutes=5)
    assert check(client) == []
    assert len(notifier(client).sent) == 1  # 한 밤에 한 통


def test_이벤트가_없으면_조용한_밤_요약(client, clock):
    configure(client)
    add_sleep(client, at(2025, 1, 15, 23, 0), at(2025, 1, 16, 7, 0))
    clock.set(datetime(2025, 1, 16, 12, 5, tzinfo=KST))
    assert check(client) == ["2025-01-15"]
    [text] = notifier(client).sent
    assert "조용한 밤" in text
    assert "종류별" not in text


def test_수집_공백이_있으면_N분간_기록_없음(client, clock):
    configure(client)
    add_event(client, at(2025, 1, 16, 2, 0), -30)
    add_gap(client, at(2025, 1, 16, 3, 0), at(2025, 1, 16, 3, 25))
    clock.set(datetime(2025, 1, 16, 12, 5, tzinfo=KST))
    check(client)
    [text] = notifier(client).sent
    assert "25분간 기록 없음" in text
    assert "분류 안 됨 1건" in text


def test_웹훅이_없으면_보내지_않고_나중에_설정하면_보낸다(client, clock):
    add_event(client, at(2025, 1, 16, 2, 0), -30)
    clock.set(datetime(2025, 1, 16, 12, 5, tzinfo=KST))
    assert check(client) == [] and notifier(client).sent == []
    configure(client)
    assert check(client) == ["2025-01-15"]


def test_수면_모드_해제_후_유예가_지나면_밤이_끝나기_전에도_발송(client, clock):
    configure(client)
    add_event(client, at(2025, 1, 16, 2, 0), -30)
    add_sleep(client, at(2025, 1, 15, 23, 0), at(2025, 1, 16, 7, 0))
    clock.set(datetime(2025, 1, 16, 7, 29, tzinfo=KST))
    assert check(client) == []
    clock.set(datetime(2025, 1, 16, 7, 31, tzinfo=KST))
    assert check(client) == ["2025-01-15"]


def test_유예_안에_재진입하면_기다리고_밤_중_여러_번_켜고_꺼도_한_통(client, clock):
    configure(client)
    add_event(client, at(2025, 1, 16, 2, 0), -30)
    add_sleep(client, at(2025, 1, 15, 23, 0), at(2025, 1, 16, 1, 0))
    clock.set(datetime(2025, 1, 16, 1, 10, tzinfo=KST))
    assert check(client) == []  # 해제 직후: 유예 중
    # 재진입(진행 중 구간) -> 유예 만료 시각이 지나도 보내지 않는다
    client.app.state.db.execute("INSERT INTO sleep_sessions(start_ts) VALUES(?)", (at(2025, 1, 16, 1, 20),))
    clock.set(datetime(2025, 1, 16, 2, 0, tzinfo=KST))
    assert check(client) == []
    client.app.state.db.execute("UPDATE sleep_sessions SET end_ts = ? WHERE end_ts IS NULL", (at(2025, 1, 16, 6, 0),))
    clock.set(datetime(2025, 1, 16, 6, 40, tzinfo=KST))
    assert check(client) == ["2025-01-15"]
    clock.set(datetime(2025, 1, 16, 12, 30, tzinfo=KST))
    assert check(client) == []
    assert len(notifier(client).sent) == 1


def test_재시작해도_같은_밤을_다시_보내지_않는다(make_app, clock):
    with TestClient(make_app()) as c:
        configure(c)
        add_event(c, at(2025, 1, 16, 2, 0), -30)
        clock.set(datetime(2025, 1, 16, 12, 5, tzinfo=KST))
        assert check(c) == ["2025-01-15"]
    with TestClient(make_app()) as c:
        assert check(c) == []
        assert notifier(c).sent == []


def test_발송이_실패하면_기록하지_않고_다음에_재시도한다(client, clock):
    configure(client)
    add_event(client, at(2025, 1, 16, 2, 0), -30)
    clock.set(datetime(2025, 1, 16, 12, 5, tzinfo=KST))
    notifier(client).fail = True
    assert check(client) == []
    notifier(client).fail = False
    assert check(client) == ["2025-01-15"]


def test_데이터가_전혀_없는_밤은_보내지_않는다(client, clock):
    configure(client)
    clock.set(datetime(2025, 1, 16, 12, 5, tzinfo=KST))
    assert check(client) == []


def test_설정_기본값과_검증(client):
    s = client.get("/api/settings").json()
    assert s["discord_webhook_url"] == "" and s["summary_grace_minutes"] == 30 and s["dashboard_base_url"] == ""
    assert client.put("/api/settings", json={"summary_grace_minutes": -1}).status_code == 422


def test_기본_알림기는_URL이_없으면_아무것도_보내지_않는다(monkeypatch):
    called = []
    monkeypatch.setattr(summary.DiscordWebhookNotifier, "_post", staticmethod(lambda *a: called.append(a)))

    class S:
        def get(self, k):
            return ""

    asyncio.run(summary.DiscordWebhookNotifier(S()).send("x"))
    assert called == []
