"""밤 타임라인: 레벨 기록(1초 다운샘플)·밤 목록·밤 상세 API."""
from datetime import datetime

from fastapi.testclient import TestClient

from nightear.nights import night_id_for, night_window

from .support import audio
from .support.clients import ingest
from .support.clock import KST

CFG = {"timezone": "Asia/Seoul", "night_start_hour": 22, "night_end_hour": 12}


def ts(*args):
    return datetime(*args, tzinfo=KST).timestamp()


def wait_gap_open(client):
    """연결 종료가 서버에서 처리되어 공백이 열릴 때까지 기다린다(시계를 움직이기 전에)."""
    for _ in range(2000):
        if client.get("/api/gaps").json()["gaps"]:
            return
    raise AssertionError("공백이 열리지 않음")


def test_밤_창과_id():
    assert night_window("2025-01-15", CFG) == (ts(2025, 1, 15, 22), ts(2025, 1, 16, 12))
    assert night_id_for(ts(2025, 1, 16, 3), CFG) == "2025-01-15"
    assert night_id_for(ts(2025, 1, 15, 23), CFG) == "2025-01-15"
    assert night_id_for(ts(2025, 1, 16, 14), CFG) is None


def test_레벨은_초당_max와_mean으로_저장된다(client, clock):
    with ingest(client) as src:
        # 첫 1초 = 100ms 프레임 10개: 9개는 -50, 1개는 -30. 둘째 초는 -50.
        src.send_pcm(audio.tone(-50, 0.9) + audio.tone(-30, 0.1) + audio.tone(-50, 1.0))
    d = client.get("/api/nights/2025-01-15").json()
    cols, rows = d["levels"]["columns"], d["levels"]["rows"]
    assert cols == ["ts", "max_db", "mean_db"]
    assert len(rows) == 2
    first = dict(zip(cols, rows[0]))
    assert first["ts"] == ts(2025, 1, 15, 23)
    assert abs(first["max_db"] + 30) < 0.5
    assert -50 < first["mean_db"] < -45
    assert abs(rows[1][1] + 50) < 0.5


def test_레벨_기록은_재시작_후에도_남는다(make_app):
    with TestClient(make_app()) as c:
        with ingest(c) as src:
            src.send_pcm(audio.tone(-40, 3.0))
    with TestClient(make_app()) as c:
        rows = c.get("/api/nights/2025-01-15").json()["levels"]["rows"]
    assert len(rows) == 3


def test_합성_하룻밤_상세(client, clock):
    with ingest(client, idle=True) as src:
        src.send_activity("idle", 20 * 60)  # 23:00 유휴 20분 도달 → 수면 모드 진입
        src.send_pcm(audio.tone(-60, 60.0))
        src.send_pcm(audio.tone(-15, 2.0))  # 큰 소리 → 이벤트
        src.send_pcm(audio.tone(-60, 30.0))
    wait_gap_open(client)
    clock.advance(minutes=10)  # 수집이 끊긴 10분
    with ingest(client, idle=True) as src:
        src.send_activity("active", 0)  # 수면 해제
        src.send_pcm(audio.tone(-55, 3.0))
    d = client.get("/api/nights/2025-01-15").json()
    assert d["id"] == "2025-01-15"
    assert (d["start_ts"], d["end_ts"]) == night_window("2025-01-15", CFG)
    assert d["levels"]["rows"]
    assert len(d["sleep_intervals"]) == 1
    sl = d["sleep_intervals"][0]
    assert sl["start_ts"] >= d["start_ts"] and sl["end_ts"] > sl["start_ts"]
    assert len(d["gaps"]) == 1
    gap = d["gaps"][0]
    assert 590 <= gap["end_ts"] - gap["start_ts"] <= 610
    assert d["summary"]["gap_seconds"] == gap["end_ts"] - gap["start_ts"]
    assert d["summary"]["event_count"] == len(d["events"]) >= 1
    ev = d["events"][0]
    assert d["start_ts"] <= ev["start_ts"] < d["end_ts"]
    assert max(r[1] for r in d["levels"]["rows"]) > -20  # 큰 소리 구간이 max로 보존됨
    assert d["summary"]["sleep_seconds"] > 0


def test_다른_밤의_데이터는_잘려_나간다(client, clock):
    with ingest(client) as src:
        src.send_pcm(audio.tone(-40, 2.0))
    wait_gap_open(client)
    clock.set(datetime(2025, 1, 16, 23, 0, tzinfo=KST))
    with ingest(client) as src:
        src.send_pcm(audio.tone(-40, 4.0))
    a = client.get("/api/nights/2025-01-15").json()
    b = client.get("/api/nights/2025-01-16").json()
    assert len(a["levels"]["rows"]) == 2
    assert len(b["levels"]["rows"]) == 4
    # 두 밤에 걸친 공백은 각 밤 창 안으로 잘린다
    assert all(g["end_ts"] <= a["end_ts"] for g in a["gaps"])
    assert all(g["start_ts"] >= b["start_ts"] for g in b["gaps"])
    assert a["gaps"], "첫 밤 끝까지 이어진 공백"


def test_밤_목록은_데이터_있는_밤만_최신순(client, clock):
    assert client.get("/api/nights").json() == {"nights": []}
    with ingest(client) as src:
        src.send_pcm(audio.tone(-40, 2.0))
    clock.set(datetime(2025, 1, 17, 1, 0, tzinfo=KST))  # 1/16 밤
    with ingest(client) as src:
        src.send_pcm(audio.tone(-40, 2.0))
    nights = client.get("/api/nights").json()["nights"]
    ids = [n["id"] for n in nights]
    assert ids[0] == "2025-01-16" and "2025-01-15" in ids
    assert ids == sorted(ids, reverse=True)
    n15 = next(n for n in nights if n["id"] == "2025-01-15")
    assert n15["summary"]["level_seconds"] == 2


def test_잘못된_밤_id는_404(client):
    assert client.get("/api/nights/abc").status_code == 404
    assert client.get("/api/nights/2025-13-45").status_code == 404


def test_프레임_처리와_조회가_동시에_일어나도_초가_유실되지_않는다(client):
    """스레드풀의 조회(flush_all)와 이벤트 루프의 프레임 처리가 LevelHistory 상태를 경쟁하지 않는다."""
    import threading
    from nightear.frames import Frame

    hist = client.app.state.history
    base = ts(2025, 1, 15, 23)
    stop = threading.Event()
    errors = []

    def reader():
        try:
            while not stop.is_set():
                hist.range(base, base + 4000)
                hist.summary(base, base + 4000)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=reader)
    t.start()
    for i in range(3000):  # 300초 분량 프레임
        hist(Frame("s", base + i * 0.1, -40.0, b""))
    stop.set()
    t.join()
    assert not errors
    assert hist.summary(base, base + 4000)[0] == 300


def test_밤_목록_요약은_상세와_일치한다(client):
    with ingest(client) as src:
        src.send_pcm(audio.tone(-40, 2.0) + audio.tone(-10, 1.0))
    listed = client.get("/api/nights").json()["nights"][0]["summary"]
    detail = client.get("/api/nights/2025-01-15").json()["summary"]
    assert listed == detail
