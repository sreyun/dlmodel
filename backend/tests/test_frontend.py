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
        assert "模型下载管理" in body
        assert 'id="app"' in body
        assert "#/download" in body
        assert "#/tasks" in body
        assert "#/library" in body
        assert "#/services" in body
        assert "#/settings" in body
        assert "下载" in body
        assert "任务" in body
        assert "模型库" in body
        assert "设置" in body
        assert "skip-link" in body
        assert "Content-Security-Policy" in body
        assert 'lang="zh-CN"' in body
        assert "Noto+Sans+SC" in body or "IBM+Plex+Sans" in body or "Noto Sans SC" in body
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
        assert "clear_hf_token" in js.text
        assert "hf_token_set" in js.text
        assert "stillOn" in js.text
        assert "set-msg" in js.text
        assert "login-token" in js.text
        assert "task-list" in js.text
        assert "progress-fill" in js.text
        assert "taskCard" in js.text
        assert "pairWarning" in js.text
        assert 'class="btn' in js.text
        css = client.get("/styles.css")
        assert css.status_code == 200
        assert "progress-track" in css.text
        assert "focus-visible" in css.text
        assert css.text.strip()


def test_api_not_shadowed_by_static(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        denied = client.get("/api/downloads")
        assert denied.status_code == 401
        assert "text/html" not in denied.headers.get("content-type", "")
        ok = client.post("/api/auth/verify", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
        assert ok.json()["ok"] is True
