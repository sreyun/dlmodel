import time
from pathlib import Path

import httpx

from app.downloaders.base import ProgressCallback

_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0)


async def http_download(
    url: str,
    dest: Path,
    on_progress: ProgressCallback,
    headers: dict[str, str] | None = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    last_time = time.monotonic()
    last_bytes = 0
    total: int | None = None

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_TIMEOUT, max_redirects=5
    ) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            if content_length := resp.headers.get("content-length"):
                total = int(content_length)

            with dest.open("wb") as f:
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
