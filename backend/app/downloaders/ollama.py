import json

import httpx

from app.downloaders.base import LogCallback, ProgressCallback


async def download_ollama(
    name: str,
    base_url: str,
    on_progress: ProgressCallback,
    on_log: LogCallback,
) -> None:
    url = f"{base_url}/api/pull"
    await on_log(f"正在从 {url} 拉取 Ollama 模型 {name}")

    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST", url, json={"name": name}, timeout=None
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
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
                    await on_progress(completed, total, None)

                if status == "success":
                    break
                if "error" in data:
                    raise RuntimeError(data["error"])

    await on_log(f"Ollama pull complete for {name}")
