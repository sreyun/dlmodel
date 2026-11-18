import time
from types import SimpleNamespace

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


def _fail(client, token):
    return client.post("/api/auth/verify", headers={"Authorization": f"Bearer {token}"})


def test_rate_limit_is_per_client_not_per_presented_token(monkeypatch):
    """每次换一个 token 不能绕过限速：桶按客户端地址划分。"""
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    from app import auth
    from app.main import create_app

    app = create_app()
    attacker = TestClient(app, client=("203.0.113.9", 50000))
    for i in range(auth._FAIL_LIMIT):
        assert _fail(attacker, f"guess-{i}").status_code == 401
    assert _fail(attacker, "yet-another-guess").status_code == 429
    # 限速期间即使猜中也不放行，否则 429 就成了“猜错”的同义词。
    assert _fail(attacker, "secret").status_code == 429

    other = TestClient(app, client=("198.51.100.7", 50000))
    assert _fail(other, "secret").status_code == 200


def test_rate_limit_window_expires(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    from app import auth
    from app.main import create_app

    client = TestClient(create_app())
    for i in range(auth._FAIL_LIMIT):
        _fail(client, f"guess-{i}")
    assert _fail(client, "secret").status_code == 429

    later = time.monotonic() + auth._FAIL_WINDOW_SEC + 1
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: later))
    assert _fail(client, "secret").status_code == 200
    assert auth._fail_buckets == {}
