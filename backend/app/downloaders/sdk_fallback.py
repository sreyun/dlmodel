"""Direct HTTP download with Range resume and transient SSL retries."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from app.aria2_client import aria2_control_path
from app.downloaders.base import DownloadControl, LogCallback, ProgressCallback
from app.downloaders.network import is_transient_network_error, retry_backoff_seconds
from app.paths import disk_error_to_message

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0)
_LIMITS = httpx.Limits(max_keepalive_connections=4, max_connections=8, keepalive_expiry=30.0)


def _disk_error(exc: OSError, dest: Path) -> BaseException:
    """Turn ENOSPC / EACCES write failures into a friendly, path-hinted error."""
    message = disk_error_to_message(exc, dest)
    return RuntimeError(message) if message else exc


def _range_total(resp: httpx.Response) -> int | None:
    """Full length from a ``Content-Range: bytes */N`` (or ``a-b/N``) header."""
    content_range = resp.headers.get("content-range") or ""
    if "/" not in content_range:
        return None
    try:
        return int(content_range.rsplit("/", 1)[-1])
    except ValueError:
        return None


def _parse_total(resp: httpx.Response, *, existing: int, resumed: bool) -> int | None:
    range_total = _range_total(resp)
    if range_total is not None:
        return range_total
    content_length = resp.headers.get("content-length")
    if not content_length:
        return None
    try:
        length = int(content_length)
    except ValueError:
        return None
    return existing + length if resumed else length


class _LocalFileMismatch(Exception):
    """The local file is not a prefix of the remote one (longer than it)."""


def discard_aria2_partial(dest: Path) -> bool:
    """Drop a file aria2 left unfinished; True when something was discarded.

    Range resume appends at the current length, which is only right for a file
    written front to back. An aria2 partial has holes in the middle, so resuming
    it here would keep the holes and still end at the expected size. Starting the
    file over is the only correct continuation without aria2.
    """
    control = aria2_control_path(dest)
    if not control.exists():
        return False
    dest.unlink(missing_ok=True)
    control.unlink(missing_ok=True)
    return True


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
                remote_total = _range_total(resp)
                if remote_total is not None and remote_total != existing:
                    raise _LocalFileMismatch()
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

            # Buffer and write off the event loop so a multi-GB download does not
            # block other workers / request handlers on synchronous per-chunk I/O.
            buffer = bytearray()
            flush_at = 256 * 1024
            try:
                with dest.open(mode) as f:
                    async for chunk in resp.aiter_bytes():
                        buffer += chunk
                        downloaded += len(chunk)
                        if len(buffer) >= flush_at:
                            await asyncio.to_thread(f.write, bytes(buffer))
                            buffer.clear()
                        now = time.monotonic()
                        speed: float | None = None
                        if now - last_time >= 0.5:
                            speed = (downloaded - last_bytes) / (now - last_time)
                            last_time = now
                            last_bytes = downloaded
                        await on_progress(downloaded, total, speed)
                    if buffer:
                        await asyncio.to_thread(f.write, bytes(buffer))
                        buffer.clear()
            except OSError as exc:
                raise _disk_error(exc, dest) from exc

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
    if discard_aria2_partial(dest) and on_log is not None:
        await on_log(f"{dest.name} 是 aria2 未完成的分片文件，HTTP 无法接续，改为从头下载")
    attempts = max(1, int(retries) + 1)
    attempt = 0
    restarted = False
    while True:
        try:
            await _http_download_once(url, dest, on_progress, headers=headers)
            return
        except DownloadControl:
            # cancel / pause: propagate immediately, never retried.
            raise
        except _LocalFileMismatch:
            if restarted:
                raise RuntimeError(f"{dest.name} 与远端文件长度不一致") from None
            # Left over from another revision: not resumable, and not a network
            # failure either, so it does not use up a retry.
            restarted = True
            dest.unlink(missing_ok=True)
            if on_log is not None:
                await on_log(f"{dest.name} 比远端文件长，改为从头下载")
        except Exception as exc:
            if attempt >= attempts - 1 or not is_transient_network_error(exc):
                raise
            delay = retry_backoff_seconds(attempt)
            msg = (
                f"网络中断（{exc}），{delay:.0f}s 后重试 "
                f"（第 {attempt + 2}/{attempts} 次尝试）…"
            )
            logger.warning(
                "http_download retry url=%s attempt=%s err=%s", url, attempt + 1, exc
            )
            if on_log is not None:
                await on_log(msg)
            await asyncio.sleep(delay)
            attempt += 1
