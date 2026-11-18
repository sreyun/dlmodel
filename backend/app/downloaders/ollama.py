import asyncio
import json
import time

import httpx

from app.downloaders.base import LogCallback, ProgressCallback


def _layer_key(data: dict) -> str:
    """Identity of the blob a progress line refers to.

    Ollama interleaves several layer transfers and every line only carries *that*
    layer's ``total``: feeding them straight through made the bar restart near zero
    for each layer, so a 12 GB pull looked like a dozen small ones. Keying by digest
    lets the layers be summed into one honest repo-wide ratio.
    """
    for field in ("digest", "id"):
        value = data.get(field)
        if isinstance(value, str) and value:
            return value
    return data.get("status") or "_"


# Ollama can go quiet for minutes (digest verification, a stalled upstream). The
# progress hook is where pause / cancel are observed, so it is ticked on a timer
# while no line arrives instead of only when the server speaks.
_HEARTBEAT_SEC = 2.0


async def _lines_with_heartbeat(resp: httpx.Response, heartbeat):
    lines = resp.aiter_lines().__aiter__()
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(lines.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=_HEARTBEAT_SEC)
            if not done:
                await heartbeat()
                continue
            try:
                line = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            yield line
    finally:
        if pending is not None:
            pending.cancel()


async def download_ollama(
    name: str,
    base_url: str,
    on_progress: ProgressCallback,
    on_log: LogCallback,
) -> None:
    url = f"{base_url}/api/pull"
    await on_log(f"正在从 {url} 拉取 Ollama 模型 {name}")

    layers: dict[str, dict[str, int]] = {}
    sample = {"ts": 0.0, "done": 0}

    async def report() -> None:
        done = sum(item["completed"] for item in layers.values())
        total = sum(item["total"] for item in layers.values())
        speed: float | None = None
        now = time.monotonic()
        elapsed = now - sample["ts"]
        # /api/pull never reports a rate, so derive it from our own samples; without
        # this the UI could only ever show "测算中" for Ollama pulls.
        if sample["ts"] and elapsed > 0 and done > sample["done"]:
            speed = (done - sample["done"]) / elapsed
        sample["ts"] = now
        sample["done"] = done
        await on_progress(done, max(total, done) or None, speed)

    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST", url, json={"name": name}, timeout=None
        ) as resp:
            resp.raise_for_status()
            async for line in _lines_with_heartbeat(resp, report):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                status = data.get("status", "")
                await on_log(status)

                total = data.get("total")
                completed = data.get("completed")
                if isinstance(total, int) and isinstance(completed, int):
                    item = layers.setdefault(
                        _layer_key(data), {"total": 0, "completed": 0}
                    )
                    # A layer announces its size on every line and can resend a
                    # slightly stale offset; only growth is trustworthy.
                    item["total"] = max(item["total"], total)
                    item["completed"] = max(item["completed"], completed)
                    await report()

                if status == "success":
                    break
                if "error" in data:
                    raise RuntimeError(data["error"])

    await on_log(f"Ollama 拉取完成：{name}")
