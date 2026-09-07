import time

import pytest
from fastapi.testclient import TestClient
from app.main import create_app


def _wait_status(client, tid, headers, *statuses, attempts=50):
    row = None
    for _ in range(attempts):
        row = client.get(f"/api/downloads/{tid}", headers=headers).json()
        if row.get("status") in statuses:
            return row
        time.sleep(0.05)
    return row


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


def test_empty_hf_token_env_exposed_as_null(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv("HF_TOKEN", "")
    monkeypatch.setenv("MODELSCOPE_API_TOKEN", "  ")
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/api/settings", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["hf_token"] is None
    assert body["modelscope_api_token"] is None


def test_blank_token_put_does_not_shadow_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv("HF_TOKEN", "env-hf-token")
    monkeypatch.setenv("MODELSCOPE_API_TOKEN", "env-ms-token")
    app = create_app()
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        r = client.put(
            "/api/settings",
            headers=headers,
            json={
                "hf_endpoint": "https://example.test",
                "hf_token": "",
                "modelscope_api_token": "   ",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["hf_endpoint"] == "https://example.test"
        assert body["hf_token"] == "env-hf-token"
        assert body["modelscope_api_token"] == "env-ms-token"
        again = client.get("/api/settings", headers=headers).json()
        assert again["hf_token"] == "env-hf-token"
        assert again["modelscope_api_token"] == "env-ms-token"


def test_create_download_rejects_traversal(client):
    headers = {"Authorization": "Bearer secret"}
    r = client.post(
        "/api/downloads",
        headers=headers,
        json={"name": "foo/../../../tmp/x", "source": "huggingface", "target": "vllm"},
    )
    assert r.status_code == 400
    assert "路径穿越" in r.json()["detail"] or "拒绝" in r.json()["detail"]


@pytest.mark.asyncio
async def test_empty_sqlite_int_does_not_break_settings(tmp_path, monkeypatch):
    from app.db import init_db, set_setting
    from app.routes.settings import effective_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv("DOWNLOAD_CONCURRENCY", "2")
    await init_db(str(tmp_path / "app.db"))
    await set_setting("download_concurrency", "")
    await set_setting("aria2_connections", "not-a-number")
    merged = await effective_settings()
    assert merged["download_concurrency"] == 2
    assert isinstance(merged["aria2_connections"], int)


@pytest.mark.asyncio
async def test_empty_sqlite_token_does_not_override_env(tmp_path, monkeypatch):
    import os

    from app.db import init_db, set_setting
    from app.routes.settings import apply_sqlite_overrides, effective_settings

    monkeypatch.setenv("HF_TOKEN", "env-hf-token")
    monkeypatch.setenv("MODELSCOPE_API_TOKEN", "env-ms-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    await init_db(str(tmp_path / "app.db"))
    await set_setting("hf_token", "")
    await set_setting("modelscope_api_token", "  ")
    await apply_sqlite_overrides()
    assert os.environ["HF_TOKEN"] == "env-hf-token"
    assert os.environ["MODELSCOPE_API_TOKEN"] == "env-ms-token"
    merged = await effective_settings()
    assert merged["hf_token"] == "env-hf-token"
    assert merged["modelscope_api_token"] == "env-ms-token"


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


def test_retry_rejects_completed(client):
    headers = {"Authorization": "Bearer secret"}
    r = client.post(
        "/api/downloads",
        headers=headers,
        json={"name": "a/b", "source": "huggingface", "target": "vllm"},
    )
    tid = r.json()["id"]
    row = _wait_status(client, tid, headers, "completed", "failed")
    assert row["status"] == "completed"
    retry = client.post(f"/api/downloads/{tid}/retry", headers=headers)
    assert retry.status_code == 400
    detail = retry.json()["detail"]
    assert "重试" in detail or "completed" in detail


def test_cancel_rejects_completed(client):
    headers = {"Authorization": "Bearer secret"}
    r = client.post(
        "/api/downloads",
        headers=headers,
        json={"name": "a/b", "source": "huggingface", "target": "vllm"},
    )
    tid = r.json()["id"]
    row = _wait_status(client, tid, headers, "completed", "failed")
    assert row["status"] == "completed"
    cancelled = client.post(f"/api/downloads/{tid}/cancel", headers=headers)
    assert cancelled.status_code == 400
    assert cancelled.json()["detail"]
