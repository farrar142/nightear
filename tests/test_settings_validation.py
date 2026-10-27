import json

import pytest


@pytest.mark.parametrize(
    "bad",
    [
        {"idle_threshold_minutes": 20.5},  # 정수 설정에 비정수 실수
        {"night_start_hour": 24},
        {"night_end_hour": -1},
        {"baseline_window_minutes": 0},
        {"retention_days": 0},
        {"max_clip_s": 0},
        {"trigger_min_duration_s": -0.1},
        {"absolute_floor_db": 5},
        {"trigger_margin_db": float("inf")},
        {"timezone": "Mars/Olympus"},
        {"timezone": 3},
    ],
)
def test_범위와_형식을_벗어난_설정은_422(client, bad):
    r = client.put("/api/settings", content=json.dumps(bad), headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_정수_설정은_정수값_실수를_받고_실수_설정은_정수를_받는다(client):
    s = client.put("/api/settings", json={"idle_threshold_minutes": 30.0, "trigger_margin_db": 8}).json()
    assert s["idle_threshold_minutes"] == 30
    assert s["trigger_margin_db"] == 8


def test_유효한_타임존은_받는다(client):
    assert client.put("/api/settings", json={"timezone": "UTC"}).status_code == 200
