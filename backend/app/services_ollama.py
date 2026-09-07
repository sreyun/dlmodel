import httpx

from app.downloaders.ollama import download_ollama


async def ollama_health(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
            if resp.status_code == 200:
                return {"ok": True, "detail": "已连接"}
            return {"ok": False, "detail": f"HTTP {resp.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": str(exc)}


async def ollama_list_models(base_url: str) -> list[dict]:
    url = f"{base_url.rstrip('/')}/api/tags"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return data.get("models", [])


async def _noop(*_args, **_kwargs) -> None:
    pass


async def ollama_pull(base_url: str, name: str) -> None:
    await download_ollama(name, base_url, _noop, _noop)
