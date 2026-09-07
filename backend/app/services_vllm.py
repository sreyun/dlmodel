import shlex

import httpx


def _friendly_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "无法连接：请检查服务地址与网络"
    if isinstance(exc, httpx.TimeoutException):
        return "连接超时"
    if isinstance(exc, httpx.HTTPError):
        return f"请求失败：{exc.__class__.__name__}"
    return str(exc)


async def vllm_health(base_url: str) -> dict:
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient() as client:
            for path in ("/health", "/v1/models"):
                try:
                    resp = await client.get(f"{base}{path}", timeout=10.0)
                    if resp.status_code == 200:
                        return {"ok": True, "detail": "已连接"}
                except httpx.HTTPError:
                    continue
            return {"ok": False, "detail": "不可达"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": _friendly_http_error(exc)}


def vllm_launch_command(model_path: str, port: int = 8000) -> str:
    model = model_path.strip()
    if not model:
        raise ValueError("模型路径不能为空")
    if not (1 <= int(port) <= 65535):
        raise ValueError("端口需在 1–65535 之间")
    return (
        "python -m vllm.entrypoints.openai.api_server "
        f"--model {shlex.quote(model)} --port {int(port)}"
    )
