import pytest
from fastapi.testclient import TestClient
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    app = create_app()
    with TestClient(app) as c:
        async def fake_run(task, on_progress, on_log, cancelled):
            await on_progress(100, 100, 0)

        app.state.queue._run_download = fake_run  # type: ignore[method-assign]
        yield c


def test_create_download_requires_auth(client):
    r = client.post("/api/downloads", json={"name": "a/b", "source": "huggingface", "target": "vllm"})
    assert r.status_code == 401


def test_create_download_ok(client, monkeypatch):
    # patch queue enqueue to no-op real download: already fake in test by short-circuit if needed
    headers = {"Authorization": "Bearer secret"}
    r = client.post("/api/downloads", headers=headers, json={"name": "a/b", "source": "huggingface", "target": "vllm"})
    assert r.status_code == 200
    assert "id" in r.json()
    tid = r.json()["id"]
    r2 = client.get(f"/api/downloads/{tid}", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["name"] == "a/b"


def test_settings_merge_env_and_sqlite(client):
    headers = {"Authorization": "Bearer secret"}
    r = client.get("/api/settings", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["hf_endpoint"] == "https://hf-mirror.com"
    assert body["download_concurrency"] == 2
    assert body["aria2_connections"] == 16
    r = client.put(
        "/api/settings",
        headers=headers,
        json={"hf_endpoint": "https://example.test", "download_concurrency": 4},
    )
    assert r.status_code == 200
    r = client.get("/api/settings", headers=headers)
    merged = r.json()
    assert merged["hf_endpoint"] == "https://example.test"
    assert merged["download_concurrency"] == 4
    assert merged["aria2_connections"] == 16
    assert merged["ollama_base_url"] == "http://ollama:11434"
    assert merged["vllm_base_url"] == "http://vllm:8000"


def test_events_accepts_token_query(client):
    headers = {"Authorization": "Bearer secret"}
    r = client.post(
        "/api/downloads",
        headers=headers,
        json={"name": "a/b", "source": "huggingface", "target": "vllm"},
    )
    tid = r.json()["id"]
    denied = client.get(f"/api/downloads/{tid}/events")
    assert denied.status_code == 401
    ev = client.get(f"/api/downloads/{tid}/events?token=secret")
    assert ev.status_code == 200
    assert ev.headers["content-type"].startswith("text/event-stream")
    assert "data: " in ev.text
    assert "status" in ev.text
