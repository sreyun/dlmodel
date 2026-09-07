import os
from ipaddress import ip_address, ip_network
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import require_admin
from app.config import get_settings, optional_secret
from app.db import delete_setting, get_setting, set_setting

router = APIRouter()

_MERGE_KEYS = (
    "hf_endpoint",
    "hf_token",
    "modelscope_api_token",
    "download_concurrency",
    "aria2_connections",
    "ollama_base_url",
    "vllm_base_url",
)
_INT_KEYS = frozenset({"download_concurrency", "aria2_connections"})
_TOKEN_KEYS = frozenset({"hf_token", "modelscope_api_token"})
_URL_KEYS = frozenset({"hf_endpoint", "ollama_base_url", "vllm_base_url"})
_ENV_NAMES = {
    "hf_endpoint": "HF_ENDPOINT",
    "hf_token": "HF_TOKEN",
    "modelscope_api_token": "MODELSCOPE_API_TOKEN",
    "download_concurrency": "DOWNLOAD_CONCURRENCY",
    "aria2_connections": "ARIA2_CONNECTIONS",
    "ollama_base_url": "OLLAMA_BASE_URL",
    "vllm_base_url": "VLLM_BASE_URL",
}
_BLOCKED_NETWORKS = (
    ip_network("169.254.169.254/32"),  # cloud metadata
    ip_network("169.254.0.0/16"),
)


class SettingsUpdate(BaseModel):
    hf_endpoint: str | None = Field(default=None, max_length=512)
    hf_token: str | None = Field(default=None, max_length=4096)
    modelscope_api_token: str | None = Field(default=None, max_length=4096)
    download_concurrency: int | None = Field(default=None, ge=1, le=32)
    aria2_connections: int | None = Field(default=None, ge=1, le=64)
    ollama_base_url: str | None = Field(default=None, max_length=512)
    vllm_base_url: str | None = Field(default=None, max_length=512)
    clear_hf_token: bool = False
    clear_modelscope_api_token: bool = False


def _usable_override(key: str, raw: str | None) -> bool:
    if raw is None:
        return False
    if key in _TOKEN_KEYS and optional_secret(raw) is None:
        return False
    if key in _INT_KEYS:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return False
        return value >= 1
    if isinstance(raw, str) and not raw.strip() and key not in _TOKEN_KEYS:
        return False
    return True


def _coerce(key: str, raw: str):
    if key in _INT_KEYS:
        return int(str(raw).strip())
    return raw


def _validate_url(key: str, value: str) -> None:
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail=f"{key} 需要是合法的 http(s) 地址",
        )
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail=f"{key} 不允许在 URL 中嵌入凭据")
    host = parsed.hostname or ""
    if host in {"metadata.google.internal", "metadata"}:
        raise HTTPException(status_code=400, detail=f"{key} 地址不被允许")
    try:
        addr = ip_address(host)
    except ValueError:
        return
    for network in _BLOCKED_NETWORKS:
        if addr in network:
            raise HTTPException(status_code=400, detail=f"{key} 地址不被允许")


async def effective_settings() -> dict:
    base = get_settings()
    out = {key: getattr(base, key) for key in _MERGE_KEYS}
    for key in _MERGE_KEYS:
        raw = await get_setting(key)
        if not _usable_override(key, raw):
            continue
        out[key] = _coerce(key, raw)
    for key in _TOKEN_KEYS:
        out[key] = optional_secret(out.get(key))
    return out


def public_settings(merged: dict) -> dict:
    """API-safe settings: never return raw tokens."""
    return {
        "hf_endpoint": merged["hf_endpoint"],
        "download_concurrency": merged["download_concurrency"],
        "aria2_connections": merged["aria2_connections"],
        "ollama_base_url": merged["ollama_base_url"],
        "vllm_base_url": merged["vllm_base_url"],
        "hf_token_set": bool(merged.get("hf_token")),
        "modelscope_api_token_set": bool(merged.get("modelscope_api_token")),
    }


async def apply_sqlite_overrides() -> None:
    # Never export secrets into process environ (visible in /proc, child processes).
    for key, env_name in _ENV_NAMES.items():
        if key in _TOKEN_KEYS:
            continue
        raw = await get_setting(key)
        if not _usable_override(key, raw):
            continue
        os.environ[env_name] = str(raw)


@router.get("/api/settings")
async def get_settings_route(_: None = Depends(require_admin)) -> dict:
    return public_settings(await effective_settings())


@router.put("/api/settings")
async def put_settings_route(
    body: SettingsUpdate,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    updates = body.model_dump(exclude_unset=True)
    clear_hf = bool(updates.pop("clear_hf_token", False))
    clear_ms = bool(updates.pop("clear_modelscope_api_token", False))

    if clear_hf:
        updates.pop("hf_token", None)
        await delete_setting("hf_token")
        os.environ.pop("HF_TOKEN", None)
    if clear_ms:
        updates.pop("modelscope_api_token", None)
        await delete_setting("modelscope_api_token")
        os.environ.pop("MODELSCOPE_API_TOKEN", None)

    for key, value in list(updates.items()):
        if key not in _ENV_NAMES:
            continue
        if key in _URL_KEYS and isinstance(value, str):
            _validate_url(key, value)
        if key in _INT_KEYS and value is not None and int(value) < 1:
            raise HTTPException(status_code=400, detail=f"{key} 必须大于等于 1")
        stored = "" if value is None else str(value)
        if key in _TOKEN_KEYS and optional_secret(stored) is None:
            continue
        await set_setting(key, stored)

    await apply_sqlite_overrides()

    if "download_concurrency" in updates and updates["download_concurrency"] is not None:
        queue = getattr(request.app.state, "queue", None)
        if queue is not None and hasattr(queue, "resize"):
            await queue.resize(int(updates["download_concurrency"]))

    return public_settings(await effective_settings())
