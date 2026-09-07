"""Classify transient TLS/network failures for download retries."""

from __future__ import annotations

import ssl
from typing import Any

import httpx

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

_MSG_NEEDLES = (
    "unexpected_eof",
    "eof occurred in violation of protocol",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "ssl",
    "tls",
    "remote end closed",
    "server disconnected",
    "incomplete read",
    "connection refused",
    "name or service not known",
    "temporary failure",
    "network is unreachable",
)


def _message_looks_transient(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(needle in text for needle in _MSG_NEEDLES)


def is_transient_network_error(exc: BaseException | None) -> bool:
    """Return True for SSL/EOF/reset/timeout style failures worth retrying."""
    if exc is None:
        return False

    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.TimeoutException,
            httpx.PoolTimeout,
            ssl.SSLError,
            ConnectionError,
            TimeoutError,
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
        ),
    ):
        return True

    if requests is not None and isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.Timeout,
            requests.exceptions.SSLError,
        ),
    ):
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code if exc.response is not None else 0
        return code in {408, 425, 429, 500, 502, 503, 504}

    if _message_looks_transient(exc):
        return True

    for attr in ("__cause__", "__context__"):
        nested = getattr(exc, attr, None)
        if nested is not None and nested is not exc:
            if is_transient_network_error(nested):
                return True

    # httpx wraps OSError/ssl in .request / args sometimes
    args: tuple[Any, ...] = getattr(exc, "args", ())
    for arg in args:
        if isinstance(arg, BaseException) and arg is not exc:
            if is_transient_network_error(arg):
                return True

    return False


def retry_backoff_seconds(attempt: int, *, base: float = 1.5, cap: float = 30.0) -> float:
    """Exponential backoff for attempt index starting at 0."""
    delay = base * (2 ** max(0, attempt))
    return min(cap, delay)
