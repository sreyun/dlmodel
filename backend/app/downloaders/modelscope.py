import asyncio
import logging
import time
from pathlib import Path
from typing import Awaitable

import requests

from modelscope import snapshot_download
from modelscope.hub.api import HubApi

from app.config import get_settings, optional_secret
from app.downloaders.base import DownloadControl, LogCallback, ProgressCallback
from app.downloaders.network import is_transient_network_error, retry_backoff_seconds
from app.downloaders.progress import dir_size_bytes, format_bytes

logger = logging.getLogger(__name__)

# Kept as a name in this module because it reads naturally next to the SDK call.
_dir_size_bytes = dir_size_bytes


def _int_size(value: object) -> int | None:
    try:
        size = int(value)  # accepts int and the numeric strings some APIs return
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


async def ms_repo_file_sizes(
    name: str, token: str | None, revision: str | None = None
) -> dict[str, int]:
    """``filename -> bytes`` for a ModelScope repo, {} when the API won't say.

    ``snapshot_download`` runs in a blocking thread that offers no callbacks, so
    the only live signal we have is bytes-on-disk — which needs a denominator
    from the repo listing or the bar can never show a percentage.
    """
    api = HubApi(token=optional_secret(token))
    try:
        files = await asyncio.to_thread(
            api.get_model_files, model_id=name, revision=revision, recursive=True
        )
    except Exception as exc:  # unreachable mirror / private repo / schema drift
        logger.debug("modelscope size probe failed name=%s err=%s", name, exc)
        return {}
    sizes: dict[str, int] = {}
    try:
        entries = list(files or [])
    except TypeError:  # unexpected payload shape: no sizes rather than no download
        return sizes
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("Path") or entry.get("path")
        size = _int_size(entry.get("Size", entry.get("size")))
        if isinstance(path, str) and size is not None:
            sizes[path] = size
    return sizes


async def ms_repo_exists(name: str, token: str | None) -> bool:
    api = HubApi(token=optional_secret(token))
    try:
        return await asyncio.to_thread(
            api.repo_exists, repo_id=name, repo_type="model", re_raise=True
        )
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return False
        raise


async def _drain_thread_task(task: asyncio.Task) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# Strong refs for fire-and-forget detach drainers (asyncio only keeps weak
# refs; a collected task would leak the caller's target-dir claim forever).
_detach_tasks: set[asyncio.Task] = set()


async def _download_modelscope_once(
    name: str,
    dest: Path,
    *,
    token: str | None,
    revision: str | None,
    on_progress: ProgressCallback,
    on_log: LogCallback,
    on_detached=None,
    expected_total: dict | int = 0,
) -> None:
    """One snapshot_download attempt.

    ``on_detached(still_writing)`` is called exactly once per propagating
    exception: ``True`` when the blocking thread was spawned and may still be
    writing (the real release follows when it drains), ``False`` when no thread
    exists or it already finished. Every failure path — including control
    errors raised *before* the thread spawns — must reach one of those calls,
    or the caller's target-dir claim leaks and every later resume/retry is
    wrongly rejected.

    ``expected_total`` is either a byte count or a ``{"total": int}`` holder the
    caller fills in from a background size listing, so a denominator learned while
    the transfer is already running still reaches the bar.
    """
    dest = Path(dest)
    download_task: asyncio.Task | None = None

    def expected_bytes() -> int:
        if isinstance(expected_total, dict):
            return int(expected_total.get("total") or 0)
        return int(expected_total or 0)

    def report(size: int, speed: float | None) -> Awaitable[None]:
        # Bytes on disk can outrun the listing (extra config files, a revision
        # mismatch): widening the denominator keeps the ratio at <= 100%. Without a
        # probed listing there is no honest denominator at all, and passing ``size``
        # would fake a permanent 100% — stay indeterminate instead.
        known = expected_bytes()
        return on_progress(size, max(known, size) if known else None, speed)

    try:
        dest.mkdir(parents=True, exist_ok=True)
        # This initial report can raise DownloadCancelled/DownloadPaused (the
        # user paused before the thread existed): nothing to detach, release
        # immediately — the historical leak this guards against.
        await report(_dir_size_bytes(dest), None)

        def _download() -> str:
            return snapshot_download(
                model_id=name,
                revision=revision,
                local_dir=str(dest),
                token=optional_secret(token),
            )

        download_task = asyncio.create_task(asyncio.to_thread(_download))
        last_size = _dir_size_bytes(dest)
        last_ts = time.monotonic()
        last_log_ts = last_ts
        scan_delay = 0.8

        while not download_task.done():
            await asyncio.sleep(scan_delay)
            started = time.monotonic()
            size = _dir_size_bytes(dest)
            # A 200 GB repo is tens of thousands of files and the SDK gives no
            # callback, so walking the tree *is* the progress source. When the
            # walk costs real time, stretch the interval instead of burning the
            # loop on it — otherwise I/O competes with the download and the
            # measured speed is the scan cost, not the transfer rate.
            scan_delay = min(5.0, max(0.8, (time.monotonic() - started) * 4))
            now = time.monotonic()
            elapsed = max(now - last_ts, 0.001)
            speed = (size - last_size) / elapsed if size >= last_size else None
            await report(size, float(speed) if speed and speed > 0 else None)
            if size != last_size and (now - last_log_ts) >= 3.0:
                known = expected_bytes()
                suffix = f" / {format_bytes(known)}" if known else ""
                await on_log(f"ModelScope 已下载 {format_bytes(size)}{suffix}…")
                last_log_ts = now
            last_size = size
            last_ts = now
        result = await download_task
    except BaseException:
        pending = download_task is not None and not download_task.done()
        if pending:
            # Mark the claim as thread-still-writing *synchronously*, before
            # yielding: the queue's terminal/paused cleanup may run in this
            # same exception propagation and must see the detached marker.
            if on_detached is not None:
                on_detached(True)

            async def _detach() -> None:
                await _drain_thread_task(download_task)
                if on_detached is not None:
                    on_detached(False)

            detacher = asyncio.create_task(_detach())
            _detach_tasks.add(detacher)
            detacher.add_done_callback(_detach_tasks.discard)
        elif on_detached is not None:
            on_detached(False)
        raise

    final_size = _dir_size_bytes(dest)
    await report(final_size, 0.0)
    await on_log(f"ModelScope 下载完成：{result}")


