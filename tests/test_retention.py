"""별표와 보관 정리: 가짜 시계로 30일을 흘려 비별표 클립만 지워지는지 본다."""
from fastapi.testclient import TestClient

from .support import audio
from .support.clients import ingest, realtime
from .test_clips import FLOOR, LOUD, events, feed, set_settings


def two_clip_events(client):
    set_settings(client, merge_window_s=2.0)
    with realtime(client) as rt, ingest(client) as src:
        feed(src, rt, audio.noise_with_bursts(FLOOR, 60.0, [(15.0, 1.0, LOUD), (45.0, 1.0, LOUD)]))
    evs = sorted(events(client), key=lambda e: e["start_ts"])
    assert len(evs) == 2 and all(e["has_clip"] for e in evs)
    return evs


def star(client, event_id, starred=True):
    return client.put(f"/api/events/{event_id}/star", json={"starred": starred})


def find(client, event_id):
    return next(e for e in events(client) if e["id"] == event_id)


def test_별표_켜기_끄기_API(client):
    a, _ = two_clip_events(client)
    assert a["starred"] is False
    r = star(client, a["id"])
    assert r.status_code == 200 and r.json()["starred"] is True
    assert find(client, a["id"])["starred"] is True
    assert star(client, a["id"], False).json()["starred"] is False
    assert star(client, 9999).status_code == 404


def test_30일_지나면_비별표_클립만_지워지고_메타데이터는_남는다(client, clock):
    a, b = two_clip_events(client)
    star(client, b["id"])
    clock.advance(days=31)
    assert client.app.state.retention.run_once() == 1
    after = {e["id"]: e for e in events(client)}
    assert after[a["id"]]["has_clip"] is False and after[a["id"]]["audio_deleted"] is True
    assert client.get(f"/api/events/{a['id']}/clip").status_code == 404
    assert not (client.app.state.clips_dir / f"event-{a['id']}.wav").exists()
    # 메타데이터 유지
    assert after[a["id"]]["start_ts"] == a["start_ts"] and after[a["id"]]["peak_db"] == a["peak_db"]
    assert after[a["id"]]["labels"] == a["labels"]
    # 별표 클립은 유지
    assert after[b["id"]]["has_clip"] is True and after[b["id"]]["audio_deleted"] is False
    assert client.get(f"/api/events/{b['id']}/clip").status_code == 200
    # 다시 돌려도 더 지울 것이 없다
    assert client.app.state.retention.run_once() == 0


def test_보관_기간_안의_클립은_지우지_않는다(client, clock):
    two_clip_events(client)
    clock.advance(days=29)
    assert client.app.state.retention.run_once() == 0
    assert all(e["has_clip"] for e in events(client))


def test_별표를_끄면_다음_정리에서_지워진다(client, clock):
    a, _ = two_clip_events(client)
    star(client, a["id"])
    clock.advance(days=40)
    client.app.state.retention.run_once()
    assert find(client, a["id"])["has_clip"] is True
    star(client, a["id"], False)
    client.app.state.retention.run_once()
    assert find(client, a["id"])["audio_deleted"] is True


def test_보관_일수_설정을_따른다(client, clock):
    two_clip_events(client)
    set_settings(client, retention_days=7)
    clock.advance(days=8)
    assert client.app.state.retention.run_once() == 2


def test_별표는_재시작_뒤에도_남는다(make_app, client):
    a, _ = two_clip_events(client)
    star(client, a["id"])
    with TestClient(make_app()) as c2:
        assert find(c2, a["id"])["starred"] is True
