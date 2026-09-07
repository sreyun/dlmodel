"""Direct HTTP download with Range resume and transient SSL retries."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from app.downloaders.base import LogCallback, ProgressCallback
from app.downloaders.network import is_transient_network_error, retry_backoff_seconds

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0)
_LIMITS = httpx.Limits(max_keepalive_connections=4, max_connections=8, keepalive_expiry=30.0)


def _parse_total(resp: httpx.Response, *, existing: int, resumed: bool) -> int | None:
    content_range = resp.headers.get("content-range")
    if content_range and "/" in content_range:
        try:
            return int(content_range.rsplit("/", 1)[-1])
        except ValueError:
            pass
    content_length = resp.headers.get("content-length")
    if not content_length:
        return None
    try:
        length = int(content_length)
    except ValueError:
        return None
    return existing + length if resumed else length


async def _http_download_once(
    url: str,
    dest: Path,
    on_progress: ProgressCallback,
    headers: dict[str, str] | None = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing = dest.stat().st_size if dest.exists() and dest.is_file() else 0
    req_headers = dict(headers or {})
    if existing > 0:
        req_headers["Range"] = f"bytes={existing}-"

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=_TIMEOUT,
        max_redirects=5,
        limits=_LIMITS,
        http2=False,
    ) as client:
        async with client.stream("GET", url, headers=req_headers) as resp:
            if resp.status_code == 416 and existing > 0:
                await on_progress(existing, existing, 0.0)
                return

            if existing > 0 and resp.status_code == 206:
                mode = "ab"
                downloaded = existing
                resumed = True
            elif resp.status_code == 200:
                mode = "wb"
                downloaded = 0
                resumed = False
            else:
                resp.raise_for_status()
                mode = "wb"
                downloaded = 0
                resumed = False

            total = _parse_total(resp, existing=existing if resumed else 0, resumed=resumed)
            last_time = time.monotonic()
            last_bytes = downloaded

            with dest.open(mode) as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    speed: float | None = None
                    if now - last_time >= 0.5:
                        speed = (downloaded - last_bytes) / (now - last_time)
                        last_time = now
                        last_bytes = downloaded
                    await on_progress(downloaded, total, speed)

    await on_progress(downloaded, total, 0.0)


async def http_download(
    url: str,
    dest: Path,
    on_progress: ProgressCallback,
    headers: dict[str, str] | None = None,
    *,
    retries: int = 3,
    on_log: LogCallback | None = None,
) -> None:
    """Download with Range resume; retry transient TLS/network failures."""
    attempts = max(1, int(retries) + 1)
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            await _http_download_once(url, dest, on_progress, headers=headers)
            return
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts - 1 or not is_transient_network_error(exc):
                raise
            delay = retry_backoff_seconds(attempt)
            msg = (
                f"网络中断（{exc}），{delay:.0f}s 后重试 "
                f"（{attempt + 1}/{attempts - 1}）…"
            )
            logger.warning(
                "http_download retry url=%s attempt=%s err=%s", url, attempt + 1, exc
            )
            if on_log is not None:
                await on_log(msg)
            await asyncio.sleep(delay)
    if last_exc is not None:
        raise last_exc
