def test_대시보드가_루트에서_서빙된다(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "nightear" in r.text


def test_API_경로가_정적_파일에_가려지지_않는다(client):
    assert client.get("/api/settings").headers["content-type"].startswith("application/json")
    assert client.get("/app.js").status_code == 200
    assert client.get("/capture-worklet.js").status_code == 200


def test_클립_플레이어_스크립트가_서빙된다(client):
    assert client.get("/clip-player.js").status_code == 200
    assert "clip-panel" in client.get("/").text


def test_그래프_스크립트가_서빙된다(client):
    assert client.get("/graph.js").status_code == 200
    assert "graph" in client.get("/").text


def test_통계_스크립트가_서빙된다(client):
    assert client.get("/stats.js").status_code == 200
    assert "stats-heat" in client.get("/").text


def test_타임라인_스크립트가_서빙되고_연결된다(client):
    assert client.get("/timeline.js").status_code == 200
    assert "timeline.js" in client.get("/app.js").text
    assert "tl-canvas" in client.get("/").text
