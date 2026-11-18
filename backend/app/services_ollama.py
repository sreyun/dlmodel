import httpx


def _friendly_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "无法连接：请检查服务地址与网络"
    if isinstance(exc, httpx.TimeoutException):
        return "连接超时"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP 状态码 {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return f"请求失败：{exc.__class__.__name__}"
    return str(exc)


async def ollama_health(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
            if resp.status_code == 200:
                return {"ok": True, "detail": "已连接"}
            return {"ok": False, "detail": f"HTTP 状态码 {resp.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": _friendly_http_error(exc)}


async def ollama_list_models(base_url: str) -> list[dict]:
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            return data.get("models", [])
    except httpx.HTTPError as exc:
        raise RuntimeError(_friendly_http_error(exc)) from exc
