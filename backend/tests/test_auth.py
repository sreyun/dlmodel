import os
from fastapi.testclient import TestClient


def test_health_ok_without_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("ALLOW_INSECURE_ADMIN", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    from app.main import create_app

    with TestClient(create_app()) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_verify_ok(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    from app.main import create_app
    client = TestClient(create_app())
    r = client.post("/api/auth/verify", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200


def test_verify_rejects_bad_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    from app.main import create_app
    client = TestClient(create_app())
    r = client.post("/api/auth/verify", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
