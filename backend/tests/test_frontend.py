from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    from app.main import create_app

    return TestClient(create_app())


def test_spa_index_served(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        body = r.text
        assert "dlmodel" in body
        assert 'id="app"' in body
        assert "#/download" in body
        assert "#/tasks" in body
        assert "#/library" in body
        assert "#/services" in body
        assert "#/settings" in body
        assert 'src="/app.js"' in body or 'src="app.js"' in body


def test_spa_assets_use_token_and_task_poll(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        js = client.get("/app.js")
        assert js.status_code == 200
        assert "dlmodel_token" in js.text
        assert "/api/auth/verify" in js.text
        assert "/api/downloads" in js.text
        assert "1000" in js.text
        assert "if (hfToken) body.hf_token = hfToken" in js.text
        assert "if (msToken) body.modelscope_api_token = msToken" in js.text
        css = client.get("/styles.css")
        assert css.status_code == 200
        assert css.text.strip()


def test_api_not_shadowed_by_static(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        denied = client.get("/api/downloads")
        assert denied.status_code == 401
        assert "text/html" not in denied.headers.get("content-type", "")
        ok = client.post("/api/auth/verify", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
        assert ok.json()["ok"] is True
