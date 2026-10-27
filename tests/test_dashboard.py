def test_대시보드가_루트에서_서빙된다(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "nightear" in r.text


def test_API_경로가_정적_파일에_가려지지_않는다(client):
    assert client.get("/api/settings").headers["content-type"].startswith("application/json")
    assert client.get("/app.js").status_code == 200
    assert client.get("/capture-worklet.js").status_code == 200
