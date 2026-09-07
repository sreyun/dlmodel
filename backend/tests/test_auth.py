import os
from fastapi.testclient import TestClient


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
