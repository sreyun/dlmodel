import asyncio
import logging
import time
from pathlib import Path

import requests

from modelscope import snapshot_download
from modelscope.hub.api import HubApi

from app.config import get_settings, optional_secret
from app.downloaders.base import LogCallback, ProgressCallback
from app.downloaders.network import is_transient_network_error, retry_backoff_seconds

logger = logging.getLogger(__name__)


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _format_bytes(n: int) -> str:
    value = float(n)
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{value:.1f} {units[i]}" if i else f"{int(value)} {units[i]}"


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


async def _download_modelscope_once(
    name: str,
    dest: Path,
    *,
    token: str | None,
    revision: str | None,
    on_progress: ProgressCallback,
    on_log: LogCallback,
    on_detached=None,
) -> None:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    await on_progress(_dir_size_bytes(dest), None, None)

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

    try:
        while not download_task.done():
            await asyncio.sleep(0.8)
            size = _dir_size_bytes(dest)
            now = time.monotonic()
            elapsed = max(now - last_ts, 0.001)
            speed = (size - last_size) / elapsed if size >= last_size else None
            await on_progress(size, None, float(speed) if speed and speed > 0 else None)
            if size != last_size and (now - last_log_ts) >= 3.0:
                await on_log(f"ModelScope 已下载 {_format_bytes(size)}…")
                last_log_ts = now
            last_size = size
            last_ts = now
        result = await download_task
    except BaseException:
        if not download_task.done():

            async def _detach() -> None:
                await _drain_thread_task(download_task)
                if on_detached is not None:
                    on_detached()

            asyncio.create_task(_detach())
        elif on_detached is not None:
            on_detached()
        raise

    final_size = _dir_size_bytes(dest)
    await on_progress(final_size, final_size if final_size else None, 0.0)
    await on_log(f"ModelScope 下载完成：{result}")


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
    await on_log(f"正在从 ModelScope 下载 {name}…")

    last_exc: BaseException | None = None
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
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts - 1 or not is_transient_network_error(exc):
                raise
            delay = retry_backoff_seconds(attempt)
            msg = (
                f"ModelScope 网络中断（{exc}），{delay:.0f}s 后重试 "
                f"（{attempt + 1}/{attempts - 1}）…"
            )
            logger.warning(
                "modelscope retry name=%s attempt=%s err=%s", name, attempt + 1, exc
            )
            await on_log(msg)
            await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc
