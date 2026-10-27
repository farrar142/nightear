from fastapi.testclient import TestClient


def test_기본값을_조회할_수_있다(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    s = r.json()
    assert s["night_start_hour"] == 22
    assert s["night_end_hour"] == 12
    assert s["idle_threshold_minutes"] == 20
    assert s["baseline_window_minutes"] == 5
    assert s["trigger_margin_db"] == 10
    assert s["trigger_min_duration_s"] == 0.2
    assert s["merge_window_s"] == 10
    assert s["timezone"] == "Asia/Seoul"
    assert s["discord_webhook_url"] == ""


def test_수정하면_조회에_반영되고_나머지는_유지된다(client):
    r = client.put("/api/settings", json={"trigger_margin_db": 12.5})
    assert r.status_code == 200
    assert r.json()["trigger_margin_db"] == 12.5
    s = client.get("/api/settings").json()
    assert s["trigger_margin_db"] == 12.5
    assert s["idle_threshold_minutes"] == 20


def test_백엔드를_재시작해도_설정이_유지된다(make_app):
    with TestClient(make_app()) as c1:
        c1.put("/api/settings", json={"idle_threshold_minutes": 30, "discord_webhook_url": "https://x/y"})
    with TestClient(make_app()) as c2:
        s = c2.get("/api/settings").json()
    assert s["idle_threshold_minutes"] == 30
    assert s["discord_webhook_url"] == "https://x/y"


def test_알_수_없는_키나_잘못된_타입은_거부된다(client):
    assert client.put("/api/settings", json={"nope": 1}).status_code == 422
    assert client.put("/api/settings", json={"trigger_margin_db": "큼"}).status_code == 422
    assert client.put("/api/settings", json={"idle_threshold_minutes": True}).status_code == 422
    # 거부된 요청은 아무것도 바꾸지 않는다
    assert client.get("/api/settings").json()["trigger_margin_db"] == 10
