import hmac
import os
import time
from collections import defaultdict
from typing import Annotated

from fastapi import Header, HTTPException, Request

from app.config import get_settings

_WEAK_TOKENS = frozenset(
    {
        "changeme",
        "password",
        "admin",
        "admin123",
        "dlmodel",
        "token",
        "12345678",
    }
)

# Simple in-memory rate limit for auth failures (per process).
_fail_buckets: dict[str, list[float]] = defaultdict(list)
_FAIL_WINDOW_SEC = 60.0
_FAIL_LIMIT = 20
_MAX_BUCKETS = 10_000


def assert_admin_token_safe() -> None:
    """Refuse insecure defaults unless explicitly allowed (tests/dev)."""
    if os.environ.get("ALLOW_INSECURE_ADMIN", "").strip() in {"1", "true", "yes"}:
        return
    token = (get_settings().admin_token or "").strip()
    if len(token) < 12:
        raise RuntimeError(
            "ADMIN_TOKEN 过短（至少 12 位）。测试环境可设置 ALLOW_INSECURE_ADMIN=1。"
        )
    if token.lower() in _WEAK_TOKENS:
        raise RuntimeError(
            "ADMIN_TOKEN 使用了不安全的默认值，请在 .env 中设置强随机令牌。"
        )


def _client_key(request: Request) -> str:
    # Bucket by peer address. Anything the caller chooses (such as the token it
    # presents) would hand every guess a fresh bucket. Behind a reverse proxy,
    # uvicorn fills this from X-Forwarded-For for proxies in FORWARDED_ALLOW_IPS.
    return request.client.host if request.client else "unknown"


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    recent = [ts for ts in _fail_buckets.get(key, ()) if now - ts < _FAIL_WINDOW_SEC]
    if recent:
        _fail_buckets[key] = recent
    else:
        _fail_buckets.pop(key, None)
    return len(recent) >= _FAIL_LIMIT


def _record_failure(key: str) -> None:
    now = time.monotonic()
    if key not in _fail_buckets and len(_fail_buckets) >= _MAX_BUCKETS:
        # Bound memory under an address spray: drop buckets that have expired.
        for stale in [
            k for k, v in _fail_buckets.items() if now - v[-1] >= _FAIL_WINDOW_SEC
        ]:
            del _fail_buckets[stale]
    _fail_buckets[key].append(now)


async def require_admin(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    key = _client_key(request)
    if _rate_limited(key):
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
    if not authorization or not authorization.startswith("Bearer "):
        _record_failure(key)
        raise HTTPException(status_code=401, detail="缺少 Bearer 令牌")
    token = authorization.removeprefix("Bearer ").strip()
    expected = get_settings().admin_token
    if not token or not hmac.compare_digest(token, expected):
        _record_failure(key)
        raise HTTPException(status_code=401, detail="令牌无效")
