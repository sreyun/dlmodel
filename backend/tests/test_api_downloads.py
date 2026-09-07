import time

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

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
    assert "hf_token" not in body
    assert body["hf_token_set"] is False
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
    assert body["hf_token_set"] is False
    assert body["modelscope_api_token_set"] is False
    assert "hf_token" not in body


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
        assert body["hf_token_set"] is True
        assert body["modelscope_api_token_set"] is True
        again = client.get("/api/settings", headers=headers).json()
        assert again["hf_token_set"] is True
        assert again["modelscope_api_token_set"] is True


def test_clear_hf_token_removes_sqlite_and_env_poison(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    app = create_app()
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        client.put(
            "/api/settings",
            headers=headers,
            json={"hf_token": "sqlite-hf-token"},
        )
        assert client.get("/api/settings", headers=headers).json()["hf_token_set"] is True
        cleared = client.put(
            "/api/settings",
            headers=headers,
            json={"clear_hf_token": True},
        ).json()
        assert cleared["hf_token_set"] is False


def test_notify_webhook_settings_masked_and_validated(client):
    headers = {"Authorization": "Bearer secret"}
    bad = client.put(
        "/api/settings",
        headers=headers,
        json={"notify_dingtalk_webhook": "https://evil.example/hook"},
    )
    assert bad.status_code == 400
    ok = client.put(
        "/api/settings",
        headers=headers,
        json={
            "notify_dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=abc",
            "notify_on_completed": True,
            "notify_on_failed": False,
            "notify_on_started": True,
            "notify_on_cancelled": False,
        },
    )
    assert ok.status_code == 200
    body = ok.json()
    assert "notify_dingtalk_webhook" not in body
    assert body["notify_dingtalk_webhook_set"] is True
    assert body["notify_on_completed"] is True
    assert body["notify_on_failed"] is False
    assert body["notify_on_started"] is True
    assert body["notify_on_cancelled"] is False
    cleared = client.put(
        "/api/settings",
        headers=headers,
        json={"clear_notify_dingtalk_webhook": True},
    ).json()
    assert cleared["notify_dingtalk_webhook_set"] is False


@respx.mock
def test_notify_test_posts_configured_webhooks(client):
    route = respx.post("https://oapi.dingtalk.com/robot/send").mock(
        return_value=Response(200, json={"errcode": 0})
    )
    headers = {"Authorization": "Bearer secret"}
    client.put(
        "/api/settings",
        headers=headers,
        json={
            "notify_dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=abc",
        },
    )
    r = client.post(
        "/api/settings/notify-test",
        headers=headers,
        json={"channel": "dingtalk"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["results"]["dingtalk"] == "ok"
    assert route.called
    missing = client.post(
        "/api/settings/notify-test",
        headers=headers,
        json={"channel": "feishu"},
    )
    assert missing.status_code == 400


@respx.mock
def test_notify_test_accepts_unsaved_form_webhook(client):
    route = respx.post("https://oapi.dingtalk.com/robot/send").mock(
        return_value=Response(200, json={"errcode": 0})
    )
    headers = {"Authorization": "Bearer secret"}
    r = client.post(
        "/api/settings/notify-test",
        headers=headers,
        json={
            "channel": "dingtalk",
            "notify_dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=unsaved",
        },
    )
    assert r.status_code == 200
    assert r.json()["results"]["dingtalk"] == "ok"
    assert route.called
    # Unsaved override must not persist into settings.
    settings = client.get("/api/settings", headers=headers).json()
    assert settings["notify_dingtalk_webhook_set"] is False


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


def test_events_requires_bearer_not_query_token(client):
    headers = {"Authorization": "Bearer secret"}
    r = client.post(
        "/api/downloads",
        headers=headers,
        json={"name": "a/b", "source": "huggingface", "target": "vllm"},
    )
    tid = r.json()["id"]
    denied = client.get(f"/api/downloads/{tid}/events")
    assert denied.status_code == 401
    query_only = client.get(f"/api/downloads/{tid}/events?token=secret")
    assert query_only.status_code == 401
    ev = client.get(f"/api/downloads/{tid}/events", headers=headers)
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


def test_delete_terminal_task(client):
    headers = {"Authorization": "Bearer secret"}
    r = client.post(
        "/api/downloads",
        headers=headers,
        json={"name": "del/me", "source": "huggingface", "target": "vllm"},
    )
    tid = r.json()["id"]
    row = _wait_status(client, tid, headers, "completed", "failed")
    assert row["status"] in ("completed", "failed")
    deleted = client.delete(f"/api/downloads/{tid}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    missing = client.get(f"/api/downloads/{tid}", headers=headers)
    assert missing.status_code == 404


def test_delete_rejects_running_task(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    app = create_app()
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        started = asyncio.Event()

        async def fake_run(task, on_progress, on_log, cancelled):
            started.set()
            await cancelled.wait()

        client.app.state.queue._run_download = fake_run  # type: ignore[method-assign]
        r = client.post(
            "/api/downloads",
            headers=headers,
            json={"name": "keep/running", "source": "huggingface", "target": "vllm"},
        )
        tid = r.json()["id"]
        for _ in range(50):
            row = client.get(f"/api/downloads/{tid}", headers=headers).json()
            if row["status"] == "running":
                break
            time.sleep(0.05)
        denied = client.delete(f"/api/downloads/{tid}", headers=headers)
        assert denied.status_code == 400
        client.post(f"/api/downloads/{tid}/cancel", headers=headers)


def test_cleanup_completed_tasks(client):
    headers = {"Authorization": "Bearer secret"}
    ids = []
    for name in ("c/a", "c/b"):
        r = client.post(
            "/api/downloads",
            headers=headers,
            json={"name": name, "source": "huggingface", "target": "vllm"},
        )
        tid = r.json()["id"]
        _wait_status(client, tid, headers, "completed", "failed")
        ids.append(tid)
    cleaned = client.post(
        "/api/downloads/cleanup",
        headers=headers,
        json={"statuses": ["completed", "failed"]},
    )
    assert cleaned.status_code == 200
    assert cleaned.json()["deleted"] >= 1
    for tid in ids:
        assert client.get(f"/api/downloads/{tid}", headers=headers).status_code == 404