async def _probe_sizes(
    name: str,
    token: str | None,
    revision: str | None,
    *,
    attempts: int = 3,
    delay: float = 2.0,
) -> dict[str, int]:
    """Size listing with a hard deadline and a few quiet retries.

    The listing is the only denominator a blocking SDK download can have, and the
    usual reason it comes back empty is a transient mirror timeout — not a repo
    without sizes. Retrying is free here (the transfer already runs in parallel and
    absorbs whatever lands), and a late denominator still turns a permanently
    unknown bar into a real percentage a few seconds in.
    """
    total_attempts = max(1, int(attempts))
    for attempt in range(total_attempts):
        try:
            sizes = await asyncio.wait_for(
                ms_repo_file_sizes(name, token, revision), timeout=10.0
            )
        except Exception as exc:
            logger.debug(
                "modelscope size probe failed name=%s attempt=%s err=%s",
                name,
                attempt + 1,
                exc,
            )
            sizes = {}
        if sizes:
            return sizes
        if attempt + 1 < total_attempts:
            await asyncio.sleep(delay)
    return {}


async def download_modelscope(
    name: str,
    dest: Path,
    *,
    token: str | None,
    revision: str | None,
    on_progress: ProgressCallback,
    on_log: LogCallback,
    on_detached=None,
    retries: int | None = None,
) -> None:
    if retries is None:
        retries = max(0, int(get_settings().download_retries))
    attempts = max(1, int(retries) + 1)
    # The listing is what buys the denominator, but an unreachable mirror must not
    # hold the download hostage for the probe's timeout: wait a short grace period
    # (the common case answers well under a second), otherwise keep probing in the
    # background and let the bar gain its percentage mid-flight.
    expected = {"total": 0, "files": 0}
    probe = asyncio.create_task(_probe_sizes(name, token, revision))

    def _absorb(task: asyncio.Task) -> None:
        try:
            sizes = task.result()
        except (asyncio.CancelledError, Exception):
            return
        expected["total"] = sum(sizes.values())
        expected["files"] = len(sizes)

    try:
        await asyncio.wait_for(asyncio.shield(probe), timeout=2.0)
    except asyncio.TimeoutError:
        probe.add_done_callback(_absorb)
    except Exception:
        pass
    else:
        _absorb(probe)

    try:
        message = f"正在从 ModelScope 下载 {name}…"
        if expected["total"]:
            message = (
                f"正在从 ModelScope 下载 {name}"
                f"（{expected['files']} 个文件，"
                f"预计 {format_bytes(expected['total'])}）…"
            )
        await on_log(message)
    except BaseException:
        if not probe.done():
            probe.cancel()
        # Pause/cancel landing on this log line happens before any thread
        # exists: release now instead of leaking the caller's target-dir claim.
        if on_detached is not None:
            on_detached(False)
        raise

    last_exc: BaseException | None = None
    try:
        for attempt in range(attempts):
            try:
                await _download_modelscope_once(
                    name,
                    dest,
                    token=token,
                    revision=revision,
                    on_progress=on_progress,
                    on_log=on_log,
                    on_detached=on_detached,
                    expected_total=expected,
                )
                return
            except asyncio.CancelledError:
                raise
            except DownloadControl:
                # cancel / pause: propagate; _download_modelscope_once already
                # reported the release (immediate, or detached-then-thread-done).
                raise
            except Exception as exc:
                last_exc = exc
                if attempt >= attempts - 1 or not is_transient_network_error(exc):
                    raise
                delay = retry_backoff_seconds(attempt)
                msg = (
                    f"ModelScope 网络中断（{exc}），{delay:.0f}s 后重试 "
                    f"（第 {attempt + 2}/{attempts} 次尝试）…"
                )
                logger.warning(
                    "modelscope retry name=%s attempt=%s err=%s", name, attempt + 1, exc
                )
                try:
                    await on_log(msg)
                    await asyncio.sleep(delay)
                except BaseException:
                    # A transient failure means the previous attempt's thread is
                    # done (already released via on_detached(False)); this line or
                    # the sleep being interrupted by pause/cancel/stop leaves no
                    # live writer, so an immediate release is safe and idempotent.
                    if on_detached is not None:
                        on_detached(False)
                    raise
    finally:
        # The download is over (success, failure or control signal); a listing that
        # never landed is now useless and must not outlive the task.
        if not probe.done():
            probe.cancel()

    if last_exc is not None:
        raise last_exc
