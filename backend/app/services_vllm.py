import httpx


async def vllm_health(base_url: str) -> dict:
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient() as client:
            for path in ("/health", "/v1/models"):
                try:
                    resp = await client.get(f"{base}{path}", timeout=10.0)
                    if resp.status_code == 200:
                        return {"ok": True, "detail": "connected"}
                except httpx.HTTPError:
                    continue
            return {"ok": False, "detail": "unreachable"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": str(exc)}


def vllm_launch_command(model_path: str, port: int = 8000) -> str:
    return (
        f"python -m vllm.entrypoints.openai.api_server "
        f"--model {model_path} --port {port}"
    )
